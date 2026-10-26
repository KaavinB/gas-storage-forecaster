"""Alert rules. Each rule reads the database and returns candidate alerts.

Every alert carries a deterministic `alert_key` (rule + subject) so re-running the job on the
same day, or on later days while the condition persists, never produces a duplicate.
"""

from dataclasses import dataclass, field
from datetime import date

import psycopg

from core.config import settings


@dataclass(frozen=True)
class Alert:
    alert_key: str
    rule_name: str
    subject_date: date
    severity: str  # 'warning' | 'critical'
    message: str
    details: dict = field(default_factory=dict)


def _severity(magnitude: float, threshold: float) -> str:
    critical_at = threshold * settings()["alerts"]["critical_multiple"]
    return "critical" if magnitude >= critical_at else "warning"


def storage_vs_five_year(conn: psycopg.Connection, as_of: date) -> list[Alert]:
    """Forecast weekly change differs from the 5-yr average change by more than X Bcf.
    Above the average = looser than normal (bearish for price); below = tighter (bullish)."""
    threshold = settings()["alerts"]["storage_vs_5yr_bcf"]
    model = settings()["forecast"]["storage_model"]
    row = conn.execute(
        """SELECT target_period, predicted_value, baseline_value FROM fact_forecast
           WHERE NOT is_backtest AND target = 'storage_change_bcf'
             AND model_name = %s AND run_date = %s""",
        (model, as_of),
    ).fetchone()
    if row is None:
        return []
    week, forecast, baseline = row[0], float(row[1]), float(row[2])
    deviation = forecast - baseline
    if abs(deviation) <= threshold:
        return []
    side = "looser" if deviation > 0 else "tighter"
    return [Alert(
        alert_key=f"storage_vs_5yr:{week}:{side}",
        rule_name="storage_vs_5yr",
        subject_date=week,
        severity=_severity(abs(deviation), threshold),
        message=(f"Storage forecast for week ending {week}: {forecast:+.0f} Bcf vs 5-yr average "
                 f"{baseline:+.0f} Bcf ({deviation:+.0f} Bcf, {side} than normal, "
                 f"{'bearish' if side == 'looser' else 'bullish'} for price)"),
        details={"model": model, "forecast_bcf": round(forecast, 1),
                 "five_year_avg_bcf": round(baseline, 1), "deviation_bcf": round(deviation, 1),
                 "threshold_bcf": threshold},
    )]


def henry_hub_day_over_day(conn: psycopg.Connection, as_of: date) -> list[Alert]:
    """Henry Hub spot moved more than Y% from the previous trading day. EIA publishes prices
    in weekly batches, so every day in the lookback window is checked on each run."""
    cfg = settings()["alerts"]
    threshold = cfg["henry_hub_dod_pct"]
    rows = conn.execute(
        """SELECT price_date, price, prev_date, prev_price FROM (
               SELECT price_date, price_usd_mmbtu AS price,
                      lag(price_date)      OVER (ORDER BY price_date) AS prev_date,
                      lag(price_usd_mmbtu) OVER (ORDER BY price_date) AS prev_price
               FROM fact_gas_price_daily WHERE hub = 'henry_hub') p
           WHERE price_date > %s::date - %s AND price_date <= %s AND prev_price IS NOT NULL
           ORDER BY price_date""",
        (as_of, cfg["price_lookback_days"], as_of),
    ).fetchall()
    alerts = []
    for price_date, price, prev_date, prev_price in rows:
        change_pct = 100 * (float(price) / float(prev_price) - 1)
        if abs(change_pct) <= threshold:
            continue
        alerts.append(Alert(
            alert_key=f"henry_hub_dod:{price_date}",
            rule_name="henry_hub_dod",
            subject_date=price_date,
            severity=_severity(abs(change_pct), threshold),
            message=(f"Henry Hub spot {change_pct:+.1f}% on {price_date}: "
                     f"${float(prev_price):.2f} -> ${float(price):.2f}/MMBtu"),
            details={"price": float(price), "prev_price": float(prev_price),
                     "prev_date": str(prev_date), "change_pct": round(change_pct, 2),
                     "threshold_pct": threshold},
        ))
    return alerts


RULES = [storage_vs_five_year, henry_hub_day_over_day]


def evaluate(conn: psycopg.Connection, as_of: date) -> list[Alert]:
    return [alert for rule in RULES for alert in rule(conn, as_of)]
