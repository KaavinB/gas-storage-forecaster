from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.config import settings
from models import features as F


@pytest.fixture(scope="module")
def inputs():
    try:
        inp = F.load_inputs()
    except Exception as exc:  # no database, or not yet ingested
        pytest.skip(f"needs ingested data ({exc})")
    if len(inp.storage) < 300:
        pytest.skip("needs ingested data (run `make ingest-full`)")
    return inp


def _corrupt_unpublished(inp: F.Inputs, issue: pd.Timestamp) -> F.Inputs:
    """Replace every value that was NOT yet published at `issue` with garbage."""
    lag = settings()["availability"]
    storage = inp.storage.copy()
    future = storage["release_date"] > issue
    storage.loc[future, ["working_gas_bcf", "net_change_bcf"]] = 9_999.0

    weather = inp.weather.copy()
    weather.loc[weather.index + pd.Timedelta(days=lag["weather_lag_days"]) > issue] = 77.0

    prices = inp.prices.copy()
    prices[prices.index + pd.Timedelta(days=lag["price_lag_days"]) > issue] = 999.0
    return F.Inputs(storage=storage, five_year=inp.five_year, weather=weather, prices=prices)


@pytest.mark.parametrize("issue", ["2019-01-09", "2021-02-17", "2023-07-12", "2025-12-31"])
def test_features_never_use_unpublished_data(inputs, issue):
    """The core look-ahead test: scrambling all data published after the issue date
    must not change a single feature value."""
    issue = pd.Timestamp(issue)
    clean = F.build_features(inputs, [issue])
    scrambled = F.build_features(_corrupt_unpublished(inputs, issue), [issue])
    pd.testing.assert_frame_equal(clean[F.FEATURES], scrambled[F.FEATURES])
    # ...while the targets (which look forward by design) do change
    assert not clean["y_storage"].equals(scrambled["y_storage"])


def test_backtest_target_is_the_report_released_the_next_day(inputs):
    df = F.build_features(inputs, F.backtest_issue_dates(inputs))
    released = inputs.storage["release_date"].reindex(pd.to_datetime(df["target_week"]))
    assert (released.to_numpy() - pd.to_datetime(df["issue_date"]).to_numpy()
            == np.timedelta64(1, "D")).all()
    # the Wednesday before release, all 7 days of target-week weather are published
    assert (df["tgt_weather_days_observed"] == 7).all()


def test_live_forecast_targets_next_unreleased_week(inputs):
    df = F.build_features(inputs, [pd.Timestamp(date.today())])
    last_released = inputs.storage.index[inputs.storage["release_date"] <= pd.Timestamp.today()][-1]
    assert pd.Timestamp(df.loc[0, "target_week"]) == last_released + pd.Timedelta(days=7)


def test_daily_normals_use_only_prior_years():
    days = pd.date_range("2000-01-01", "2012-12-31")
    weather = pd.DataFrame({"hdd": days.year - 2000.0, "cdd": 0.0}, index=days)
    normals = F.daily_normals(weather, years=10)
    # normal for 2012 = mean of 2002..2011 values (2..11) = 6.5; 2012 itself is excluded
    assert normals["hdd"].at[2012, "07-04"] == pytest.approx(6.5)
    # next year's normal exists already (needed for live forecasts in early January)
    assert normals["hdd"].at[2013, "07-04"] == pytest.approx(7.5)


@pytest.mark.parametrize(
    ("week_ending", "expected"),
    [("2025-11-28", True),   # Thanksgiving Thu 2025-11-27
     ("2025-12-26", True),   # Christmas
     ("2026-01-02", True),   # New Year's Day
     ("2025-12-05", False)],
)
def test_holiday_week_flag(week_ending, expected):
    assert F._contains_major_holiday(pd.Timestamp(week_ending)) is expected
