"""Export everything the static dashboard needs into dashboards/site/data/.

    python -m dashboards.export

Writes dashboard.json (read by the site) plus CSVs of the same tables, so the data can
also be pulled into Tableau / Excel. Runs at the end of the daily pipeline.
"""

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from core.config import ROOT, settings
from core.db import read_sql
from core.log import get_logger

log = get_logger("export")

OUT = ROOT / "dashboards" / "site" / "data"
HISTORY_START = "2016-01-01"
RELEASE_LAG = timedelta(days=6)
STORAGE_MODELS = ["baseline_5yr", "ridge", "xgboost"]


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
        elif df[col].dtype == object:
            df[col] = df[col].map(lambda v: v.isoformat() if isinstance(v, date) else v)
        elif pd.api.types.is_float_dtype(df[col]):
            df[col] = df[col].round(3)
    return json.loads(df.to_json(orient="records"))


def storage_history() -> pd.DataFrame:
    df = read_sql(
        """SELECT week_ending, release_date, working_gas_bcf AS level, net_change_bcf AS change,
                  avg_level_5yr AS avg5, min_level_5yr AS min5, max_level_5yr AS max5,
                  avg_change_5yr AS avg_change5
           FROM v_storage_weekly WHERE region = 'lower48' AND week_ending >= %(start)s
           ORDER BY week_ending""",
        {"start": HISTORY_START},
    )
    return df.astype({c: float for c in df.columns if c not in ("week_ending", "release_date")})


def storage_forecasts() -> pd.DataFrame:
    """One forecast per (target week, model): the latest live forecast issued before the
    report's release if there is one, otherwise the walk-forward backtest forecast."""
    df = read_sql(
        """SELECT target_period AS week_ending, model_name, run_date, is_backtest,
                  predicted_value, actual_value
           FROM fact_forecast
           WHERE target = 'storage_change_bcf' AND model_name = ANY(%(models)s)""",
        {"models": STORAGE_MODELS},
    )
    if df.empty:
        return pd.DataFrame(columns=["week_ending", *STORAGE_MODELS, "actual", "source"])
    df = df[pd.to_datetime(df["run_date"]) < pd.to_datetime(df["week_ending"]) + RELEASE_LAG]
    df = df.sort_values(["is_backtest", "run_date"], ascending=[True, False])  # live first
    df = df.drop_duplicates(["week_ending", "model_name"])
    wide = df.pivot(index="week_ending", columns="model_name", values="predicted_value")
    wide["actual"] = df.groupby("week_ending")["actual_value"].first()
    live = df[~df["is_backtest"]].groupby("week_ending").size()
    wide["source"] = np.where(wide.index.isin(live.index), "live", "backtest")
    return wide.reset_index().sort_values("week_ending").astype(
        {m: float for m in [*STORAGE_MODELS, "actual"]}
    )


def rolling_mae(forecasts: pd.DataFrame, window: int = 52) -> pd.DataFrame:
    scored = forecasts.dropna(subset=["actual"]).set_index("week_ending").astype(
        {m: float for m in [*STORAGE_MODELS, "actual"]}
    )
    out = pd.DataFrame(index=scored.index)
    for m in STORAGE_MODELS:
        out[m] = (scored[m] - scored["actual"]).abs().rolling(window, min_periods=26).mean()
    return out.dropna().reset_index()


def prices() -> pd.DataFrame:
    df = read_sql(
        """SELECT price_date, price_usd_mmbtu AS price FROM fact_gas_price_daily
           WHERE hub = 'henry_hub' AND price_date >= %(start)s ORDER BY price_date""",
        {"start": HISTORY_START},
    )
    df["price"] = df["price"].astype(float)
    df["dod_pct"] = 100 * df["price"].pct_change()
    return df


def latest_live() -> dict:
    df = read_sql(
        """SELECT run_date, target, target_period, model_name, predicted_value, baseline_value
           FROM fact_forecast
           WHERE NOT is_backtest AND run_date = (SELECT max(run_date) FROM fact_forecast
                                                 WHERE NOT is_backtest)"""
    )
    if df.empty:
        return {}
    by_model = {
        (r.target, r.model_name): float(r.predicted_value) for r in df.itertuples()
    }
    cfg = settings()
    storage = df[df["target"] == "storage_change_bcf"].iloc[0]
    week = storage["target_period"]
    primary = by_model[("storage_change_bcf", cfg["forecast"]["storage_model"])]
    baseline = float(storage["baseline_value"])
    return {
        "run_date": storage["run_date"].isoformat(),
        "storage": {
            "week_ending": week.isoformat(),
            "release_date": (week + RELEASE_LAG).isoformat(),
            "model": cfg["forecast"]["storage_model"],
            "forecast": primary,
            "ridge": by_model.get(("storage_change_bcf", "ridge")),
            "baseline": baseline,
            "deviation": primary - baseline,
            "threshold": cfg["alerts"]["storage_vs_5yr_bcf"],
        },
        "direction": {
            "model": cfg["forecast"]["direction_model"],
            "prob_up": by_model.get(("henry_hub_direction", cfg["forecast"]["direction_model"])),
            "window_end": (storage["run_date"] + timedelta(days=7)).isoformat(),
        },
    }


