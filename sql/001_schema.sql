-- Core star schema. Every statement is idempotent so `make migrate` is safe to re-run.
-- Convention: fact tables store raw source values only; anything derived
-- (weekly change, 5-year stats) lives in views so source revisions flow through automatically.

-- ---------------------------------------------------------------------------
-- Dimension: one row per calendar day
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_date (
    full_date            date PRIMARY KEY,
    date_key             integer  NOT NULL UNIQUE,      -- YYYYMMDD, handy for BI tools
    year                 smallint NOT NULL,
    quarter              smallint NOT NULL,
    month                smallint NOT NULL,
    day_of_month         smallint NOT NULL,
    day_of_week          smallint NOT NULL,             -- ISO: 1 = Monday ... 7 = Sunday
    iso_year             smallint NOT NULL,
    iso_week             smallint NOT NULL,
    is_weekend           boolean  NOT NULL,
    storage_week_ending  date     NOT NULL,             -- EIA storage weeks run Saturday -> Friday
    season               text     NOT NULL CHECK (season IN ('injection', 'withdrawal'))
);

INSERT INTO dim_date
SELECT
    d::date,
    to_char(d, 'YYYYMMDD')::int,
    extract(year FROM d),
    extract(quarter FROM d),
    extract(month FROM d),
    extract(day FROM d),
    extract(isodow FROM d),
    extract(isoyear FROM d),
    extract(week FROM d),
    extract(isodow FROM d) IN (6, 7),
    -- days until the next Friday (ISO dow 5); a Friday maps to itself
    d::date + ((5 - extract(isodow FROM d)::int + 7) % 7),
    CASE WHEN extract(month FROM d) BETWEEN 4 AND 10 THEN 'injection' ELSE 'withdrawal' END
FROM generate_series('2000-01-01'::date, '2035-12-31'::date, interval '1 day') AS d
ON CONFLICT (full_date) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_storage_weekly (
    week_ending      date          NOT NULL REFERENCES dim_date (full_date),
    region           text          NOT NULL DEFAULT 'lower48',
    working_gas_bcf  numeric(8, 1) NOT NULL CHECK (working_gas_bcf >= 0),
    -- EIA's API has no publication timestamp. Reports normally come out the Thursday
    -- after week_ending (+6 days); holidays can push that later. Used to prevent look-ahead.
    release_date     date          NOT NULL,
    series_id        text          NOT NULL,
    ingested_at      timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (week_ending, region)
);

CREATE TABLE IF NOT EXISTS fact_gas_price_daily (
    price_date       date          NOT NULL REFERENCES dim_date (full_date),
    hub              text          NOT NULL DEFAULT 'henry_hub',
    price_usd_mmbtu  numeric(8, 3) NOT NULL,
    series_id        text          NOT NULL,
    ingested_at      timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (price_date, hub)
);

-- Long format: one row per (day, region, metric, weighting) so new weightings need no DDL.
CREATE TABLE IF NOT EXISTS fact_weather_daily (
    obs_date     date          NOT NULL REFERENCES dim_date (full_date),
    region       text          NOT NULL,
    metric       text          NOT NULL CHECK (metric IN ('hdd', 'cdd')),
    weighting    text          NOT NULL,                 -- 'population' | 'utility_gas'
    value        numeric(6, 1) NOT NULL CHECK (value >= 0),
    source_file  text          NOT NULL,
    ingested_at  timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (obs_date, region, metric, weighting)
);

CREATE TABLE IF NOT EXISTS fact_forecast (
    forecast_id      bigserial    PRIMARY KEY,
    run_date         date         NOT NULL,              -- as-of date: only data available by then was used
    target           text         NOT NULL CHECK (target IN ('storage_change_bcf', 'henry_hub_direction')),
    target_period    date         NOT NULL,              -- storage week_ending, or the price date forecast
    model_name       text         NOT NULL,
    predicted_value  numeric(10, 3),                     -- Bcf, or P(price up) for direction
    predicted_label  text,                               -- 'up' / 'down' for direction
    baseline_value   numeric(10, 3),                     -- naive baseline for the same target
    is_backtest      boolean      NOT NULL DEFAULT false,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (run_date, target, target_period, model_name, is_backtest)
);

-- ---------------------------------------------------------------------------
-- Operations: alerts, data quality, ETL run log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id         bigserial    PRIMARY KEY,
    alert_key        text         NOT NULL UNIQUE,       -- rule + subject; makes re-runs idempotent
    rule_name        text         NOT NULL,
    run_date         date         NOT NULL,
    subject_date     date         NOT NULL,
    severity         text         NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message          text         NOT NULL,
    details          jsonb        NOT NULL DEFAULT '{}',
    channel          text         NOT NULL,
    delivery_status  text         NOT NULL DEFAULT 'pending'
                     CHECK (delivery_status IN ('pending', 'sent', 'failed', 'skipped')),
    sent_at          timestamptz,
    created_at       timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dq_results (
    dq_id        bigserial    PRIMARY KEY,
    run_id       uuid         NOT NULL,
    check_name   text         NOT NULL,
    table_name   text         NOT NULL,
    status       text         NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    observed     numeric,
    threshold    numeric,
    details      text,
    checked_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS etl_runs (
    run_id         uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    job            text         NOT NULL,
    started_at     timestamptz  NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text         NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    rows_fetched   integer,
    rows_upserted  integer,                              -- inserted + actually changed
    error          text
);
