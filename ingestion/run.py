"""Ingest EIA storage, EIA Henry Hub spot, and NOAA CPC degree days into Postgres.

    python -m ingestion.run              # incremental; re-pulls a lookback window for revisions
    python -m ingestion.run --full       # full history reload
    python -m ingestion.run --source price
"""

import argparse
from datetime import date, timedelta

import pandas as pd

from core.config import settings
from core.db import connect
from core.log import get_logger
from ingestion import cpc, eia
from ingestion.load import track_run, upsert

log = get_logger("ingest")

# How far back an incremental run re-fetches, so late revisions are picked up.
STORAGE_LOOKBACK = timedelta(weeks=8)
PRICE_LOOKBACK = timedelta(days=30)
WEATHER_LOOKBACK = timedelta(days=14)
# Storage reports are normally released the Thursday after the Friday week-ending date.
STORAGE_RELEASE_LAG = timedelta(days=6)


def _incremental_start(table: str, date_col: str, lookback: timedelta, full: bool) -> date:
    history_start = date.fromisoformat(settings()["sources"]["eia"]["history_start"])
    if full:
        return history_start
    with connect() as conn:
        latest = conn.execute(f"SELECT max({date_col}) FROM {table}").fetchone()[0]
    return history_start if latest is None else max(history_start, latest - lookback)


def ingest_storage(full: bool) -> None:
    series_id = settings()["sources"]["eia"]["storage_series"]
    start = _incremental_start("fact_storage_weekly", "week_ending", STORAGE_LOOKBACK, full)
    with track_run("eia_storage_weekly") as stats:
        raw = eia.fetch_series(series_id, start.isoformat())
        df = pd.DataFrame(
            {
                "week_ending": raw["period"],
                "region": "lower48",
                "working_gas_bcf": raw["value"],
                "release_date": [d + STORAGE_RELEASE_LAG for d in raw["period"]],
                "series_id": series_id,
            }
        )
        with connect() as conn:
            stats.rows_upserted = upsert(
                conn, "fact_storage_weekly", df,
                key_cols=["week_ending", "region"],
                value_cols=["working_gas_bcf", "release_date", "series_id"],
            )
        stats.rows_fetched = len(df)
    log.info(
        "storage: fetched %d rows since %s, upserted %d",
        stats.rows_fetched, start, stats.rows_upserted,
    )


def ingest_price(full: bool) -> None:
    series_id = settings()["sources"]["eia"]["henry_hub_series"]
    start = _incremental_start("fact_gas_price_daily", "price_date", PRICE_LOOKBACK, full)
    with track_run("eia_henry_hub_daily") as stats:
        raw = eia.fetch_series(series_id, start.isoformat())
        df = pd.DataFrame(
            {
                "price_date": raw["period"],
                "hub": "henry_hub",
                "price_usd_mmbtu": raw["value"],
                "series_id": series_id,
            }
        )
        with connect() as conn:
            stats.rows_upserted = upsert(
                conn, "fact_gas_price_daily", df,
                key_cols=["price_date", "hub"],
                value_cols=["price_usd_mmbtu", "series_id"],
            )
        stats.rows_fetched = len(df)
    log.info(
        "price: fetched %d rows since %s, upserted %d",
        stats.rows_fetched, start, stats.rows_upserted,
    )


def ingest_weather(full: bool) -> None:
    cfg = settings()["sources"]["cpc"]
    today = date.today()
    if full:
        years = range(cfg["history_start_year"], today.year + 1)
    else:
        years = sorted({(today - WEATHER_LOOKBACK).year, today.year})

    with track_run("cpc_degree_days") as stats:
        frames = []
        for year in years:
            for filename, metric, weighting in cfg["files"]:
                df = cpc.parse(cpc.fetch_year(year, filename), region=cfg["region"])
                df["region"], df["metric"], df["weighting"] = cfg["region"], metric, weighting
                df["source_file"] = f"{year}/{filename}"
                frames.append(df)
        df = pd.concat(frames, ignore_index=True)
        with connect() as conn:
            stats.rows_upserted = upsert(
                conn, "fact_weather_daily", df,
                key_cols=["obs_date", "region", "metric", "weighting"],
                value_cols=["value", "source_file"],
            )
        stats.rows_fetched = len(df)
    log.info(
        "weather: fetched %d rows for %s, upserted %d",
        stats.rows_fetched, f"{years[0]}-{years[-1]}", stats.rows_upserted,
    )


SOURCES = {"storage": ingest_storage, "price": ingest_price, "weather": ingest_weather}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="reload full history")
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    args = parser.parse_args()

    selected = SOURCES if args.source == "all" else {args.source: SOURCES[args.source]}
    failures = []
    for name, job in selected.items():
        try:
            job(args.full)
        except Exception:
            # One broken source shouldn't block the others; the failure is recorded in etl_runs.
            log.exception("%s ingestion failed", name)
            failures.append(name)
    if failures:
        raise SystemExit(f"ingestion failed for: {', '.join(failures)}")


if __name__ == "__main__":
    main()
