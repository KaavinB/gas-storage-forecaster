"""Walk-forward backtest.

Every Wednesday from TEST_START onward, each model is retrained on an expanding window of
rows whose labels were already published that day, then forecasts that week's report.

    python -m models.backtest              # run, store forecasts, write reports/backtest_results.md
"""

import json
from datetime import date

import numpy as np
import pandas as pd

from core.config import ROOT, settings
from core.db import connect
from core.log import get_logger
from ingestion.load import upsert
from models import metrics
from models.features import FEATURES, backtest_issue_dates, build_features, load_inputs
from models.registry import (
    direction_models,
    fit_predict_direction,
    fit_predict_storage,
    storage_models,
)

log = get_logger("backtest")

TEST_START = pd.Timestamp("2020-01-01")
MIN_TRAIN_ROWS = 150
REPORT_DIR = ROOT / "reports"


def label_known_dates(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """When each row's labels became public (the training-side guard against leakage)."""
    issue = pd.to_datetime(df["issue_date"])
    storage_known = issue + pd.Timedelta(days=1)  # report released the next day
    price_lag = pd.Timedelta(days=settings()["availability"]["price_lag_days"])
    price_known = issue + pd.Timedelta(days=7) + price_lag  # window end + publication lag
    return storage_known, price_known


def walk_forward(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=FEATURES).reset_index(drop=True)
    issue = pd.to_datetime(df["issue_date"])
    storage_known, price_known = label_known_dates(df)
    records = []

    for i in np.flatnonzero(issue >= TEST_START):
        test = df.iloc[[i]]
        today = issue.iloc[i]
        row = test.iloc[0]

        train = df[(storage_known <= today) & df["y_storage"].notna()]
        if len(train) >= MIN_TRAIN_ROWS and pd.notna(row["y_storage"]):
            base = dict(target="storage_change_bcf", run_date=row["issue_date"],
                        target_period=row["target_week"], actual_value=row["y_storage"],
                        baseline_value=row["baseline_storage"])
            records.append(base | {"model_name": "baseline_5yr",
                                   "predicted_value": row["baseline_storage"]})
            for name, model in storage_models().items():
                pred = fit_predict_storage(model, train, test)[0]
                records.append(base | {"model_name": name, "predicted_value": pred})

        train = df[(price_known <= today) & df["y_price_up"].notna()]
        if len(train) >= MIN_TRAIN_ROWS and pd.notna(row["y_price_up"]):
            base = dict(target="henry_hub_direction", run_date=row["issue_date"],
                        target_period=(today + pd.Timedelta(days=7)).date(),
                        actual_value=row["y_price_up"],
                        baseline_value=row["baseline_price_up"])
            majority = float(train["y_price_up"].mean() >= 0.5)
            records.append(base | {"model_name": "baseline_persistence",
                                   "predicted_value": row["baseline_price_up"]})
            records.append(base | {"model_name": "baseline_majority",
                                   "predicted_value": majority})
            for name, model in direction_models().items():
                prob = fit_predict_direction(model, train, test)[0]
                records.append(base | {"model_name": name, "predicted_value": prob})

        if len(records) and i % 52 == 0:
            log.info("walk-forward at %s", row["issue_date"])

    out = pd.DataFrame(records)
    is_dir = out["target"] == "henry_hub_direction"
    out["predicted_label"] = None
    out.loc[is_dir, "predicted_label"] = np.where(
        out.loc[is_dir, "predicted_value"] >= 0.5, "up", "down"
    )
    return out


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
def summarize(preds: pd.DataFrame) -> dict:
    storage = preds[preds["target"] == "storage_change_bcf"]
    direction = preds[preds["target"] == "henry_hub_direction"]
    summary = {"storage": [], "direction": [], "storage_by_season": [], "storage_by_year": []}

    base = storage[storage["model_name"] == "baseline_5yr"].set_index("run_date")
    base_mae = metrics.mae(base["actual_value"], base["predicted_value"])
    for name, g in storage.groupby("model_name", sort=False):
        g = g.set_index("run_date")
        model_mae = metrics.mae(g["actual_value"], g["predicted_value"])
        summary["storage"].append({
            "model": name,
            "n": len(g),
            "mae": model_mae,
            "rmse": metrics.rmse(g["actual_value"], g["predicted_value"]),
            "bias": float((g["predicted_value"] - g["actual_value"]).mean()),
            "mae_vs_baseline_pct": 100 * (model_mae / base_mae - 1),
            "dm_p_value": None if name == "baseline_5yr" else metrics.diebold_mariano(
                g["actual_value"], g["predicted_value"], base.loc[g.index, "predicted_value"]
            ),
        })

    storage = storage.assign(
        season=np.where(pd.to_datetime(storage["target_period"]).dt.month.isin([11, 12, 1, 2, 3]),
                        "withdrawal (Nov-Mar)", "injection (Apr-Oct)"),
        year=pd.to_datetime(storage["run_date"]).dt.year,
        abs_err=(storage["actual_value"] - storage["predicted_value"]).abs(),
    )
    for key, out in (("season", "storage_by_season"), ("year", "storage_by_year")):
        table = storage.pivot_table(index=key, columns="model_name", values="abs_err",
                                    aggfunc="mean", sort=False)
        summary[out] = table.reset_index().to_dict(orient="records")

    persistence = direction[direction["model_name"] == "baseline_persistence"]
    persistence_acc = float((persistence["predicted_value"] == persistence["actual_value"]).mean())
    for name, g in direction.groupby("model_name", sort=False):
        predicted_up = (g["predicted_value"] >= 0.5).astype(float)
        acc = metrics.directional_accuracy(g["actual_value"], predicted_up)
        vs_persistence = 100 * (acc["accuracy"] - persistence_acc)
        summary["direction"].append({"model": name, **acc, "vs_persistence_pp": vs_persistence})

    xgb = storage[storage["model_name"] == "xgboost"].nlargest(5, "abs_err")
    summary["largest_storage_misses"] = [
        {"week_ending": r.target_period, "actual": r.actual_value,
         "forecast": r.predicted_value, "baseline": r.baseline_value}
        for r in xgb.itertuples()
    ]
    return summary


def ridge_drivers(df: pd.DataFrame, top: int = 8) -> list[dict]:
    """Standardized ridge coefficients on all labelled weeks: Bcf of deviation from the
    5-yr average per one standard deviation of each feature."""
    train = df.dropna(subset=[*FEATURES, "y_storage"])
    model = storage_models()["ridge"]
    model.fit(train[FEATURES], train["y_storage"] - train["baseline_storage"])
    coefs = pd.Series(model[-1].coef_, index=FEATURES)
    ranked = coefs.reindex(coefs.abs().sort_values(ascending=False).index).head(top)
    return [{"feature": k, "bcf_per_std": float(v)} for k, v in ranked.items()]


def write_report(summary: dict, preds: pd.DataFrame, data_as_of: date) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / "backtest_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    first, last = preds["run_date"].min(), preds["run_date"].max()
    lines = [
        "# Walk-forward backtest results",
        "",
        f"Forecasts issued every Wednesday from **{first}** to **{last}**, each using only data "
        f"published by that day, models retrained weekly on an expanding window. "
        f"Data as of {data_as_of}. Generated by `make backtest`.",
        "",
        "## Weekly Lower-48 storage change (Bcf)",
        "",
        "| Model | Weeks | MAE | RMSE | Bias | MAE vs baseline | DM p-value |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary["storage"]:
        dm = "—" if r["dm_p_value"] is None else f"{r['dm_p_value']:.3f}"
        vs = "—" if r["model"] == "baseline_5yr" else f"{r['mae_vs_baseline_pct']:+.1f}%"
        lines.append(f"| {r['model']} | {r['n']} | {r['mae']:.1f} | {r['rmse']:.1f} | "
                     f"{r['bias']:+.1f} | {vs} | {dm} |")

    lines += ["", "MAE by season:", "", _table(summary["storage_by_season"], "season"),
              "", "MAE by year:", "", _table(summary["storage_by_year"], "year")]

    lines += ["", "Largest XGBoost misses:", "",
              "| Week ending | Actual | Forecast | 5-yr baseline |", "|---|---:|---:|---:|"]
    lines += [f"| {m['week_ending']} | {m['actual']:+.0f} | {m['forecast']:+.0f} | "
              f"{m['baseline']:+.0f} |" for m in summary["largest_storage_misses"]]
    lines += ["", "What drives the ridge forecast (standardized coefficients, all weeks; "
              "Bcf of deviation from the 5-yr average per 1 std of the feature):", "",
              "| Feature | Bcf per std |", "|---|---:|"]
    lines += [f"| `{d['feature']}` | {d['bcf_per_std']:+.1f} |" for d in summary["ridge_drivers"]]

    lines += [
        "",
        "## Henry Hub direction (next 7 days' average vs prior 7 days')",
        "",
        "| Model | Weeks | Accuracy | 95% CI | p vs coin flip | vs persistence |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in summary["direction"]:
        vs = "—" if r["model"] == "baseline_persistence" else f"{r['vs_persistence_pp']:+.1f} pp"
        lines.append(f"| {r['model']} | {r['n']} | {100 * r['accuracy']:.1f}% | "
                     f"{100 * r['ci_low']:.0f}–{100 * r['ci_high']:.0f}% | "
                     f"{r['p_vs_coin']:.3f} | {vs} |")
    lines += [
        "",
        "DM p-value: Diebold-Mariano test (Newey-West, 4 lags) that the model's absolute errors "
        "differ from the 5-year-average baseline's. p vs coin flip: two-sided binomial test "
        "against 50%.",
        "",
    ]
    (REPORT_DIR / "backtest_results.md").write_text("\n".join(lines))


def _table(records: list[dict], key: str) -> str:
    models = [k for k in records[0] if k != key]
    header = f"| {key} | " + " | ".join(models) + " |"
    sep = "|---|" + "---:|" * len(models)
    rows = [f"| {r[key]} | " + " | ".join(f"{r[m]:.1f}" for m in models) + " |" for r in records]
    return "\n".join([header, sep, *rows])


def store(preds: pd.DataFrame) -> int:
    df = preds.assign(is_backtest=True)
    with connect() as conn:
        return upsert(
            conn, "fact_forecast", df,
            key_cols=["run_date", "target", "target_period", "model_name", "is_backtest"],
            value_cols=["predicted_value", "predicted_label", "baseline_value", "actual_value"],
        )


def main() -> None:
    inputs = load_inputs()
    df = build_features(inputs, backtest_issue_dates(inputs))
    log.info("feature rows: %d (%s to %s)", len(df), df["issue_date"].min(), df["issue_date"].max())
    preds = walk_forward(df)
    log.info("stored %d backtest forecasts", store(preds))
    summary = summarize(preds)
    summary["ridge_drivers"] = ridge_drivers(df)
    write_report(summary, preds, data_as_of=inputs.storage.index.max().date())
    print((REPORT_DIR / "backtest_results.md").read_text())


if __name__ == "__main__":
    main()
