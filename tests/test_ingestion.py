from datetime import date
from unittest.mock import MagicMock

import pytest
import requests
from tenacity import wait_none

from ingestion import cpc, eia, http

CPC_SAMPLE = """Product: Daily Heating Degree Days
Regions: CPC::Regions::CensusDivisions
Weights: Population
Region|20250101|20250102|20250103
1|26|28|30
CONUS|24|25|-9999
"""


def test_cpc_parse_extracts_conus_and_drops_missing_sentinel():
    df = cpc.parse(CPC_SAMPLE)
    assert list(df["obs_date"]) == [date(2025, 1, 1), date(2025, 1, 2)]
    assert list(df["value"]) == [24, 25]


def test_cpc_parse_rejects_misaligned_rows():
    bad = CPC_SAMPLE.replace("CONUS|24|25|-9999", "CONUS|24|25")
    with pytest.raises(ValueError, match="3 dates but CONUS has 2"):
        cpc.parse(bad)


def test_cpc_parse_requires_region_row():
    with pytest.raises(ValueError, match="no row for region"):
        cpc.parse(CPC_SAMPLE, region="PACIFIC")


def test_eia_parse_rows_coerces_strings_and_drops_blanks_and_dupes():
    rows = [
        {"period": "2026-10-16", "value": "3644"},
        {"period": "2026-10-16", "value": "3644"},
        {"period": "2026-10-09", "value": None},
    ]
    df = eia.parse_rows(rows)
    assert len(df) == 1
    assert df.loc[0, "period"] == date(2026, 10, 16)
    assert df.loc[0, "value"] == 3644.0


def test_eia_fetch_series_follows_pagination(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test")
    monkeypatch.setattr(eia, "PAGE_SIZE", 2)
    pages = [
        {"total": "3", "data": [{"period": "2026-01-01", "value": "1"},
                                {"period": "2026-01-02", "value": "2"}]},
        {"total": "3", "data": [{"period": "2026-01-03", "value": "3"}]},
    ]
    calls = []

    def fake_get(url, params):
        calls.append(params["offset"])
        resp = MagicMock()
        resp.json.return_value = {"response": pages[len(calls) - 1]}
        return resp

    monkeypatch.setattr(eia.http, "get", fake_get)
    df = eia.fetch_series("RNGWHHD", "2026-01-01")
    assert calls == [0, 2]
    assert list(df["value"]) == [1.0, 2.0, 3.0]


def test_http_get_retries_transient_errors_then_succeeds(monkeypatch):
    ok = MagicMock(status_code=200)
    responses = iter([requests.ConnectionError("reset"), ok])

    def fake_requests_get(*args, **kwargs):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(http.requests, "get", fake_requests_get)
    assert http.get.retry_with(wait=wait_none())("https://example.org") is ok


def test_http_get_does_not_retry_client_errors(monkeypatch):
    calls = 0

    def fake_requests_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        resp = requests.Response()
        resp.status_code = 403
        return resp

    monkeypatch.setattr(http.requests, "get", fake_requests_get)
    with pytest.raises(requests.HTTPError):
        http.get.retry_with(wait=wait_none())("https://example.org")
    assert calls == 1
