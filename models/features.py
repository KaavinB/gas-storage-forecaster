"""Point-in-time feature builder.

For an issue date D, every feature uses only data published by D:
    storage  release_date <= D
    weather  obs_date + weather_lag_days <= D   (unobserved days fall back to 10-year normals)
    price    price_date + price_lag_days <= D
Targets look forward by design and are only used for training and evaluation.

The storage target is the next unreleased EIA report. The price target is whether Henry Hub
averages higher in the 7 days after D than in the 7 days up to D.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from core.config import settings
from core.db import read_sql

WEEK = pd.Timedelta(days=7)

FEATURES = [
    # storage: seasonal baseline + recent behaviour
    "avg_change_5yr",       # 5-yr average change for the target week (the naive forecast)
    "chg_lag1",             # last reported weekly change
    "chg_lag2",
    "dev_5yr_lag1",         # last reported change minus its 5-yr average
    "dev_5yr_mean4",        # same, averaged over the last 4 reports (persistent imbalance)
    "level_vs_5yr",         # storage surplus (+) / deficit (-) vs 5-yr average, Bcf
    # weather for the target week and the last reported week
    "tgt_hdd", "tgt_cdd", "tgt_hdd_anom", "tgt_cdd_anom",
    "prev_hdd", "prev_cdd", "prev_hdd_anom", "prev_cdd_anom",
    "hdd_anom_change", "cdd_anom_change",
    # price (as of the last published day)
    "price_last", "ret_5d", "ret_20d", "vol_20d", "price_vs_ma60",
    # calendar
    "doy_sin", "doy_cos", "is_withdrawal_season", "is_holiday_week",
]


@dataclass
class Inputs:
    storage: pd.DataFrame    # index week_ending: release_date, working_gas_bcf, net_change_bcf
    five_year: pd.DataFrame  # index week_ending, incl. future weeks: avg_level/avg_change_5yr
    weather: pd.DataFrame    # index obs_date: hdd (gas-home weighted), cdd (population weighted)
    prices: pd.Series        # index price_date: Henry Hub spot $/MMBtu


def load_inputs() -> Inputs:
    storage = read_sql(
        "SELECT week_ending, release_date, working_gas_bcf, net_change_bcf "
        "FROM v_storage_weekly WHERE region = 'lower48' ORDER BY week_ending"
    )
    five_year = read_sql(
        "SELECT week_ending, avg_level_5yr, avg_change_5yr FROM v_storage_5yr_calendar"
    )
    weather = read_sql(
        """SELECT obs_date,
                  max(value) FILTER (WHERE metric = 'hdd' AND weighting = 'utility_gas') AS hdd,
                  max(value) FILTER (WHERE metric = 'cdd' AND weighting = 'population')  AS cdd
           FROM fact_weather_daily WHERE region = 'CONUS'
           GROUP BY obs_date ORDER BY obs_date"""
    )
    prices = read_sql(
        "SELECT price_date, price_usd_mmbtu FROM fact_gas_price_daily "
        "WHERE hub = 'henry_hub' ORDER BY price_date"
    )
    return Inputs(
        storage=_indexed(storage, "week_ending", ["release_date"]),
        five_year=_indexed(five_year, "week_ending"),
        weather=_indexed(weather, "obs_date"),
        prices=_indexed(prices, "price_date")["price_usd_mmbtu"],
    )


def _indexed(df: pd.DataFrame, index_col: str, date_cols: Iterable[str] = ()) -> pd.DataFrame:
    df = df.copy()
    df[index_col] = pd.to_datetime(df[index_col])
    for col in date_cols:
        df[col] = pd.to_datetime(df[col])
    df = df.set_index(index_col)
    numeric = [c for c in df.columns if c not in date_cols]
    df[numeric] = df[numeric].astype(float)  # Postgres numeric -> Decimal -> float
    return df


# ---------------------------------------------------------------------------
# Weather normals
# ---------------------------------------------------------------------------
def _month_day(index: pd.DatetimeIndex) -> pd.Index:
    return index.strftime("%m-%d").str.replace("02-29", "02-28")


def daily_normals(weather: pd.DataFrame, years: int) -> dict[str, pd.DataFrame]:
    """year x month-day tables where the normal for year Y averages years Y-n .. Y-1.
    Prior years only, so a normal is known before its year starts."""
    df = weather.assign(year=weather.index.year, md=_month_day(weather.index))
    normals = {}
    for col in weather.columns:
        table = df.pivot_table(index="year", columns="md", values=col, aggfunc="mean")
        table = table.reindex(range(table.index.min(), table.index.max() + 2))  # + next year
        normals[col] = table.shift(1).rolling(years, min_periods=years // 2).mean()
    return normals


# ---------------------------------------------------------------------------
# Feature construction
# ---------------------------------------------------------------------------
def build_features(inputs: Inputs, issue_dates: Iterable[date | pd.Timestamp]) -> pd.DataFrame:
    cfg = settings()
    ctx = _Context(
        inputs=inputs,
        normals=daily_normals(inputs.weather, cfg["features"]["weather_normal_years"]),
        weather_lag=pd.Timedelta(days=cfg["availability"]["weather_lag_days"]),
        price_lag=pd.Timedelta(days=cfg["availability"]["price_lag_days"]),
    )
    rows = [ctx.row(pd.Timestamp(d)) for d in issue_dates]
    return pd.DataFrame([r for r in rows if r is not None]).reset_index(drop=True)


def backtest_issue_dates(inputs: Inputs) -> list[pd.Timestamp]:
    """The day before each report's release: when desk analysts submit their estimates."""
    return list(inputs.storage["release_date"] - pd.Timedelta(days=1))