def alert_log() -> list[dict]:
    df = read_sql(
        """SELECT created_at, run_date, subject_date, rule_name, severity, message,
                  delivery_status FROM alerts ORDER BY created_at DESC LIMIT 100"""
    )
    df["created_at"] = df["created_at"].map(lambda t: t.strftime("%Y-%m-%d %H:%M UTC"))
    return _records(df)


def alert_replay(forecasts: pd.DataFrame, price_df: pd.DataFrame) -> list[dict]:
    """What the current rules would have raised historically, from backtest forecasts and
    price history. Clearly labelled as a replay on the dashboard; never mixed with the log."""
    cfg = settings()["alerts"]
    critical = cfg["critical_multiple"]
    model = settings()["forecast"]["storage_model"]
    rows = []
    dev = forecasts[model] - forecasts["baseline_5yr"]
    for week, d, f, b in zip(forecasts["week_ending"], dev, forecasts[model],
                             forecasts["baseline_5yr"], strict=True):
        if abs(d) > cfg["storage_vs_5yr_bcf"]:
            side = "looser" if d > 0 else "tighter"
            rows.append({
                "subject_date": week.isoformat(), "rule_name": "storage_vs_5yr",
                "severity": "critical" if abs(d) >= critical * cfg["storage_vs_5yr_bcf"]
                else "warning",
                "message": f"Forecast {f:+.0f} Bcf vs 5-yr avg {b:+.0f} ({d:+.0f}, {side})",
            })
    moves = price_df[price_df["dod_pct"].abs() > cfg["henry_hub_dod_pct"]]
    for r in moves.itertuples():
        rows.append({
            "subject_date": r.price_date.isoformat(), "rule_name": "henry_hub_dod",
            "severity": "critical" if abs(r.dod_pct) >= critical * cfg["henry_hub_dod_pct"]
            else "warning",
            "message": f"Henry Hub spot {r.dod_pct:+.1f}% to ${r.price:.2f}/MMBtu",
        })
    return sorted(rows, key=lambda r: r["subject_date"], reverse=True)


def pipeline_health() -> dict:
    dq = read_sql(
        """SELECT max(checked_at) AS checked_at,
                  count(*) FILTER (WHERE status = 'pass') AS pass,
                  count(*) FILTER (WHERE status = 'warn') AS warn,
                  count(*) FILTER (WHERE status = 'fail') AS fail
           FROM dq_results
           WHERE run_id = (SELECT run_id FROM dq_results ORDER BY checked_at DESC LIMIT 1)"""
    ).iloc[0]
    fresh = read_sql(
        """SELECT (SELECT max(week_ending) FROM fact_storage_weekly) AS storage,
                  (SELECT max(price_date) FROM fact_gas_price_daily) AS price,
                  (SELECT max(obs_date) FROM fact_weather_daily) AS weather"""
    ).iloc[0]
    return {
        "dq_checked_at": dq["checked_at"].strftime("%Y-%m-%d %H:%M UTC")
        if dq["checked_at"] is not None else None,
        "dq": {"pass": int(dq["pass"]), "warn": int(dq["warn"]), "fail": int(dq["fail"])},
        "data_through": {k: fresh[k].isoformat() for k in ("storage", "price", "weather")},
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    storage = storage_history()
    forecasts = storage_forecasts()
    price_df = prices()
    mae = rolling_mae(forecasts)
    recent_prices = price_df[price_df["price_date"] >= date(2020, 1, 1)]
    backtest = json.loads((ROOT / "reports" / "backtest_summary.json").read_text())

    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "health": pipeline_health(),
        "latest": latest_live(),
        "backtest": {k: backtest[k] for k in ("storage", "direction", "storage_by_season")},
        "storage": _records(storage),
        "forecasts": _records(forecasts),
        "rolling_mae": _records(mae),
        "prices": _records(recent_prices),
        "alerts": alert_log(),
        "alert_replay": alert_replay(forecasts, recent_prices),
        "thresholds": settings()["alerts"],
    }
    (OUT / "dashboard.json").write_text(json.dumps(payload, separators=(",", ":"), default=str))
    storage.to_csv(OUT / "storage_weekly.csv", index=False)
    forecasts.to_csv(OUT / "storage_forecasts.csv", index=False)
    price_df.to_csv(OUT / "henry_hub_daily.csv", index=False)
    log.info("exported dashboard data: %d storage weeks, %d forecasts, %d prices -> %s",
             len(storage), len(forecasts), len(price_df), OUT)


if __name__ == "__main__":
    main()
