-- Derived functions and views. All statements are idempotent.

-- 5-year storage statistics for any calendar day, following EIA's method
-- (https://ir.eia.gov/ngs/methodology.html, "Computing the Five-Year Averages ..."):
-- weekly levels are linearly interpolated to daily values, and the stats for day D use the
-- interpolated level on the same calendar day in each of the 5 prior years.
-- Works for future days too (e.g. the week being forecast), and only ever reads data at
-- least ~1 year old, so it is always known in advance -> safe to use as a feature.
CREATE OR REPLACE FUNCTION storage_5yr_stats(p_region text, p_day date)
RETURNS TABLE (avg_level numeric, min_level numeric, max_level numeric)
LANGUAGE sql STABLE AS $$
    SELECT
        CASE WHEN count(*) = 5 THEN avg(i.level) END,
        CASE WHEN count(*) = 5 THEN min(i.level) END,
        CASE WHEN count(*) = 5 THEN max(i.level) END
    FROM generate_series(1, 5) AS k
    CROSS JOIN LATERAL (SELECT (p_day - make_interval(years => k))::date AS day) AS d
    -- p1 = last report on or before that day, p2 = the report one week later
    JOIN fact_storage_weekly AS p1
      ON p1.region = p_region AND p1.week_ending <= d.day AND p1.week_ending > d.day - 7
    JOIN fact_storage_weekly AS p2
      ON p2.region = p_region AND p2.week_ending = p1.week_ending + 7
    CROSS JOIN LATERAL (
        SELECT p1.working_gas_bcf
             + (p2.working_gas_bcf - p1.working_gas_bcf) * (d.day - p1.week_ending) / 7.0
               AS level
    ) AS i
$$;

-- Weekly storage with net change and 5-year band.
DROP VIEW IF EXISTS v_storage_weekly;
CREATE VIEW v_storage_weekly AS
WITH s AS (
    SELECT
        f.week_ending,
        f.region,
        f.release_date,
        f.working_gas_bcf,
        f.working_gas_bcf - lag(f.working_gas_bcf) OVER w AS net_change_bcf,
        fy.avg_level AS avg_level_5yr,
        fy.min_level AS min_level_5yr,
        fy.max_level AS max_level_5yr,
        -- 5-yr average weekly change = change in the interpolated 5-yr average level
        fy.avg_level - lag(fy.avg_level) OVER w AS avg_change_5yr
    FROM fact_storage_weekly AS f
    CROSS JOIN LATERAL storage_5yr_stats(f.region, f.week_ending) AS fy
    WINDOW w AS (PARTITION BY f.region ORDER BY f.week_ending)
)
SELECT
    week_ending,
    region,
    release_date,
    working_gas_bcf,
    net_change_bcf,
    round(avg_level_5yr, 1)                    AS avg_level_5yr,
    round(min_level_5yr, 1)                    AS min_level_5yr,
    round(max_level_5yr, 1)                    AS max_level_5yr,
    round(avg_change_5yr, 1)                   AS avg_change_5yr,
    round(working_gas_bcf - avg_level_5yr, 1)  AS level_vs_5yr_bcf,
    round(net_change_bcf - avg_change_5yr, 1)  AS change_vs_5yr_bcf
FROM s;

-- 5-year average level and weekly change for every Friday, including weeks not yet reported.
DROP VIEW IF EXISTS v_storage_5yr_calendar;
CREATE VIEW v_storage_5yr_calendar AS
SELECT
    w::date                                                AS week_ending,
    fy.avg_level                                           AS avg_level_5yr,
    fy.avg_level - lag(fy.avg_level) OVER (ORDER BY w)     AS avg_change_5yr
FROM generate_series(
         (SELECT min(week_ending) FROM fact_storage_weekly),
         (SELECT max(week_ending) + 28 FROM fact_storage_weekly),
         interval '7 days'
     ) AS w
CROSS JOIN LATERAL storage_5yr_stats('lower48', w::date) AS fy;