@dataclass
class _Context:
    inputs: Inputs
    normals: dict[str, pd.DataFrame]
    weather_lag: pd.Timedelta
    price_lag: pd.Timedelta

    def row(self, issue: pd.Timestamp) -> dict | None:
        s = self.inputs.storage
        known = s[s["release_date"] <= issue]
        prices = self.inputs.prices[self.inputs.prices.index + self.price_lag <= issue]
        if len(known) < 5 or len(prices) < 61:
            return None

        last_week = known.index[-1]
        target_week = last_week + WEEK
        five = self.inputs.five_year
        if target_week not in five.index or pd.isna(five.at[target_week, "avg_change_5yr"]):
            return None

        row = {"issue_date": issue.date(), "target_week": target_week.date()}
        row.update(self._storage(known, target_week))
        row.update(self._weather(issue, target_week, last_week))
        row.update(self._price(prices))
        row.update(self._calendar(target_week))
        row.update(self._targets(issue, target_week, prices))
        return row

    def _storage(self, known: pd.DataFrame, target_week: pd.Timestamp) -> dict:
        five = self.inputs.five_year
        chg = known["net_change_bcf"]
        dev = chg - five["avg_change_5yr"].reindex(chg.index)
        last_week = known.index[-1]
        return {
            "avg_change_5yr": five.at[target_week, "avg_change_5yr"],
            "chg_lag1": chg.iloc[-1],
            "chg_lag2": chg.iloc[-2],
            "dev_5yr_lag1": dev.iloc[-1],
            "dev_5yr_mean4": dev.iloc[-4:].mean(),
            "level_vs_5yr": known["working_gas_bcf"].iloc[-1] - five.at[last_week, "avg_level_5yr"],
        }

    def _week_weather(self, issue: pd.Timestamp, week_ending: pd.Timestamp) -> dict:
        days = pd.date_range(week_ending - pd.Timedelta(days=6), week_ending)
        observed = self.inputs.weather.reindex(days)
        published = days + self.weather_lag <= issue
        out = {"days_observed": int((published & observed["hdd"].notna()).sum())}
        keys = list(zip(days.year, _month_day(days), strict=True))
        for col in ("hdd", "cdd"):
            normal = np.array([self.normals[col].at[year, md] for year, md in keys])
            actual = observed[col].to_numpy()
            use_actual = published & ~np.isnan(actual)
            value = np.where(use_actual, actual, normal)
            out[col] = value.sum()
            out[f"{col}_anom"] = value.sum() - normal.sum()
        return out

    def _weather(self, issue, target_week, last_week) -> dict:
        tgt = self._week_weather(issue, target_week)
        prev = self._week_weather(issue, last_week)
        return {
            "tgt_hdd": tgt["hdd"], "tgt_cdd": tgt["cdd"],
            "tgt_hdd_anom": tgt["hdd_anom"], "tgt_cdd_anom": tgt["cdd_anom"],
            "prev_hdd": prev["hdd"], "prev_cdd": prev["cdd"],
            "prev_hdd_anom": prev["hdd_anom"], "prev_cdd_anom": prev["cdd_anom"],
            "hdd_anom_change": tgt["hdd_anom"] - prev["hdd_anom"],
            "cdd_anom_change": tgt["cdd_anom"] - prev["cdd_anom"],
            "tgt_weather_days_observed": tgt["days_observed"],
        }

    @staticmethod
    def _price(prices: pd.Series) -> dict:
        log_p = np.log(prices)
        return {
            "price_last": prices.iloc[-1],
            "price_last_date": prices.index[-1].date(),
            "ret_5d": log_p.iloc[-1] - log_p.iloc[-6],
            "ret_20d": log_p.iloc[-1] - log_p.iloc[-21],
            "vol_20d": log_p.diff().iloc[-20:].std() * np.sqrt(252),
            "price_vs_ma60": prices.iloc[-1] / prices.iloc[-60:].mean() - 1,
        }

    @staticmethod
    def _calendar(target_week: pd.Timestamp) -> dict:
        angle = 2 * np.pi * target_week.day_of_year / 365.25
        return {
            "doy_sin": np.sin(angle),
            "doy_cos": np.cos(angle),
            "is_withdrawal_season": int(target_week.month in (11, 12, 1, 2, 3)),
            "is_holiday_week": int(_contains_major_holiday(target_week)),
        }

    def _targets(self, issue, target_week, available_prices) -> dict:
        s, five, p = self.inputs.storage, self.inputs.five_year, self.inputs.prices
        y_storage = s.at[target_week, "net_change_bcf"] if target_week in s.index else np.nan

        def window_mean(series, start, end):  # mean over (start, end]
            w = series[(series.index > start) & (series.index <= end)]
            return w.mean() if len(w) else np.nan

        after = window_mean(p, issue, issue + WEEK)
        before = window_mean(p, issue - WEEK, issue)
        complete = p.index.max() >= issue + WEEK
        y_price_up = float(after > before) if complete and not np.isnan(after + before) else np.nan

        # Naive direction baseline: last fully published week-over-week move persists.
        last = available_prices.index[-1]
        recent = window_mean(available_prices, last - WEEK, last)
        prior = window_mean(available_prices, last - 2 * WEEK, last - WEEK)
        return {
            "y_storage": y_storage,
            "baseline_storage": five.at[target_week, "avg_change_5yr"],
            "y_price_up": y_price_up,
            "baseline_price_up": float(recent > prior),
        }


def _contains_major_holiday(week_ending: pd.Timestamp) -> bool:
    """Thanksgiving, Christmas or New Year's Day inside the Saturday-Friday storage week.
    Industrial demand drops on these weeks, which skews the weekly change."""
    start = week_ending - pd.Timedelta(days=6)
    holidays = []
    for year in {start.year, week_ending.year}:
        nov1 = pd.Timestamp(year, 11, 1)
        thanksgiving = nov1 + pd.Timedelta(days=(3 - nov1.day_of_week) % 7 + 21)
        holidays += [thanksgiving, pd.Timestamp(year, 12, 25), pd.Timestamp(year, 1, 1)]
    return any(start <= h <= week_ending for h in holidays)
