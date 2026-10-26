"""Model definitions shared by the backtest and the live forecast job.

Hyperparameters are fixed up front (not tuned on the backtest period) so the
walk-forward results are an honest estimate of live performance.
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, RidgeCV
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier, XGBRegressor

from models.features import FEATURES

_XGB_COMMON = dict(
    n_estimators=300,
    max_depth=3,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    random_state=0,
    n_jobs=1,  # faster than multi-threading on a few hundred rows
)


def storage_models() -> dict:
    """Regressors for the weekly storage change. They predict the deviation from the
    5-year average; the baseline is added back in `predict_storage`."""
    return {
        # alpha picked by chronological CV inside each training window
        "ridge": make_pipeline(
            StandardScaler(), RidgeCV(alphas=np.logspace(-2, 3, 30), cv=TimeSeriesSplit(5))
        ),
        "xgboost": XGBRegressor(objective="reg:absoluteerror", **_XGB_COMMON),
    }


def direction_models() -> dict:
    """Classifiers for P(Henry Hub averages higher next week)."""
    return {
        "ridge_logistic": make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000)),
        "xgboost": XGBClassifier(objective="binary:logistic", eval_metric="logloss", **_XGB_COMMON),
    }


def fit_predict_storage(model, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    model.fit(train[FEATURES], train["y_storage"] - train["baseline_storage"])
    return test["baseline_storage"].to_numpy() + model.predict(test[FEATURES])


def fit_predict_direction(model, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    model.fit(train[FEATURES], train["y_price_up"].astype(int))
    return model.predict_proba(test[FEATURES])[:, 1]
