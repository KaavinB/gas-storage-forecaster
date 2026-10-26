"""EIA Open Data API v2 client: https://www.eia.gov/opendata/documentation.php"""

import pandas as pd

from core.config import env, settings
from ingestion import http

PAGE_SIZE = 5000  # API maximum rows per request

# series_id -> (API route, frequency). Verified against the live API on 2026-10-26.
SERIES_ROUTES = {
    "NW2_EPG0_SWO_R48_BCF": ("natural-gas/stor/wkly", "weekly"),
    "RNGWHHD": ("natural-gas/pri/fut", "daily"),
}


def fetch_series(series_id: str, start: str) -> pd.DataFrame:
    """Return all observations of `series_id` on or after `start` as columns [period, value]."""
    route, frequency = SERIES_ROUTES[series_id]
    url = f"{settings()['sources']['eia']['base_url']}/{route}/data/"
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "api_key": env("EIA_API_KEY"),
            "frequency": frequency,
            "data[0]": "value",
            "facets[series][]": series_id,
            "start": start,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": PAGE_SIZE,
        }
        payload = http.get(url, params).json()["response"]
        page = payload["data"]
        rows.extend(page)
        offset += len(page)
        if not page or offset >= int(payload["total"]):
            break
    return parse_rows(rows)


def parse_rows(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["period", "value"])
    df["period"] = pd.to_datetime(df["period"]).dt.date
    # EIA sends values as strings; non-numeric placeholders become NaN and are dropped.
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["value"]).drop_duplicates("period").reset_index(drop=True)
