"""聚宽策略回测脚本。收盘采样，次日开盘起计算后续收益，不下单。"""

from jqdata import *
import numpy as np
import pandas as pd


ETF_CODES = [
    "512880.XSHG", "512800.XSHG", "512400.XSHG", "512200.XSHG",
    "515880.XSHG", "515220.XSHG", "159992.XSHE", "159611.XSHE",
]
SAMPLE_EVERY = 20
HORIZONS = (20, 60)
MIN_COVERAGE = 0.80
MAX_MOMENTUM_GAP = 0.10
FEATURE_FILE = "etf_breadth_features.csv"
RETURN_FILE = "etf_breadth_pairs.csv"


def initialize(context):
    set_option("avoid_future_data", True)
    g.day = -1
    g.pending = []
    g.features = []
    g.results = []
    run_daily(observe, time="after_close")


def tracked_indices(today):
    target = finance.FUND_INVEST_TARGET
    mapping = finance.run_query(query(target).filter(
        target.code.in_(ETF_CODES), target.pub_date <= today
    ))
    if mapping is None or mapping.empty:
        return {}
    date = pd.Timestamp(today)
    for field in ("pub_date", "start_date", "end_date"):
        mapping[field] = pd.to_datetime(mapping[field])
    active = mapping[(mapping.pub_date <= date) & (mapping.start_date <= date)
                     & (mapping.end_date.isna() | (mapping.end_date > date))]
    current = active.sort_values("pub_date").drop_duplicates("code", keep="last")
    return {row.code: str(row.traced_index_code).strip() for row in current.itertuples()
            if pd.notna(row.traced_index_code) and str(row.traced_index_code).strip()}


def quote_panel(codes, today, count, field, require_trading=False):
    """用统一交易日对齐行情，保留缺值，避免把缺失行当成更短的窗口。"""
    dates = pd.DatetimeIndex(get_trade_days(end_date=today, count=count)).normalize()
    frames = []
    codes = sorted(codes)
    for offset in range(0, len(codes), 100):
        batch = codes[offset:offset + 100]
        bars = get_price(batch, end_date=today, count=count, frequency="daily",
                         fields=[field, "paused"], fq="post", skip_paused=False, panel=False)
        if bars is None or bars.empty:
            continue
        bars = bars.copy()
        if "time" not in bars.columns:
            bars.index.name = "time"
            bars = bars.reset_index()
        if "code" not in bars.columns and len(batch) == 1:
            bars["code"] = batch[0]
        bars["time"] = pd.to_datetime(bars["time"]).dt.normalize()
        if require_trading:
            bars[field] = bars[field].where(bars.paused.eq(0))
        frames.append(bars.pivot(index="time", columns="code", values=field))
    panel = pd.concat(frames, axis=1) if frames else pd.DataFrame(dtype=float)
    panel = panel.reindex(index=dates, columns=codes)
    panel = panel.astype(float)
    return panel.where(np.isfinite(panel) & panel.gt(0))


def snapshot(today):
    indices = tracked_indices(today)
    members = {}
    for index in sorted(set(indices.values())):
        try:
            members[index] = sorted(set(get_index_stocks(index, date=today) or []))
        except Exception as exc:
            log.warn("%s %s 成分查询失败 %s" % (today, index, exc))
            members[index] = []

    stock_codes = {code for group in members.values() for code in group}
    closes = quote_panel(stock_codes, today, 41, "close")
    etf_close = quote_panel(ETF_CODES, today, 121, "close")
    momentum = (etf_close.iloc[-1].div(etf_close.iloc[0])
                + etf_close.iloc[-1].div(etf_close.iloc[-61])) / 2 - 1
    rows = []
    for etf in ETF_CODES:
        index = indices.get(etf, "")
        codes = members.get(index, [])
        prices = closes.reindex(columns=codes).iloc[[0, -21, -1]].dropna(axis=1)
        coverage = float(len(prices.columns)) / len(codes) if codes else 0.0
        prior = current = np.nan
        if coverage >= MIN_COVERAGE:
            prior = (prices.iloc[1] > prices.iloc[0]).mean()
            current = (prices.iloc[2] > prices.iloc[1]).mean()
        rows.append(dict(date=today, etf=etf, index_code=index, members=len(codes),
                         usable_members=len(prices.columns), coverage=coverage,
                         momentum=momentum[etf], prior_breadth=prior, breadth=current,
                         breadth_change=current - prior))
    return rows


