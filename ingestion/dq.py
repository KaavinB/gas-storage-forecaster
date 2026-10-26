"""Data-quality checks. Each check is a SQL query returning one number that must stay at or
below its thresholds (e.g. "days since last report", "rows out of range").
Results go to dq_results; the process exits non-zero on any failure so the daily
pipeline stops before forecasting or publishing on bad data.

    python -m ingestion.dq
    python -m ingestion.dq --as-of 2026-10-26
"""

import argparse
import uuid
from dataclasses import dataclass
from datetime import date

from core.db import connect
from core.log import get_logger

log = get_logger("dq")


@dataclass(frozen=True)
class Check:
    name: str
    table: str
    sql: str  # returns a single numeric value; may reference %(as_of)s
    warn_above: float | None
    fail_above: float | None  # None = warn-only: never blocks the pipeline
    description: str


CHECKS = [
    # --- Freshness: thresholds reflect each source's normal publication lag ---
    Check(
        "storage_freshness_days", "fact_storage_weekly",
        "SELECT %(as_of)s::date - max(week_ending) FROM fact_storage_weekly",
        warn_above=13, fail_above=20,
        description="Week ending Friday is published the next Thursday, so up to 13 days is normal",
    ),
    Check(
        "price_freshness_days", "fact_gas_price_daily",
        "SELECT %(as_of)s::date - max(price_date) FROM fact_gas_price_daily",
        warn_above=10, fail_above=17,
        description="EIA republishes Henry Hub spot weekly, so ~1 week of lag is normal",
    ),
    Check(
        "weather_freshness_days", "fact_weather_daily",
        "SELECT %(as_of)s::date - max(obs_date) FROM fact_weather_daily",
        warn_above=4, fail_above=7,
        description="CPC degree days normally lag by 1-2 days",
    ),
    # --- Completeness ---
    Check(
        "storage_missing_weeks", "fact_storage_weekly",
        """SELECT count(*) FROM (
               SELECT week_ending - lag(week_ending) OVER (ORDER BY week_ending) AS gap
               FROM fact_storage_weekly WHERE region = 'lower48') g
           WHERE gap <> 7""",
        warn_above=None, fail_above=0,
        description="Consecutive storage weeks must be exactly 7 days apart",
    ),
    Check(
        "weather_missing_days", "fact_weather_daily",
        """SELECT coalesce(sum((max_d - min_d + 1) - n), 0) FROM (
               SELECT min(obs_date) AS min_d, max(obs_date) AS max_d, count(*) AS n
               FROM fact_weather_daily GROUP BY region, metric, weighting) s""",
        warn_above=None, fail_above=0,
        description="Each degree-day series must have one row per calendar day",
    ),
    Check(
        "price_max_gap_days_last_year", "fact_gas_price_daily",
        """SELECT coalesce(max(gap), 0) FROM (
               SELECT price_date - lag(price_date) OVER (ORDER BY price_date) AS gap
               FROM fact_gas_price_daily
               WHERE hub = 'henry_hub' AND price_date > %(as_of)s::date - 365) g""",
        warn_above=5, fail_above=7,
        description="Thursday holidays (Thanksgiving, Christmas) legitimately leave Wed->Mon gaps",
    ),
    # --- Validity ---
    Check(
        "storage_not_friday", "fact_storage_weekly",
        "SELECT count(*) FROM fact_storage_weekly WHERE extract(isodow FROM week_ending) <> 5",
        warn_above=None, fail_above=0,
        description="EIA storage weeks end on Fridays",
    ),
    Check(
        "storage_level_out_of_range", "fact_storage_weekly",
        "SELECT count(*) FROM fact_storage_weekly WHERE working_gas_bcf NOT BETWEEN 500 AND 4500",
        warn_above=None, fail_above=0,
        description="Lower-48 working gas has stayed within ~800-4,050 Bcf since 2010",
    ),
    Check(
        "storage_change_out_of_range", "v_storage_weekly",
        "SELECT count(*) FROM v_storage_weekly WHERE abs(net_change_bcf) > 400",
        warn_above=None, fail_above=0,
        description="Record weekly withdrawal is -359 Bcf (Jan 2018 and Jan 2026)",
    ),
    Check(
        "price_out_of_range", "fact_gas_price_daily",
        "SELECT count(*) FROM fact_gas_price_daily "
        "WHERE price_usd_mmbtu <= 0 OR price_usd_mmbtu > 50",
        warn_above=None, fail_above=0,
        description="Highest Henry Hub spot in the data is $30.72 (2026-01-23 cold snap)",
    ),
    Check(
        "price_dod_moves_over_40pct_last_30d", "fact_gas_price_daily",
        """SELECT count(*) FROM (
               SELECT price_date, price_usd_mmbtu / lag(price_usd_mmbtu)
                          OVER (ORDER BY price_date) - 1 AS ret
               FROM fact_gas_price_daily WHERE hub = 'henry_hub') r
           WHERE abs(ret) > 0.40 AND price_date > %(as_of)s::date - 30""",
        # Warn-only: in Jan 2026 spot moved +265% / -46% in days. Those were real, and blocking
        # the pipeline then would stop forecasts and alerts exactly when the desk needs them.
        warn_above=0, fail_above=None,
        description="Real during cold snaps but rare; flag for a human look, never block",
    ),
    Check(
        "degree_days_out_of_range", "fact_weather_daily",
        "SELECT count(*) FROM fact_weather_daily WHERE value > 80",
        warn_above=None, fail_above=0,
        description="A CONUS-average daily HDD or CDD above 80 is physically implausible",
    ),
]


def evaluate(observed: float | None, check: Check) -> str:
    if observed is None:
        return "fail"
    if check.fail_above is not None and observed > check.fail_above:
        return "fail"
    if check.warn_above is not None and observed > check.warn_above:
        return "warn"
    return "pass"


def run(as_of: date) -> dict[str, int]:
    run_id = uuid.uuid4()
    counts = {"pass": 0, "warn": 0, "fail": 0}
    with connect() as conn:
        for check in CHECKS:
            observed = conn.execute(check.sql, {"as_of": as_of}).fetchone()[0]
            observed = None if observed is None else float(observed)
            status = evaluate(observed, check)
            counts[status] += 1
            conn.execute(
                """INSERT INTO dq_results
                       (run_id, check_name, table_name, status, observed, threshold, details)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (run_id, check.name, check.table, status, observed,
                 check.fail_above, check.description),
            )
            level = {"pass": log.info, "warn": log.warning, "fail": log.error}[status]
            level("%-36s %-4s observed=%s fail_above=%s", check.name, status.upper(),
                  observed, check.fail_above)
    log.info("dq run %s: %d pass, %d warn, %d fail", run_id, *counts.values())
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Run data-quality checks")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    counts = run(parser.parse_args().as_of)
    if counts["fail"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
