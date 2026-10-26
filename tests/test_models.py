import numpy as np
import pandas as pd
import pytest

from models import metrics
from models.backtest import label_known_dates


def test_mae_and_rmse():
    assert metrics.mae([1, 2, 3], [2, 2, 5]) == pytest.approx(1.0)
    assert metrics.rmse([0, 0], [3, 4]) == pytest.approx(np.sqrt(12.5))


def test_diebold_mariano_detects_clearly_better_model():
    rng = np.random.default_rng(0)
    actual = rng.normal(0, 30, 300)
    good = actual + rng.normal(0, 5, 300)
    bad = actual + rng.normal(0, 30, 300)
    assert metrics.diebold_mariano(actual, good, bad) < 0.001
    # equally good forecasts -> no evidence of a difference
    noise = rng.normal(0, 10, 300)
    assert metrics.diebold_mariano(actual, actual + noise, actual - noise) > 0.5


def test_directional_accuracy_reports_coin_flip_p_value():
    result = metrics.directional_accuracy([1, 0] * 50, [1, 0] * 50)
    assert result["accuracy"] == 1.0 and result["p_vs_coin"] < 1e-20
    coin = metrics.directional_accuracy([1, 0] * 50, [1, 1] * 50)
    assert coin["accuracy"] == 0.5 and coin["p_vs_coin"] == pytest.approx(1.0)


def test_price_labels_are_embargoed_until_published():
    """A Wednesday's price label needs the next 7 days of prices, which EIA publishes
    up to 7 days later; it must not be used for training before then."""
    df = pd.DataFrame({"issue_date": [pd.Timestamp("2026-01-07").date()]})
    storage_known, price_known = label_known_dates(df)
    assert storage_known.iloc[0] == pd.Timestamp("2026-01-08")  # Thursday release
    assert price_known.iloc[0] == pd.Timestamp("2026-01-21")    # +7 window +7 lag