def match_pairs(rows):
    """按动量从低到高，相邻配对。只有一边广度改善的配对进入比较。"""
    ordered = sorted((row for row in rows
                      if row["momentum"] > 0 and np.isfinite(row["breadth_change"])),
                     key=lambda row: (row["momentum"], row["etf"]))
    unique = []
    seen = set()
    for row in ordered:
        if row["index_code"] not in seen:
            unique.append(row)
            seen.add(row["index_code"])
    pairs = []
    for left, right in zip(unique[::2], unique[1::2]):
        if right["momentum"] - left["momentum"] > MAX_MOMENTUM_GAP:
            continue
        if (left["breadth_change"] > 0) == (right["breadth_change"] > 0):
            continue
        improved, other = (left, right) if left["breadth_change"] > 0 else (right, left)
        pairs.append((improved, other))
    return pairs


def update_returns(today):
    due_ages = (1,) + tuple(horizon + 1 for horizon in HORIZONS)
    due = [pair for pair in g.pending if g.day - pair["signal_day"] in due_ages]
    if not due:
        return
    codes = {code for pair in due for code in pair["codes"]}
    prices = quote_panel(codes, today, 1, "open", require_trading=True).iloc[-1]
    for pair in due:
        age = g.day - pair["signal_day"]
        current = np.asarray(prices.reindex(pair["codes"]).values, dtype=float)
        if age == 1:
            pair["entry"] = current
            pair["entry_date"] = today
            continue
        returns = current / pair["entry"] - 1
        if not np.isfinite(returns).all():
            returns[:] = np.nan
        high, low = returns
        g.results.append(dict(
            signal_date=pair["signal_date"], entry_date=pair["entry_date"], exit_date=today,
            horizon=age - 1, improved_etf=pair["codes"][0], other_etf=pair["codes"][1],
            improved_index=pair["improved_index"], other_index=pair["other_index"],
            momentum_gap=pair["momentum_gap"], improved_change=pair["improved_change"],
            other_change=pair["other_change"], improved_return=high, other_return=low,
            spread=high - low,
        ))
    g.pending = [pair for pair in g.pending
                 if g.day - pair["signal_day"] < HORIZONS[-1] + 1]
    if g.results:
        results = pd.DataFrame(g.results)
        write_file(RETURN_FILE, results.to_csv(index=False), append=False)
        summary = []
        for horizon, group in results.groupby("horizon"):
            # 同一观察日有多个配对时先在日内等权，再跨日统计。
            spread = group.groupby("signal_date").spread.mean().dropna()
            if len(spread):
                summary.append("%d日 有效观察日=%d 平均收益差=%+.2fpp 胜率=%.1f%%"
                               % (horizon, len(spread), spread.mean() * 100,
                                  spread.gt(0).mean() * 100))
        if summary:
            log.info("%s %s" % (today, " | ".join(summary)))


def observe(context):
    today = context.current_dt.strftime("%Y-%m-%d")
    g.day += 1
    update_returns(today)
    if g.day % SAMPLE_EVERY:
        return
    rows = snapshot(today)
    pairs = match_pairs(rows)
    for improved, other in pairs:
        g.pending.append(dict(
            signal_date=today, signal_day=g.day, codes=[improved["etf"], other["etf"]],
            improved_index=improved["index_code"], other_index=other["index_code"],
            momentum_gap=abs(improved["momentum"] - other["momentum"]),
            improved_change=improved["breadth_change"], other_change=other["breadth_change"],
        ))
    g.features.extend(rows)
    write_file(FEATURE_FILE, pd.DataFrame(g.features).to_csv(index=False), append=False)
    usable = sum(np.isfinite(row["breadth_change"]) for row in rows)
    log.info("%s 有效广度 %d/%d，动量相近配对 %d" % (today, usable, len(rows), len(pairs)))
