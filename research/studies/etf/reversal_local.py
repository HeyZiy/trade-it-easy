# -*- coding: utf-8 -*-
"""reversal_v1 天级策略的本地回测 runner（无聚宽依赖）。

数据：research/tools/etf_data/_cache 东财宽表。信号内核直接调用同目录
reversal_v1.py 的纯函数（entry_signal/entry_allowed/exit_reason），注入空
jqdata 桩仅为满足其模块顶 `from jqdata import *`，不调用任何平台函数。

与聚宽版的口径差异（首版研究假设，须如实读报告）：
- 信号在供应商后复权价空间计算。内核全是比率/同一窗口内比较，pre 与 hfq
  只差一个常数标尺，信号与退出等价；免去逐日动态前复权锚点重算。
- 成交与净值用原始价（对齐聚宽 use_real_price=True）：买开+0.002、卖开-0.002，
  单边万一佣金最低5元，100份整手，T+1。份数按昨日原始收盘估算。分红现金不建模。
- 本地无 FUND_INVEST_TARGET 跟踪指数映射：建池不做指数归并，每只ETF独立成组
  （index_key=code），仍剔除货币基金、上市满365日、20日均额>=3000万。
- 宇宙默认仅取缓存中具备可信复权（ok_rows>0）的ETF；复权缺失标的的信号帧被
  质量门置 NaN，自然不触发。全市场覆盖见任务#5。

运行（仓库根目录）：
    .conda/python.exe -m research.studies.etf.reversal_local --start 2016-01-01
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
LIQ_MIN = 30_000_000.0
WEIGHT = 0.10
MAX_NAMES = 3
MAX_EXPOSURE = 0.30
MAX_HOLD_DAYS = 10
POOL_REFRESH_DAYS = 5
COMMISSION = 0.0001
SLIPPAGE = 0.002
CASH_PATTERN = "货币|快线|快钱"


def load_kernel():
    if "jqdata" not in sys.modules:
        sys.modules["jqdata"] = types.ModuleType("jqdata")
    spec = importlib.util.spec_from_file_location(
        "reversal_v1_kernel", str(STUDY / "reversal_v1.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LocalBacktest:
    def __init__(self, capital=1_000_000.0, universe="verified", cache_dir=None):
        self.kernel = load_kernel()
        self.data = LocalEtfData(cache_dir) if cache_dir else LocalEtfData()
        self.cal = self.data.trading_days()
        self.loc_of = {d: i for i, d in enumerate(self.cal)}
        self.securities = self.data.securities_table
        if universe == "verified":
            self.universe = sorted(self.securities.index[self.securities.ok_rows > 0])
        else:
            self.universe = sorted(self.securities.index)
        fields = ["hfq_open", "hfq_high", "hfq_low", "hfq_close",
                  "amount", "quality", "open", "close"]
        self.frames = {f: self.data._frame(f) for f in fields}
        self.frames["close"] = self.frames["close"].ffill()
        self.account = Account(capital, commission=COMMISSION,
                               min_commission=5.0, slippage=SLIPPAGE)

    def _loc(self, date):
        key = pd.Timestamp(date).strftime("%Y-%m-%d")
        if key in self.loc_of:
            return self.loc_of[key]
        loc = self.cal.searchsorted(key, side="right") - 1
        if loc < 0:
            raise KeyError("%s 早于缓存首日" % key)
        return loc

    def signal_frames(self, codes, loc):
        sl = slice(max(0, loc - 21), loc + 1)
        o = self.frames["hfq_open"].iloc[sl]
        h = self.frames["hfq_high"].iloc[sl]
        l = self.frames["hfq_low"].iloc[sl]
        c = self.frames["hfq_close"].iloc[sl]
        m = self.frames["amount"].iloc[sl]
        q = self.frames["quality"].iloc[sl]
        index = pd.DatetimeIndex(o.index)
        out = {}
        for code in codes:
            out[code] = pd.DataFrame({
                "open": o[code].to_numpy(), "high": h[code].to_numpy(),
                "low": l[code].to_numpy(), "close": c[code].to_numpy(),
                "money": m[code].to_numpy(),
                "paused": q[code].ne("ok").astype(int).to_numpy()}, index=index)
        return out

    def build_pool(self, loc):
        date = pd.Timestamp(self.cal[loc])
        codes = self.universe
        sec = self.securities.loc[codes]
        start = pd.to_datetime(sec["start_date"], errors="coerce")
        listed = start.notna() & (start <= date - pd.Timedelta(days=365))
        sl = slice(max(0, loc - 19), loc + 1)
        avg = self.frames["amount"].iloc[sl][codes].mean(skipna=True).fillna(0.0)
        names = sec["display_name"].astype(str)
        cash = names.str.contains(CASH_PATTERN, na=False)
        keep = listed & (avg >= LIQ_MIN) & ~cash
        rows = [{"code": c, "index_key": c, "pool_key": c, "avg_money": float(avg[c])}
                for c in codes if bool(keep[c])]
        rows.sort(key=lambda r: (-r["avg_money"], r["code"]))
        return rows

    def run(self, start, end):
        k = self.kernel
        acct = self.account
        start_loc, end_loc = self._loc(start), self._loc(end)
        book = {}
        self.pool_rows = []
        pool_loc = None
        dates, navs, expos = [], [], []
        raw_open = self.frames["open"]
        raw_close = self.frames["close"]
        for loc in range(start_loc, end_loc + 1):
            today = self.cal[loc]
            prev_loc = loc - 1
            bought_today = set()
            open_today = raw_open.iloc[loc]
            sold_today = False
            for code in list(acct.positions):
                meta = book.get(code)
                if meta is None or meta["entry_date"] >= today:
                    continue
                if not meta.get("exit"):
                    age = prev_loc - self.loc_of[meta["entry_date"]] + 1
                    if age >= MAX_HOLD_DAYS:
                        meta["exit"] = "time"
                    else:
                        frame = self.signal_frames([code], prev_loc)[code]
                        meta["exit"] = k.exit_reason(frame, meta)
                if not meta.get("exit"):
                    continue
                sold_today = True
                closeable = acct.closeable(code, today)
                px = open_today.get(code, np.nan)
                if closeable > 0 and np.isfinite(px) and px > 0:
                    acct.sell(today, code, closeable, float(px))
            for code in list(book):
                if acct.shares(code) == 0:
                    del book[code]
            if not sold_today:
                held = list(acct.positions)
                unknown = [c for c in held if c not in book]
                if not unknown and len(held) < MAX_NAMES:
                    if pool_loc is None or (prev_loc - pool_loc) > POOL_REFRESH_DAYS:
                        self.pool_rows = self.build_pool(prev_loc)
                        pool_loc = prev_loc
                    self._entries(today, loc, prev_loc, book, bought_today, open_today)
            close_today = raw_close.iloc[loc]
            prices = {c: close_today.get(c, 0.0) for c in acct.positions}
            nav = acct.value(prices)
            dates.append(today)
            navs.append(nav)
            expos.append(acct.exposure(prices) / nav if nav > 0 else 0.0)
        nav = pd.Series(navs, index=pd.DatetimeIndex(dates), name="nav")
        expo = pd.Series(expos, index=nav.index, name="exposure")
        return nav, expo

    def _entries(self, today, loc, prev_loc, book, bought_today, open_today):
        k = self.kernel
        acct = self.account
        rows = sorted(self.pool_rows, key=lambda r: (-r["avg_money"], r["code"]))
        frames = self.signal_frames([r["code"] for r in rows], prev_loc)
        close_prev = self.frames["close"].iloc[prev_loc]
        for row in rows:
            held = list(acct.positions)
            if len(held) >= MAX_NAMES:
                break
            if any(c not in book for c in held):
                break
            indices = {book[c]["index_key"] for c in held}
            code = row["code"]
            if code in held or code in bought_today or row["index_key"] in indices:
                continue
            signal = k.entry_signal(frames.get(code))
            if signal is None or not k.entry_allowed(signal, signal["signal_close"]):
                continue
            prices_open = {c: open_today.get(c, 0.0) for c in held}
            equity = acct.value(prices_open)
            exposure = acct.exposure(prices_open)
            budget = equity * WEIGHT
            if equity <= 0 or exposure + budget > equity * MAX_EXPOSURE + 1e-8 \
                    or acct.cash < budget:
                continue
            price = float(close_prev.get(code, np.nan))
            px = float(open_today.get(code, np.nan))
            if not np.isfinite(price) or price <= 0 or not np.isfinite(px) or px <= 0:
                continue
            amount = int((budget - 5.0) / ((price + SLIPPAGE) * (1 + COMMISSION)) // 100) * 100
            if amount < 100:
                continue
            if acct.buy(today, code, amount, px):
                book[code] = dict(signal, pool_key=row["pool_key"],
                                  index_key=row["index_key"], entry_date=today, exit=None)
                bought_today.add(code)

    def benchmark(self, start, end, capital=1_000_000.0):
        idx = self.data.index_close("000300")
        idx.index = pd.DatetimeIndex(idx.index)
        idx = idx.loc[pd.Timestamp(start):pd.Timestamp(end)].dropna()
        return idx / idx.iloc[0] * capital


def write_report(bt, nav, expo, bench, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    trades = len(bt.account.ledger)
    met = metrics(nav, expo, trades, bt.account.capital)
    rt = round_trip_stats(bt.account.ledger)
    nav.to_frame().join(expo).to_csv(out_dir / "nav.csv", encoding="utf-8-sig")
    pd.DataFrame(bt.account.ledger).to_csv(out_dir / "ledger.csv",
                                           index=False, encoding="utf-8-sig")
    annual_returns(nav, bt.account.capital).to_csv(out_dir / "annual.csv",
                                                   encoding="utf-8-sig")
    bench_nav = bench.reindex(nav.index).ffill()
    bench_met = metrics(bench_nav, pd.Series(1.0, index=nav.index), 1, bt.account.capital)
    starts = pd.to_datetime(bt.securities.loc[bt.universe, "start_date"], errors="coerce")
    recent = int((starts >= pd.Timestamp("2025-01-01")).sum())
    old = int((starts <= pd.Timestamp("2020-01-01")).sum())
    led = pd.DataFrame(bt.account.ledger)
    first_trade = led["date"].min() if len(led) else "无"
    lines = ["reversal_v1 本地天级回测", "宇宙: %d 只可信复权ETF" % len(bt.universe),
             "窗口: %s → %s (%d 交易日)" % (nav.index[0].date(), nav.index[-1].date(), len(nav)),
             "", "数据覆盖：",
             "  首个成交日 %s；此前年份空仓。" % first_trade,
             "  宇宙中 2025 年后上市 %d 只、2020 年前上市 %d 只。" % (recent, old),
             "  结果仅覆盖该缓存宇宙与其上市区间，非全市场无偏验证。",
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


def _fmt(value):
    return "%.3f" % value if isinstance(value, (int, float, np.floating)) else str(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--cache-dir", default=None,
                        help="LocalEtfData 兼容缓存目录，如 research/data/etf_daily_ad")
    parser.add_argument("--out", default=str(STUDY / "results" / "reversal_local"))
    args = parser.parse_args()
    bt = LocalBacktest(capital=args.capital, cache_dir=args.cache_dir)
    end = args.end or bt.cal[-1]
    nav, expo = bt.run(args.start, end)
    bench = bt.benchmark(args.start, end, args.capital)
    text = write_report(bt, nav, expo, bench, Path(args.out))
    print(text)


if __name__ == "__main__":
    main()