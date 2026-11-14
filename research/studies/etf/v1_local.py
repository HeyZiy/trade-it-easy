# -*- coding: utf-8 -*-
"""v1 (etf_v1_baseline 动量轮动) 的本地日线回测 runner（无聚宽依赖）。

信号内核直接调用同目录 v1.py 的纯函数 momentum_details 与常量，注入空 jqdata
桩仅为满足其模块顶 `from jqdata import *`，不调用任何平台函数。

与聚宽版的口径差异（首版研究假设，须如实读报告）：
- 聚宽在 14:55 用"25 根已完成日线 + 当日最新价"打分并成交；本地日线用当日收盘
  同时充当最新价与成交价，属收盘近似的无未来函数映射。
- 打分用 AD 后复权价；将同一窗口按当日原始价换算为前复权价时，
  回归斜率与 R² 不变。成交与估值用原始价。
- 用相邻有效行情的复权因子变化折算已有持仓份额。现金分红隐含再投资，
  允许产生零碎模拟份额，缺行情时沿用上一有效估值。
- 本地无点时历史名单：用 AD 当前 ETF 名单按首根行情（start_date）过滤当日已上市，
  仍有幸存者偏差。无 get_current_data 挂单表，成交即时撮合、无未成交挂单。
- 停牌/缺数据以 quality!=ok 表达；建池流动性用截至昨日的 20 根完整成交额。

运行（仓库根目录，默认使用 research/data/etf_daily_ad）：
    .conda/python.exe -m research.studies.etf.v1_local
"""
import argparse
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

from research.tools.backtest.engine import (Account, annual_returns, metrics,
                                            round_trip_stats)
from research.tools.etf_data.data import LocalEtfData

STUDY = Path(__file__).resolve().parent


