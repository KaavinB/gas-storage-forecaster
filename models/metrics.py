"""Forecast evaluation metrics and significance tests."""

import numpy as np
from scipy import stats


def mae(actual, predicted) -> float:
    return float(np.mean(np.abs(np.asarray(actual) - np.asarray(predicted))))


def rmse(actual, predicted) -> float:
    return float(np.sqrt(np.mean((np.asarray(actual) - np.asarray(predicted)) ** 2)))


def diebold_mariano(actual, model_pred, baseline_pred, max_lag: int = 4) -> float:
    """Two-sided p-value that the model's absolute errors differ from the baseline's.

    Uses a Newey-West variance with `max_lag` lags because weekly forecast errors are
    autocorrelated (a cold month makes several consecutive weeks miss the same way)."""
    actual = np.asarray(actual)
    d = np.abs(actual - np.asarray(model_pred)) - np.abs(actual - np.asarray(baseline_pred))
    n = len(d)
    d_centered = d - d.mean()
    variance = np.sum(d_centered**2) / n
    for lag in range(1, max_lag + 1):
        weight = 1 - lag / (max_lag + 1)
        variance += 2 * weight * np.sum(d_centered[lag:] * d_centered[:-lag]) / n
    statistic = d.mean() / np.sqrt(variance / n)
    return float(2 * stats.norm.sf(abs(statistic)))


def directional_accuracy(actual_up, predicted_up) -> dict:
    """Hit rate, its 95% Wilson interval, and the p-value against a 50% coin flip."""
    hits = int(np.sum(np.asarray(actual_up) == np.asarray(predicted_up)))
    n = len(actual_up)
    test = stats.binomtest(hits, n, 0.5)
    ci = test.proportion_ci(confidence_level=0.95, method="wilson")
    return {"accuracy": hits / n, "ci_low": ci.low, "ci_high": ci.high,
            "p_vs_coin": test.pvalue, "n": n}
