"""Paste into a JoinQuant strategy backtest to export weekly A-share breadth."""

from datetime import timedelta
import csv
import io
import re
import time

from jqdata import *
import numpy as np
import pandas as pd


BENCHMARK = "000300.XSHG"
OUTPUT = "a_share_stock_breadth_weekly.csv"
BATCH_SIZE = 250
A_SHARE = re.compile(r"^(?:6\d{5}\.XSHG|[03]\d{5}\.XSHE|[489]\d{5}\.XBSE)$")
FIELDS = (
    "week_ending", "trade_date", "reference_date", "listed_count",
    "valid_count", "up_count", "coverage", "up_ratio", "hs300_close",
)


def initialize(context):
    set_option("avoid_future_data", True)
    write_row(FIELDS, append=False)
    run_daily(observe, time="after_close", reference_security=BENCHMARK)


def write_row(row, append=True):
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(row)
    write_file(OUTPUT, buffer.getvalue(), append=append)


def week_ending(day):
    return day + timedelta(days=4 - day.weekday())


def market_counts(codes, reference, today):
    valid_count = up_count = 0
    endpoints = pd.DatetimeIndex([reference, today])
    for offset in range(0, len(codes), BATCH_SIZE):
        batch = codes[offset:offset + BATCH_SIZE]
        bars = get_price(
            batch, start_date=reference, end_date=today, frequency="daily",
            fields=["close", "paused"], fq="post", skip_paused=False, panel=False,
        )
        if bars is None or bars.empty:
            continue
        bars = bars.copy()
        if "time" not in bars.columns:
            bars = bars.rename_axis("time").reset_index()
        if "code" not in bars.columns and len(batch) == 1:
            bars["code"] = batch[0]
        bars["time"] = pd.to_datetime(bars["time"]).dt.normalize()
        bars = bars.loc[bars["time"].isin(endpoints) & bars["paused"].eq(0)]
        closes = bars.pivot(index="time", columns="code", values="close").reindex(endpoints)
        valid = (np.isfinite(closes) & closes.gt(0)).all(axis=0)
        valid_count += int(valid.sum())
        up_count += int((closes.iloc[1].gt(closes.iloc[0]) & valid).sum())
    return valid_count, up_count


def observe(context):
    today = context.current_dt.date()
    friday = week_ending(today)
    days_this_week = get_trade_days(start_date=friday - timedelta(days=4), end_date=friday)
    if not len(days_this_week) or pd.Timestamp(days_this_week[-1]).date() != today:
        return

    days = get_trade_days(start_date=today - timedelta(days=90), end_date=today)
    weekly = {week_ending(day.date()): day.date() for day in pd.DatetimeIndex(days)}
    if len(weekly) < 5:
        return
    reference = weekly[sorted(weekly)[-5]]

    started = time.monotonic()
    securities = get_all_securities(["stock"], date=today)
    listed_at = pd.to_datetime(securities["start_date"])
    codes = sorted(
        code for code in listed_at[listed_at.le(pd.Timestamp(reference))].index
        if A_SHARE.fullmatch(code)
    )
    valid, up = market_counts(codes, reference, today)
    benchmark = get_price(BENCHMARK, end_date=today, count=1, fields=["close"], fq=None)
    if benchmark is None or benchmark.empty or pd.Timestamp(benchmark.index[-1]).date() != today:
        raise ValueError("Missing CSI 300 close for %s" % today)
    close = float(benchmark["close"].iloc[-1])
    if not np.isfinite(close) or close <= 0:
        raise ValueError("Invalid CSI 300 close for %s" % today)
    coverage = valid / len(codes) if codes else 0
    write_row((friday, today, reference, len(codes), valid, up, coverage, up / valid if valid else "", close))
    log.info("%s coverage %.1f%% (%s/%s), %.1fs" % (
        today, 100 * coverage, valid, len(codes), time.monotonic() - started,
    ))
