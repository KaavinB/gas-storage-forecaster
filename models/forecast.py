"""Daily live forecast: retrain on everything published so far, forecast the next storage
report and next week's Henry Hub direction, store to fact_forecast, and score past forecasts
whose outcomes have since been published.

    python -m models.forecast
    python -m models.forecast --as-of 2026-10-28
"""

import argparse
from datetime import date

import pandas as pd

from core.db import connect
from core.log import get_logger
from ingestion.load import upsert
from models.backtest import label_known_dates
from models.features import FEATURES, backtest_issue_dates, build_features, load_inputs
from models.registry import (
    direction_models,
    fit_predict_direction,
    fit_predict_storage,
    storage_models,
    with_direction_labels,
)

log = get_logger("forecast")


def forecast(as_of: date) -> pd.DataFrame:
    inputs = load_inputs()
    history = build_features(inputs, backtest_issue_dates(inputs)).dropna(subset=FEATURES)
    live = build_features(inputs, [as_of])
    if live.empty:
        raise RuntimeError(f"not enough published data to forecast as of {as_of}")
    row = live.iloc[0]
    today = pd.Timestamp(as_of)
    storage_known, price_known = label_known_dates(history)
    records = []

    base = dict(run_date=as_of, target="storage_change_bcf", target_period=row["target_week"],
                baseline_value=row["baseline_storage"])
    train = history[(storage_known <= today) & history["y_storage"].notna()]
    records.append(base | {"model_name": "baseline_5yr",
                           "predicted_value": row["baseline_storage"]})
    for name, model in storage_models().items():
        records.append(base | {"model_name": name,
                               "predicted_value": fit_predict_storage(model, train, live)[0]})

    base = dict(run_date=as_of, target="henry_hub_direction",
                target_period=(today + pd.Timedelta(days=7)).date(),
                baseline_value=row["baseline_price_up"])
    train = history[(price_known <= today) & history["y_price_up"].notna()]
    records.append(base | {"model_name": "baseline_persistence",
                           "predicted_value": row["baseline_price_up"]})
    for name, model in direction_models().items():
        records.append(base | {"model_name": name,
                               "predicted_value": fit_predict_direction(model, train, live)[0]})

    out = with_direction_labels(pd.DataFrame(records))
    is_dir = out["target"] == "henry_hub_direction"
    log.info(
        "as of %s: storage week ending %s -> %s (5-yr avg %+.1f) | weather days observed %d/7",
        as_of, row["target_week"],
        ", ".join(f"{r.model_name} {r.predicted_value:+.1f}"
                  for r in out[~is_dir].itertuples()),
        row["baseline_storage"], row["tgt_weather_days_observed"],
    )
    return out


def store(forecasts: pd.DataFrame) -> int:
    with connect() as conn:
        return upsert(
            conn, "fact_forecast", forecasts.assign(is_backtest=False),
            key_cols=["run_date", "target", "target_period", "model_name", "is_backtest"],
            value_cols=["predicted_value", "predicted_label", "baseline_value"],
        )


# Fill in outcomes for live forecasts once they are published. Same definitions as the
# training targets in models/features.py.
SCORE_STORAGE = """
UPDATE fact_forecast AS f
SET actual_value = s.net_change_bcf, updated_at = now()
FROM v_storage_weekly AS s
WHERE f.target = 'storage_change_bcf' AND NOT f.is_backtest
  AND s.region = 'lower48' AND s.week_ending = f.target_period
  AND f.actual_value IS DISTINCT FROM s.net_change_bcf
"""

SCORE_DIRECTION = """
UPDATE fact_forecast AS f
SET actual_value = o.went_up, updated_at = now()
FROM (
    SELECT f2.forecast_id,
           (avg(p.price_usd_mmbtu) FILTER (WHERE p.price_date >  f2.run_date)
          > avg(p.price_usd_mmbtu) FILTER (WHERE p.price_date <= f2.run_date))::int AS went_up
    FROM fact_forecast AS f2
    JOIN fact_gas_price_daily AS p
      ON p.hub = 'henry_hub'
     AND p.price_date >  f2.run_date - 7
     AND p.price_date <= f2.run_date + 7
    WHERE f2.target = 'henry_hub_direction' AND NOT f2.is_backtest
      AND f2.run_date + 7 <= (SELECT max(price_date) FROM fact_gas_price_daily
                              WHERE hub = 'henry_hub')
    GROUP BY f2.forecast_id
) AS o
WHERE f.forecast_id = o.forecast_id AND f.actual_value IS DISTINCT FROM o.went_up
"""


def score() -> tuple[int, int]:
    with connect() as conn:
        storage = conn.execute(SCORE_STORAGE).rowcount
        direction = conn.execute(SCORE_DIRECTION).rowcount
    return storage, direction


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily forecast")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    as_of = parser.parse_args().as_of
    log.info("stored %d forecast rows", store(forecast(as_of)))
    log.info("scored %d storage and %d direction forecasts", *score())


if __name__ == "__main__":
    main()
