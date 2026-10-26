-- Derived views. CREATE OR REPLACE keeps these idempotent.

-- Weekly storage with net change and 5-year statistics, following EIA's method
-- (https://ir.eia.gov/ngs/methodology.html, "Computing the Five-Year Averages ..."):
-- weekly levels are linearly interpolated to daily values, and the 5-year stats for
-- week W use the interpolated level on the same calendar day in each of the 5 prior years.
-- Only past years are used, so these are known in advance -> safe to use as features.
DROP VIEW IF EXISTS v_storage_weekly;
CREATE VIEW v_storage_weekly AS
WITH s AS (
    SELECT
        week_ending,
        region,
        working_gas_bcf,
        release_date,
        working_gas_bcf - lag(working_gas_bcf) OVER (PARTITION BY region ORDER BY week_ending)
            AS net_change_bcf
    FROM fact_storage_weekly
),
five_year AS (
    SELECT
        s.*,
        fy.avg_level_5yr,
        fy.min_level_5yr,
        fy.max_level_5yr
    FROM s
    LEFT JOIN LATERAL (
        SELECT
            CASE WHEN count(*) = 5 THEN avg(i.level) END AS avg_level_5yr,
            CASE WHEN count(*) = 5 THEN min(i.level) END AS min_level_5yr,
            CASE WHEN count(*) = 5 THEN max(i.level) END AS max_level_5yr
        FROM generate_series(1, 5) AS k
        CROSS JOIN LATERAL (SELECT (s.week_ending - make_interval(years => k))::date AS day) AS d
        -- p1 = last report on or before that day, p2 = the report one week later
        JOIN fact_storage_weekly AS p1
          ON p1.region = s.region AND p1.week_ending <= d.day AND p1.week_ending > d.day - 7
        JOIN fact_storage_weekly AS p2
          ON p2.region = s.region AND p2.week_ending = p1.week_ending + 7
        CROSS JOIN LATERAL (
            SELECT p1.working_gas_bcf
                 + (p2.working_gas_bcf - p1.working_gas_bcf) * (d.day - p1.week_ending) / 7.0
                   AS level
        ) AS i
    ) AS fy ON true
)
SELECT
    week_ending,
    region,
    release_date,
    working_gas_bcf,
    net_change_bcf,
    round(avg_level_5yr, 1) AS avg_level_5yr,
    round(min_level_5yr, 1) AS min_level_5yr,
    round(max_level_5yr, 1) AS max_level_5yr,
    -- 5-yr average weekly change = change in the interpolated 5-yr average level
    round(avg_level_5yr - lag(avg_level_5yr) OVER (PARTITION BY region ORDER BY week_ending), 1)
        AS avg_change_5yr,
    round(working_gas_bcf - avg_level_5yr, 1) AS level_vs_5yr_bcf,
    round(
        net_change_bcf
        - (avg_level_5yr - lag(avg_level_5yr) OVER (PARTITION BY region ORDER BY week_ending)),
        1
    ) AS change_vs_5yr_bcf
FROM five_year;
