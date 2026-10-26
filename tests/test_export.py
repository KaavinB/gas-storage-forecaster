import json

import pytest

from dashboards import export

pytestmark = pytest.mark.db


def test_export_writes_dashboard_json_and_csvs(tmp_path, monkeypatch):
    """Runs against whatever is in the database, including CI's (which has no forecasts)."""
    try:
        export.read_sql("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Postgres not available ({exc})")
    monkeypatch.setattr(export, "OUT", tmp_path)
    export.main()
    payload = json.loads((tmp_path / "dashboard.json").read_text())
    assert {"health", "storage", "forecasts", "prices", "alerts", "backtest"} <= payload.keys()
    assert payload["health"]["dq"].keys() == {"pass", "warn", "fail"}
    for name in ("storage_weekly.csv", "storage_forecasts.csv", "henry_hub_daily.csv"):
        assert (tmp_path / name).stat().st_size > 0
