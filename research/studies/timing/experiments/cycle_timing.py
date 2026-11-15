"""Cycle 单 ETF 择时对照。运行 python -m research.studies.timing.experiments.cycle_timing。

东财后复权 OHLC 按日期合并 AmazingData 成交量，取两者共同覆盖期。
沿用择时研究的成本、开盘成交、复权份额和 MA200 起点。阶段机保留完整
可得量价历史，各窗口单独从现金启动。每个账户只交易一只固定 ETF，
空仓时按 Cycle 信号用可用资金建仓，持仓时沿用 Cycle v8.1 的止损、
05 分批减仓和 06/07 清仓规则。
恢复满仓变体只在减仓后遇到完整入场条件时买入，继续沿用原持仓的止损和减仓记录。
"""

from collections import deque
from functools import partial

import numpy as np
import pandas as pd

from research.studies.cycle import cycle_pool_jq as cycle
from research.studies.timing import scan as holding

base = holding.base
OUT = base.RESULTS / "cycle"
VOLUME_DIR = base.DATA / "etf_daily_ad" / "etf_daily"
COMPARATORS = ("长拿", "唐奇安20/10", holding.ATR_CASE)
CYCLE_RULE = "Cycle v8.1（入场满仓）"
REFILL_RULE = "Cycle v8.1（信号恢复满仓）"
CYCLE_RULES = (CYCLE_RULE, REFILL_RULE)


def load_history(code):
    prices = base.load_etf(code)
    volume = pd.read_csv(VOLUME_DIR / f"{code}.csv", index_col="date", parse_dates=True,
                         usecols=["date", "volume"])
    history = prices.loc[volume.index.min():].join(volume, validate="one_to_one")
    if (history.empty or not np.isfinite(history.to_numpy()).all()
            or (history <= 0).any().any()):
        raise ValueError(f"{code} 在量价共同覆盖区间存在缺失或无效数据")
    return history


def build_signals(history):
    """保留完整可得历史的阶段状态，并将收盘判定映射至下一根日线开盘。"""
    indicators = cycle.build_indicators(history)
    stages = deque(maxlen=cycle.TAIL_KEEP)
    signals = {}
    for i, execution_date in enumerate(history.index[1:]):
        row = cycle.RowView(indicators, i)
        stage, _ = cycle.advance_stage(row, stages)
        stages.append(stage)
        if i + 1 >= cycle.MIN_BUFFER:
            signals[execution_date] = (stage, row)
    return signals


def simulate(px, signals, ledger=None, *, refill=False):
    cash, shares, trades = base.CAPITAL, 0, 0
    stop_kind, stop_price = None, 0.0
    extension, ordinal, extension_sold = None, 0, False
    reduced = False
    daily = []
    for date, opening, close in px[["open", "close"]].itertuples():
        signal = signals.get(date)
        if signal is not None:
            stage, row = signal
            quantity = 0
            if shares:
                if row["ext_on"] and row["ext_seg"] != extension:
                    extension, ordinal, extension_sold = row["ext_seg"], row["ext_count"], False
                stop = row["e20"] if stop_kind == "20EMA" else stop_price
                if stage in cycle.EXIT_REASON:
                    quantity, reason = shares, cycle.EXIT_REASON[stage]
                elif row["close"] < stop:
                    quantity, reason = shares, f"stop-{stop_kind}"
                elif stage == "05" and not extension_sold and ordinal in (2, 3):
                    fraction = 0.5 if ordinal == 2 else 0.75
                    quantity = int(shares * fraction // base.LOT) * base.LOT
                    reason = f"exhaustion-{ordinal}"
                    extension_sold = True
                if quantity:
                    shares -= quantity
                    reduced = shares > 0
                    cash += quantity * opening * (1 - base.COST)
                    trades += 1
                    base.record_trade(ledger, date, "sell", opening, quantity, cash,
                                      shares, stage=stage, reason=reason)
            if not quantity and (not shares or (refill and reduced)) and stage in ("02", "03", "04"):
                entry_stop_kind = "20EMA" if stage == "03" else "平台低点"
                entry_stop = row["e20"] if stage == "03" else row["plat_low"]
                if (0 < entry_stop < min(row["close"], opening)
                        and (stage == "03" or opening <= row["close"] * (1 + cycle.CHASE_LIMIT))):
                    quantity = int(cash / (opening * (1 + base.COST)) // base.LOT) * base.LOT
                    if quantity:
                        reason = "refill" if shares else "entry"
                        if not shares:
                            stop_kind, stop_price = entry_stop_kind, entry_stop
                            extension, ordinal, extension_sold = None, 0, False
                        shares += quantity
                        reduced = False
                        cash -= quantity * opening * (1 + base.COST)
                        trades += 1
                        base.record_trade(ledger, date, "buy", opening, quantity, cash,
                                          shares, stage=stage, reason=reason, stop_kind=stop_kind,
                                          stop_price=stop_price, entry_stop=entry_stop)
        nav = cash + shares * close
        daily.append((nav, shares * close / nav, cash, shares))
    return pd.DataFrame(daily, index=px.index, columns=["nav", "exposure", "cash", "shares"]), trades


def save_curves(group, curves, ledgers):
    folder = OUT / group
    folder.mkdir(parents=True, exist_ok=True)
    pd.concat(curves, names=["strategy", "date"]).reset_index().to_csv(
        folder / "daily.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"strategy": name, **trade} for name, trades in ledgers.items()
                  for trade in trades]).to_csv(folder / "trades.csv", index=False, encoding="utf-8-sig")


def main():
    samples, cohorts = [], []
    for asset in base.ASSETS:
        code = asset.etf_code
        history = load_history(code)
        signals = build_signals(history)
        available = history.loc[base.window_start(history):]
        standard = holding.strategy_runners(history)
        runners = {name: standard[name] for name in COMPARATORS}
        runners[CYCLE_RULE] = partial(simulate, signals=signals)
        runners[REFILL_RULE] = partial(simulate, signals=signals, refill=True)
        periods = {"全期": available}
        if code == "159915":
            periods["恒科同期"] = available.loc["2022-03-18":]
        for period, px in periods.items():
            metrics, curves, ledgers = holding.evaluate_window(px, runners, collect_ledgers=True)
            samples.append(metrics.reset_index().assign(
                code=code, asset=asset.name, period=period, start=px.index[0], end=px.index[-1]))
            save_curves(f"{code}_{period}", curves, ledgers)
        rows = holding.rolling_cohorts(history, runners, 3)
        cohorts.append(rows.assign(code=code, asset=asset.name))
        print(f"{code} {asset.name} 完成，{rows.start.nunique()} 个 3 年窗口", flush=True)
    metrics = pd.concat(samples, ignore_index=True)
    rolling = pd.concat(cohorts, ignore_index=True)
    roll_summary = pd.concat([
        base.experience.rolling_summary(rows).reset_index().assign(code=code)
        for code, rows in rolling.groupby("code", sort=False)
    ], ignore_index=True)
    for name, frame in (("metrics", metrics), ("rolling_cohorts", rolling),
                        ("rolling_summary", roll_summary)):
        decimals = dict.fromkeys(frame.select_dtypes(include="number").columns, 6)
        frame.round(decimals).to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
