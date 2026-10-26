"""NOAA Climate Prediction Center weighted daily degree days.

Files live at {base_url}/{year}/{Weighting}.{Heating|Cooling}.txt and look like:

    Product: Daily Heating Degree Days
    Regions: CPC::Regions::CensusDivisions
    Weights: Population
    Region|20250101|20250102|...
    1|26|28|...
    CONUS|24|25|...
"""

import pandas as pd

from core.config import settings
from ingestion import http


def fetch_year(year: int, filename: str) -> str:
    return http.get(f"{settings()['sources']['cpc']['base_url']}/{year}/{filename}").text


def parse(text: str, region: str = "CONUS") -> pd.DataFrame:
    """Return the `region` row of a CPC file as columns [obs_date, value]."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    header = next((ln for ln in lines if ln.startswith("Region|")), None)
    if header is None:
        raise ValueError("CPC file has no 'Region|' header row")
    row = next((ln for ln in lines if ln.split("|", 1)[0] == region), None)
    if row is None:
        raise ValueError(f"CPC file has no row for region {region!r}")

    dates = header.split("|")[1:]
    values = row.split("|")[1:]
    if len(dates) != len(values):
        raise ValueError(f"CPC header has {len(dates)} dates but {region} has {len(values)} values")

    df = pd.DataFrame({"obs_date": pd.to_datetime(dates, format="%Y%m%d").date, "value": values})
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    # CPC uses large negative sentinels for missing data; degree days can't be negative.
    return df[df["value"].between(0, 200)].reset_index(drop=True)
