from datetime import date

import pytest

from alerts import notify
from alerts import run as alert_run
from alerts.rules import henry_hub_day_over_day, storage_vs_five_year
from core.config import settings

pytestmark = pytest.mark.db
AS_OF = date(2000, 1, 12)  # synthetic rows live in 2000, before any real data


def _forecast(conn, predicted: float, baseline: float) -> None:
    conn.execute(
        """INSERT INTO fact_forecast (run_date, target, target_period, model_name,
                                      predicted_value, baseline_value, is_backtest)
           VALUES (%s, 'storage_change_bcf', '2000-01-07', %s, %s, %s, false)""",
        (AS_OF, settings()["forecast"]["storage_model"], predicted, baseline),
    )


@pytest.mark.parametrize(
    ("predicted", "baseline", "expected"),
    [(-100, -110, None),                       # 10 Bcf off: below threshold
     (-70, -110, ("warning", "looser")),       # +40 Bcf: smaller draw than normal
     (-170, -110, ("critical", "tighter"))],   # -60 Bcf >= 2x threshold
)
def test_storage_rule(db_conn, predicted, baseline, expected):
    _forecast(db_conn, predicted, baseline)
    alerts = storage_vs_five_year(db_conn, AS_OF)
    if expected is None:
        assert alerts == []
    else:
        (alert,) = alerts
        assert alert.severity == expected[0]
        assert alert.alert_key == f"storage_vs_5yr:2000-01-07:{expected[1]}"


def test_henry_hub_rule_flags_only_large_moves(db_conn):
    db_conn.execute(
        """INSERT INTO fact_gas_price_daily (price_date, hub, price_usd_mmbtu, series_id)
           VALUES ('2000-01-03', 'henry_hub', 2.00, 'TEST'),
                  ('2000-01-04', 'henry_hub', 2.10, 'TEST'),
                  ('2000-01-05', 'henry_hub', 2.625, 'TEST')"""
    )
    (alert,) = henry_hub_day_over_day(db_conn, AS_OF)  # +5% ignored, +25% flagged
    assert alert.alert_key == "henry_hub_dod:2000-01-05"
    assert alert.details["change_pct"] == pytest.approx(25.0)
    assert alert.severity == "critical"  # 25% >= 2 x 10%


def test_rerun_never_duplicates_or_resends(db_conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "email_configured", lambda: True)
    monkeypatch.setattr(notify, "send_email", lambda msg: sent.append(msg))
    _forecast(db_conn, -60, -110)
    alerts = storage_vs_five_year(db_conn, AS_OF)

    assert alert_run.record(db_conn, alerts, AS_OF) == 1
    assert alert_run.deliver(db_conn) == ("sent", 1)
    # same day re-run, and a later day while the condition persists
    assert alert_run.record(db_conn, alerts, AS_OF) == 0
    assert alert_run.record(db_conn, alerts, date(2000, 1, 13)) == 0
    assert alert_run.deliver(db_conn) == ("none", 0)
    assert len(sent) == 1


def test_failed_delivery_is_retried(db_conn, monkeypatch):
    monkeypatch.setattr(notify, "email_configured", lambda: True)

    def boom(msg):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(notify, "send_email", boom)
    _forecast(db_conn, -60, -110)
    alert_run.record(db_conn, storage_vs_five_year(db_conn, AS_OF), AS_OF)
    assert alert_run.deliver(db_conn) == ("failed", 1)

    monkeypatch.setattr(notify, "send_email", lambda msg: None)
    assert alert_run.deliver(db_conn) == ("sent", 1)


def test_email_escapes_html_and_summarises(monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "desk@example.com")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "bot@example.com")
    msg = notify.build_email([
        {"severity": "critical", "message": "Henry Hub <b>spike</b>"},
        {"severity": "warning", "message": "Storage forecast"},
    ])
    assert msg["Subject"].startswith("[Gas Storage Monitor] 2 critical alerts")
    html = msg.get_body(("html",)).get_content()
    assert "&lt;b&gt;spike&lt;/b&gt;" in html and "<b>spike</b>" not in html
