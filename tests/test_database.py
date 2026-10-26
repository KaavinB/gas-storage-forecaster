"""Tests against the real Postgres schema. Each test runs inside a rolled-back transaction."""

from datetime import date

import pandas as pd
import pytest

from ingestion.load import upsert

pytestmark = pytest.mark.db


def _prices(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "price_date": pd.date_range("2000-01-03", periods=len(values)).date,
            "hub": "test_hub",
            "price_usd_mmbtu": values,
            "series_id": "TEST",
        }
    )


def test_upsert_is_idempotent_and_only_counts_real_changes(db_conn):
    kwargs = dict(key_cols=["price_date", "hub"], value_cols=["price_usd_mmbtu", "series_id"])
    df = _prices([2.0, 2.1, 2.2])

    assert upsert(db_conn, "fact_gas_price_daily", df, **kwargs) == 3
    assert upsert(db_conn, "fact_gas_price_daily", df, **kwargs) == 0  # re-run: no-op

    revised = df.copy()
    revised.loc[1, "price_usd_mmbtu"] = 2.15
    assert upsert(db_conn, "fact_gas_price_daily", revised, **kwargs) == 1  # only the revision

    stored = db_conn.execute(
        "SELECT price_usd_mmbtu FROM fact_gas_price_daily "
        "WHERE hub = 'test_hub' ORDER BY price_date"
    ).fetchall()
    assert [float(r[0]) for r in stored] == [2.0, 2.15, 2.2]


def test_upsert_writes_null_for_missing_values(db_conn):
    df = pd.DataFrame({
        "run_date": [date(2000, 1, 5)] * 2,
        "target": ["storage_change_bcf", "henry_hub_direction"],
        "target_period": [date(2000, 1, 7)] * 2,
        "model_name": ["test"] * 2,
        "predicted_value": [10.0, float("nan")],
        "predicted_label": [None, "up"],
        "is_backtest": [True] * 2,
    })
    upsert(db_conn, "fact_forecast", df,
           key_cols=["run_date", "target", "target_period", "model_name", "is_backtest"],
           value_cols=["predicted_value", "predicted_label"])
    rows = db_conn.execute(
        "SELECT predicted_value IS NULL, predicted_label IS NULL FROM fact_forecast "
        "WHERE model_name = 'test' ORDER BY target DESC"
    ).fetchall()
    assert rows == [(False, True), (True, False)]


def test_dim_date_maps_every_day_to_the_friday_ending_its_storage_week(db_conn):
    rows = db_conn.execute(
        "SELECT full_date, storage_week_ending FROM dim_date "
        "WHERE full_date BETWEEN '2026-10-17' AND '2026-10-23'"
    ).fetchall()
    assert len(rows) == 7  # Saturday .. Friday
    assert {r[1] for r in rows} == {date(2026, 10, 23)}


def test_five_year_average_interpolates_to_same_calendar_day(db_conn):
    """Synthetic region: level rises 7 Bcf/week, i.e. exactly 1 Bcf/day, so the
    interpolated level on any day is easy to compute by hand."""
    start = date(2010, 1, 1)  # a Friday
    weeks = pd.date_range(start, "2016-12-30", freq="7D").date
    db_conn.execute(
        """
        INSERT INTO fact_storage_weekly
            (week_ending, region, working_gas_bcf, release_date, series_id)
        SELECT w, 'test', (w - %s::date), w + 6, 'TEST' FROM unnest(%s::date[]) AS w
        """,
        (start, list(weeks)),
    )
    target = date(2016, 12, 30)
    row = db_conn.execute(
        "SELECT avg_level_5yr, avg_change_5yr FROM v_storage_weekly "
        "WHERE region = 'test' AND week_ending = %s",
        (target,),
    ).fetchone()
    # Same calendar day in 2011..2015, level = days since 2010-01-01
    expected = sum((target.replace(year=target.year - k) - start).days for k in range(1, 6)) / 5
    assert float(row[0]) == pytest.approx(expected, abs=0.1)
    assert float(row[1]) == pytest.approx(7.0, abs=0.1)