def load_kernel():
    if "jqdata" not in sys.modules:
        sys.modules["jqdata"] = types.ModuleType("jqdata")
    spec = importlib.util.spec_from_file_location("v1_kernel", str(STUDY / "v1.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MomentumLocal:
    def __init__(self, capital=1_000_000.0, cache_dir=None):
        self.k = load_kernel()
        self.data = LocalEtfData(cache_dir)
        self.cal = self.data.trading_days()
        self.securities = self.data.securities_table
        self.universe = sorted(self.securities.index[self.securities.ok_rows > 0])
        adjusted = self.data._frame("hfq_close")
        raw = self.data._frame("close").reindex(columns=adjusted.columns)
        quality = self.data._frame("quality").reindex(columns=adjusted.columns)
        self.columns = {code: i for i, code in enumerate(adjusted.columns)}
        self.adjusted_close = adjusted.to_numpy(dtype=float)
        self.valid = (quality.eq("ok").to_numpy() & np.isfinite(raw)
                      & raw.gt(0) & np.isfinite(adjusted) & adjusted.gt(0)).to_numpy()
        self.raw_close = raw.where(self.valid).ffill()
        factors = adjusted.div(raw.where(self.valid)).ffill()
        changes = factors.div(factors.shift()).to_numpy()
        self.adjustments = np.where(np.isclose(changes, 1.0, rtol=0, atol=1e-9)
                                    | np.isnan(changes), 1.0, changes)
        self.amount = self.data._frame("amount")
        # v1 冻结口径：无滑点、万一佣金、无最低佣金。
        self.account = Account(capital, commission=self.k.COMMISSION_RATE,
                               min_commission=0.0, slippage=0.0)
        self.pool = []
        self.pool_valid = False
        self.neg_run = {}
        self.day = 0
        self.diag = {"pool_invalid": 0, "rank_incomplete": 0}

    def _closes(self, code, count, loc):
        """截至 loc 的最近 count 个交易日收盘；要求整窗天天有效。

        打分回归把每根当等间隔并乘 250 年化，跳过停牌日会让步长不齐、
        年化失真，故缺一天即整窗作废（返回 None），不跨缺口拼接。
        """
        if loc < 0 or count <= 0:
            return None
        start = loc - count + 1
        if start < 0:
            return None
        col = self.columns[code]
        if not self.valid[start:loc + 1, col].all():
            return None
        return self.adjusted_close[start:loc + 1, col]

    def _quality_ok(self, code, loc):
        return self.valid[loc, self.columns[code]]

    def rebuild_pool(self, loc, held):
        k = self.k
        date = pd.Timestamp(self.cal[loc])
        sec = self.securities.loc[self.universe]
        start = pd.to_datetime(sec["start_date"], errors="coerce")
        listed = start.notna() & (start <= date)
        codes = [c for c in self.universe if bool(listed[c])]
        if not codes and not held:
            raise ValueError("当日无已上市ETF")
        if loc < 20:
            raise ValueError("样本不足20日")
        amount = self.amount.iloc[loc - 20:loc].reindex(columns=codes)
        values = amount.to_numpy(dtype=float)
        contiguous = np.isfinite(values).all(axis=0) & (values >= 0).all(axis=0)
        avg = np.full(values.shape[1], np.nan)
        avg[contiguous] = values[:, contiguous].mean(axis=0)
        liquid = set()
        for i, code in enumerate(codes):
            if contiguous[i] and np.isfinite(avg[i]) and avg[i] >= k.LIQ_AMT20_MIN:
                liquid.add(code)
        pool = set(held)
        for code in sorted(liquid - set(held)):
            if self._closes(code, k.MIN_BARS - 1, loc - 1) is not None:
                pool.add(code)
        if not pool:
            raise ValueError("流动性过滤后池为空")
        return sorted(pool)

    def rank_pool(self, loc, held):
        k = self.k
        rows = []
        complete = True
        for code in self.pool:
            if not self._quality_ok(code, loc):
                continue
            price = self.adjusted_close[loc, self.columns[code]]
            closes = self._closes(code, k.SCORE_DAYS if code in held else k.MIN_BARS - 1,
                                  loc - 1)
            if not np.isfinite(price) or price <= 0 or closes is None:
                complete = False
                continue
            score = k.momentum_details(closes, price)
            if score is None:
                complete = False
                continue
            rows.append({"code": code, "score": score})
        rows.sort(key=lambda r: (-r["score"], r["code"]))
        return rows, complete

    def evaluate_exits(self, loc, held, rows, rank_valid, today):
        k = self.k
        acct = self.account
        rank_of = {r["code"]: i + 1 for i, r in enumerate(rows)}
        row_of = {r["code"]: r for r in rows}
        raw_close = self.raw_close.iloc[loc]
        sold = set()
        for code in list(held):
            if not self._quality_ok(code, loc):
                continue
            price = self.adjusted_close[loc, self.columns[code]]
            if not np.isfinite(price) or price <= 0:
                continue
            score_now = row_of.get(code, {}).get("score")
            if score_now is None:
                closes = self._closes(code, k.SCORE_DAYS, loc - 1)
                if closes is not None:
                    score_now = k.momentum_details(closes, price)
            rank = rank_of.get(code)
            out_of_rank = rank_valid and rank is not None and rank > k.TOPN * k.EXIT_RANK_MULT
            nonpositive = score_now is not None and score_now <= 0
            previous = self.neg_run.get(code, 0)
            neg = previous
            if nonpositive or out_of_rank:
                neg += 1
            elif rank_valid and rank is not None and score_now is not None:
                neg = 0
            self.neg_run[code] = neg
            if not ((nonpositive or out_of_rank) and neg >= k.SCORE_EXIT_CONFIRM):
                continue
            shares = acct.closeable(code, today)
            px = float(raw_close.get(code, np.nan))
            if shares > 0 and np.isfinite(px) and px > 0:
                if acct.sell(today, code, shares, px):
                    self.neg_run.pop(code, None)
                    sold.add(code)
        return sold

    def execute_buys(self, loc, rows, sold_today, today):
        k = self.k
        acct = self.account
        raw_close = self.raw_close.iloc[loc]
        held = set(acct.positions)
        reserved = set(held)
        slots = k.TOPN - len(reserved)
        if slots <= 0:
            return
        prices = {c: float(raw_close.get(c, 0.0)) for c in acct.positions}
        equity = acct.value(prices)
        cash_left = acct.cash
        per = min(cash_left / slots, equity / k.TOPN)
        for row in rows[:k.TOPN]:
            if len(reserved) >= k.TOPN:
                break
            code = row["code"]
            if code in reserved or code in sold_today or row["score"] <= 0:
                continue
            px = float(raw_close.get(code, np.nan))
            if not np.isfinite(px) or px <= 0:
                continue
            budget = min(per, cash_left, acct.cash)
            amount = int(budget / (px * (1 + k.COMMISSION_RATE)) // 100) * 100
            if amount < 100:
                continue
            if acct.buy(today, code, amount, px):
                reserved.add(code)
                cash_left = max(0.0, cash_left - amount * px * (1 + k.COMMISSION_RATE))

    def run(self, start, end):
        acct = self.account
        k = self.k
        first = pd.Timestamp(start).strftime("%Y-%m-%d")
        last = pd.Timestamp(end).strftime("%Y-%m-%d")
        start_loc = self.cal.searchsorted(first, side="left")
        stop_loc = self.cal.searchsorted(last, side="right")
        if start_loc >= stop_loc:
            raise ValueError("回测区间与缓存没有重叠交易日")
        self.pool, self.pool_valid, self.neg_run, self.day = [], False, {}, 0
        dates, navs, expos = [], [], []
        for loc in range(start_loc, stop_loc):
            today = self.cal[loc]
            self.day += 1
            held = set(acct.positions)
            for code in sorted(held):
                multiplier = self.adjustments[loc, self.columns[code]]
                if multiplier != 1.0:
                    acct.apply_adjustment(today, code, multiplier)
            if not self.pool_valid or (self.day - 1) % k.REBUILD_EVERY == 0:
                try:
                    self.pool = self.rebuild_pool(loc, held)
                    self.pool_valid = True
                except Exception:
                    self.pool_valid = False
            if self.pool_valid:
                rows, complete = self.rank_pool(loc, held)
            else:
                rows, complete = [], False
            rank_valid = self.pool_valid and complete
            if not self.pool_valid:
                self.diag["pool_invalid"] += 1
            elif not complete:
                self.diag["rank_incomplete"] += 1
            sold_today = self.evaluate_exits(loc, held, rows, rank_valid, today)
            if rank_valid:
                self.execute_buys(loc, rows, sold_today, today)
            close_today = self.raw_close.iloc[loc]
            prices = {c: float(close_today.get(c, 0.0)) for c in acct.positions}
            nav = acct.value(prices)
            dates.append(today)
            navs.append(nav)
            expos.append(acct.exposure(prices) / nav if nav > 0 else 0.0)
        nav = pd.Series(navs, index=pd.DatetimeIndex(dates), name="nav")
        expo = pd.Series(expos, index=nav.index, name="exposure")
        return nav, expo

    def benchmark(self, start, end, capital=1_000_000.0):
        idx = self.data.index_close("000300")
        idx.index = pd.DatetimeIndex(idx.index)
        idx = idx.loc[pd.Timestamp(start):pd.Timestamp(end)].dropna()
        return idx / idx.iloc[0] * capital


def _fmt(value):
    return "%.3f" % value if isinstance(value, (int, float, np.floating)) else str(value)


def write_report(bt, nav, expo, bench, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = pd.DataFrame(bt.account.ledger)
    fills = ledger[ledger["side"].isin(["buy", "sell"])] if len(ledger) else ledger
    trades = len(fills)
    met = metrics(nav, expo, trades, bt.account.capital)
    rt = round_trip_stats(bt.account.ledger)
    nav.to_frame().join(expo).to_csv(out_dir / "nav.csv", encoding="utf-8-sig")
    ledger.to_csv(out_dir / "ledger.csv", index=False, encoding="utf-8-sig")
    annual_returns(nav, bt.account.capital).to_csv(out_dir / "annual.csv",
                                                   encoding="utf-8-sig")
    bench_nav = bench.reindex(nav.index).ffill()
    bench_met = metrics(bench_nav, pd.Series(1.0, index=nav.index), 1, bt.account.capital)
    first_trade = fills["date"].min() if trades else "无"
    lines = ["v1 本地天级回测（动量轮动基线）", "宇宙: %d 只可信复权ETF" % len(bt.universe),
             "窗口: %s → %s (%d 交易日)" % (nav.index[0].date(), nav.index[-1].date(), len(nav)),
             "", "数据覆盖：",
             "  首个成交日 %s；此前年份空仓。" % first_trade,
             "  结果仅覆盖该缓存宇宙与其上市区间，非全市场无偏验证。",
             "  收盘成交近似；按复权因子调整在手份额 %d 次，现金分红隐含再投资。"
             % (len(ledger) - trades),
             "", "指标："]
    for key, value in met.items():
        lines.append("  %-12s %s" % (key, _fmt(value)))
    lines.append("回合：")
    for key, value in rt.items():
        lines.append("  %-12s %s" % (key, _fmt(value)))
    lines.append("沪深300买入持有对照：")
    for key in ["年化收益%", "最大回撤%", "Sharpe", "期末终值(万)"]:
        lines.append("  %-12s %s" % (key, _fmt(bench_met[key])))
    text = "\n".join(lines)
    (out_dir / "report.txt").write_text(text, encoding="utf-8")
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--cache-dir", default=None,
                        help="LocalEtfData 兼容缓存目录，默认 research/data/etf_daily_ad")
    parser.add_argument("--out", default=str(STUDY / "results" / "v1_local"))
    args = parser.parse_args()
    bt = MomentumLocal(capital=args.capital, cache_dir=args.cache_dir)
    end = args.end or bt.cal[-1]
    nav, expo = bt.run(args.start, end)
    bench = bt.benchmark(nav.index[0], nav.index[-1], args.capital)
    text = write_report(bt, nav, expo, bench, Path(args.out))
    print("[diag] 池无效日 %d | 排名不完整日 %d"
          % (bt.diag["pool_invalid"], bt.diag["rank_incomplete"]))
    print(text)


if __name__ == "__main__":
    main()
