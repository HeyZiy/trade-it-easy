# -*- coding: utf-8 -*-
"""宽基ETF典型策略横向对比：长拿 / 网格 / 趋势 / 估值（定投放附录）。

统一口径：
- 数据：本研究 _cache/ 东财后复权 OHLC；信号与成交均在复权价空间，
  属于复权份额模拟，尚未独立核验上游复权与分红口径。
- 成本：单边 0.06%（佣金 0.01% + 滑点 0.05%），按 100 股取整（复权价口径）。
- 窗口：各 ETF 从"MA200 首次可得"之日起至缓存截止日（2026-09-30），
  四策略同起点同窗口，保证可比。
- 节奏：趋势月末出信号、次日开盘执行；估值使用早于交易日的 PE。
- 网格：T+1，跳空按开盘价成交；主结果假设 O-L-H-C 路径，O-H-L-C 作对照。

策略（参数事先固定，不做寻优）：
- buyhold：期初全仓买入持有。
- grid：以窗口首日开盘价为中枢，几何步长 3%，上下各 10 档；资金 20 等份，
  期初买 10 份（等量份额）；跌破下一档买 1 份、涨破上一档卖 1 份，越界躺平。
  固定日内路径不依赖当日收盘涨跌；当日买入不增加可卖份额。
- trend：月末收盘价 > MA200 满仓，否则清仓。
- valuation：跟踪指数 PE 全历史分位（月频，乐咕 ttm），分位<=30% 满仓、>=70% 清仓、
  区间维持；交易日用"最近一期 PE 早于该日"的口径，无前视；至少 24 期历史。

补充四策略对照：python -m research.studies.broad_index_strategies.common.backtest
输出：results/supplementary/baseline/ 下 CSV + 控制台 markdown 表。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import assets as asset_config
from . import experience

ASSETS = asset_config.ASSETS
STUDY = Path(__file__).resolve().parents[1]
PE_CACHE = STUDY / "_cache"
OUT = STUDY / "results" / "supplementary" / "baseline"

# 兼容既有调用；两张映射均由唯一标的配置生成，不单独维护。
ETF_INDEX = {asset.etf_code: asset.index_code for asset in ASSETS}
ETF_NAME = {asset.etf_code: asset.name for asset in ASSETS}
COST = 0.0006          # 单边：佣金0.01% + 滑点0.05%
LOT = 100
CAPITAL = 1_000_000.0
ANN = 244              # A股年均交易日
GRID_STEP = 0.03
GRID_N = 10            # 上下各10档
VAL_LO, VAL_HI = 0.30, 0.70
VAL_MIN_POINTS = 24
AIP_MONTHLY = 10_000.0
STRATS = ["长拿", "网格", "趋势", "估值"]


# ---------------------------------------------------------------- 数据加载
def load_etf(code):
    """东财后复权 OHLC，DatetimeIndex 升序；统一截至 2026-09-30。"""
    df = pd.read_csv(PE_CACHE / f"etf_hfq_{code}.csv", index_col="date")
    df.index = pd.to_datetime(df.index)
    return df.sort_index().loc[:"2026-09-30"]


def load_pe(code):
    df = pd.read_csv(PE_CACHE / f"pe_{code}.csv", index_col="date")
    s = df["pe"]
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def window_start(px):
    """MA200 首次可得的首个交易日。"""
    ready = px["close"].rolling(200, min_periods=200).count().ge(200)
    if not ready.any():
        raise ValueError("行情不足 200 个有效收盘价，无法确定回测窗口")
    return ready.idxmax()


def record_trade(ledger, date, side, price, quantity, cash, shares, **details):
    if ledger is not None and quantity > 0:
        ledger.append({"date": date, "side": side, "price": price,
                       "quantity": quantity, "fee": quantity * price * COST,
                       "cash_after": cash, "shares_after": shares, **details})


# ---------------------------------------------------------------- 策略实现
def sim_buyhold(px, ledger=None):
    n = len(px)
    nav = np.full(n, np.nan)
    expo = np.zeros(n)
    cash_hist, shares_hist = np.zeros(n), np.zeros(n)
    trades = 0
    cash = CAPITAL
    shares = 0
    o_arr = px["open"].to_numpy()
    c_arr = px["close"].to_numpy()
    for i in range(n):
        o, c = o_arr[i], c_arr[i]
        if shares == 0 and not np.isnan(o):
            shares = int(cash / (o * (1 + COST)) // LOT) * LOT
            cash -= shares * o * (1 + COST)
            if shares > 0:
                trades += 1
                record_trade(ledger, px.index[i], "buy", o, shares, cash, shares,
                             phase="initial")
        if not np.isnan(c):
            nav[i] = cash + shares * c
            if nav[i] > 0:
                expo[i] = shares * c / nav[i]
        cash_hist[i], shares_hist[i] = cash, shares
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


def sim_grid(px, step=GRID_STEP, n=GRID_N, reanchor=True, reanchor_every=1,
             intraday_path="low_first", ledger=None):
    """reanchor=True：每隔 reanchor_every 个月以昨日收盘为新中枢重建网格
    （份额与现金不变），日线不能确定真实路径；low_first=O-L-H-C，high_first=O-H-L-C。
    两种路径是场景假设，不是收益上下界。跳空订单按开盘价改善成交。
    卖出只能消耗昨日库存，期初 n 份合并为一笔买单。
    """
    if n <= 0 or step <= 0 or intraday_path not in ("low_first", "high_first"):
        raise ValueError("网格档数、步长必须为正，日内路径须为 low_first/high_first")
    if reanchor and reanchor_every < 1:
        raise ValueError("reanchor_every 必须为正整数月数")
    nrow = len(px)
    nav = np.full(nrow, np.nan)
    expo = np.zeros(nrow)
    cash_hist, shares_hist = np.zeros(nrow), np.zeros(nrow)
    trades = 0
    cash = CAPITAL
    s0 = 0.0
    units = 0
    last_idx = 0
    levels = None
    cur_month = None
    o_arr = px["open"].to_numpy()
    h_arr = px["high"].to_numpy()
    l_arr = px["low"].to_numpy()
    c_arr = px["close"].to_numpy()
    idx_arr = px.index
    for i in range(nrow):
        o, h, l, c = o_arr[i], h_arr[i], l_arr[i], c_arr[i]
        sellable = units  # 当日新买份额不可卖
        if reanchor and levels is not None \
                and (idx_arr[i].to_period("M").ordinal - cur_month.ordinal) >= reanchor_every \
                and not np.isnan(c_arr[i - 1]):
            # 新月份：以昨日收盘为新中枢，last_idx 归零（份额与现金不动）
            cur_month = idx_arr[i].to_period("M")
            levels = {k: c_arr[i - 1] * (1 + step) ** k for k in range(-n, n + 1)}
            last_idx = 0
        valid_bar = np.isfinite([o, h, l, c]).all() and min(o, h, l, c) > 0
        if valid_bar:
            if not (l <= min(o, c) <= max(o, c) <= h):
                raise ValueError(f"{idx_arr[i]} OHLC 高低价范围不合法")
            if levels is None:
                levels = {k: o * (1 + step) ** k for k in range(-n, n + 1)}
                cur_month = idx_arr[i].to_period("M")
                s0 = float(int((CAPITAL / (2 * n)) / (o * (1 + COST)) // LOT) * LOT)
                if s0 <= 0:
                    raise ValueError("资金不足以买入一档网格份额")
                units = n
                cash -= units * s0 * o * (1 + COST)
                trades += 1
                record_trade(ledger, idx_arr[i], "buy", o, units * s0, cash,
                             units * s0, phase="initial", grid_level=0,
                             sellable_after=0, intraday_path=intraday_path)

            def fill(is_buy, price, level, phase):
                nonlocal cash, units, last_idx, trades, sellable
                if is_buy:
                    cash -= s0 * price * (1 + COST)
                    units += 1
                    last_idx -= 1
                else:
                    cash += s0 * price * (1 - COST)
                    units -= 1
                    sellable -= 1
                    last_idx += 1
                trades += 1
                record_trade(ledger, idx_arr[i], "buy" if is_buy else "sell",
                             price, s0, cash, units * s0, phase=phase,
                             grid_level=level, sellable_after=sellable * s0,
                             intraday_path=intraday_path)

            # 开盘时已越过的挂单，成交价改善为开盘价，而不是不可达的格价。
            for is_buy in (True, False):
                while True:
                    k = last_idx - 1 if is_buy else last_idx + 1
                    if not -n <= k <= n:
                        break
                    level = levels[k]
                    if is_buy:
                        if units >= 2 * n or o > level or cash < s0 * o * (1 + COST):
                            break
                    elif units <= 0 or sellable <= 0 or o < level:
                        break
                    fill(is_buy, o, level, "open_gap")

            points = (o, l, h, c) if intraday_path == "low_first" else (o, h, l, c)
            for segment, (start, end) in enumerate(zip(points, points[1:])):
                if start == end:
                    continue
                is_buy = end < start
                while True:
                    k = last_idx - 1 if is_buy else last_idx + 1
                    if not -n <= k <= n:
                        break
                    level = levels[k]
                    if not min(start, end) <= level <= max(start, end):
                        break
                    if is_buy:
                        if units >= 2 * n or cash < s0 * level * (1 + COST):
                            break
                    elif units <= 0 or sellable <= 0:
                        break
                    fill(is_buy, level, level,
                         "close_segment" if segment == 2 else f"segment_{segment + 1}")
        if not np.isnan(c):
            nav[i] = cash + units * s0 * c
            if nav[i] > 0:
                expo[i] = (nav[i] - cash) / nav[i]
        cash_hist[i], shares_hist[i] = cash, units * s0
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


def sim_target(px, target, ledger=None):
    """target：{日期: 0/1}，目标在当日开盘执行（信号生成严格早于执行时点）。"""
    n = len(px)
    nav = np.full(n, np.nan)
    expo = np.zeros(n)
    cash_hist, shares_hist = np.zeros(n), np.zeros(n)
    trades = 0
    cash = CAPITAL
    shares = 0
    cur = 0.0
    pending = None
    o_arr = px["open"].to_numpy()
    c_arr = px["close"].to_numpy()
    for i, t in enumerate(px.index):
        o, c = o_arr[i], c_arr[i]
        if t in target:
            pending = target[t]
        want = pending
        if want is not None and want != cur and not np.isnan(o):
            if want > cur:  # 买至满仓
                shares = int(cash / (o * (1 + COST)) // LOT) * LOT
                cash -= shares * o * (1 + COST)
                if shares > 0:
                    trades += 1
                    record_trade(ledger, t, "buy", o, shares, cash, shares, phase="target")
            elif shares > 0:  # 清仓
                quantity = shares
                cash += shares * o * (1 - COST)
                shares = 0
                trades += 1
                record_trade(ledger, t, "sell", o, quantity, cash, shares, phase="target")
            cur = want
            pending = None
        if not np.isnan(c):
            nav[i] = cash + shares * c
            if nav[i] > 0:
                expo[i] = shares * c / nav[i]
        cash_hist[i], shares_hist[i] = cash, shares
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


# ---------------------------------------------------------------- 目标序列
def month_end_dates(px):
    return px.index.to_series().groupby(px.index.to_period("M")).max()


def build_trend_target(px, window=200, start=None):
    ma = px["close"].rolling(window, min_periods=window).mean()
    valid = px["close"].notna() & ma.notna()
    sig = pd.Series(np.where(valid, px["close"] > ma, np.nan), index=px.index)
    target = {}
    for t in month_end_dates(px):
        v = sig.loc[t]
        if not np.isnan(v):
            loc = px.index.get_loc(t)
            if loc + 1 < len(px):
                execution = px.index[loc + 1]
                if start is None or execution >= start:
                    target[execution] = 1.0 if v else 0.0
    return target


def build_val_target(px, pe, changes_only=True):
    """全历史分位；对交易日 t 用最近一期早于 t 的 PE，目标当日开盘执行（无前视）。"""
    target = {}
    cur = None
    for i, t in enumerate(px.index):
        avail = pe.loc[pe.index < t]
        if len(avail) < VAL_MIN_POINTS:
            continue
        pct = (avail <= avail.iloc[-1]).mean()
        want = 1.0 if pct <= VAL_LO else (0.0 if pct >= VAL_HI else None)
        if want is not None and (not changes_only or want != cur):
            target[t] = want
            cur = want
    return target


# ---------------------------------------------------------------- 指标
def metrics(nav, expo, trades):
    holding = experience.drawdown_metrics(nav, CAPITAL)
    previous = nav.shift(1)
    previous.iloc[0] = CAPITAL
    ret = nav / previous - 1
    cagr = (nav.iloc[-1] / CAPITAL) ** (ANN / len(nav)) - 1
    vol = ret.std() * np.sqrt(ANN)
    dd = (nav / nav.cummax().clip(lower=CAPITAL) - 1).min()
    month_nav = nav.resample("ME").last()
    monthly = month_nav.pct_change()
    monthly.iloc[0] = month_nav.iloc[0] / CAPITAL - 1
    monthly = monthly.dropna()
    return {
        "年化收益%": cagr * 100,
        "年化波动%": vol * 100,
        "Sharpe": ret.mean() / vol * ANN if vol > 0 else np.nan,
        "最大回撤%": dd * 100,
        "Calmar": cagr / abs(dd) if dd < 0 else np.nan,
        "平均仓位%": expo.loc[nav.index].mean() * 100,
        "交易次数": trades,
        "年均成交笔数": trades * ANN / len(nav),
        "月胜率%": (monthly > 0).mean() * 100,
        "期末终值(万)": nav.iloc[-1] / 1e4,
        **holding,
    }


def annual_returns(navs):
    rows = {}
    for name, nav in navs.items():
        nav = nav.dropna()
        ye = nav.resample("YE").last()
        r = ye.pct_change()
        r.iloc[0] = ye.iloc[0] / CAPITAL - 1
        rows[name] = r
    df = pd.DataFrame(rows)
    df.index = df.index.year
    return df


def regime_table(navs, px, history=None):
    """多头/空头段按 ETF 收盘 vs MA200 划分，段>=20 日，统计各策略段年化均值。"""
    history = px if history is None else history
    ma = history["close"].rolling(200, min_periods=200).mean().reindex(px.index)
    bull = (px["close"] > ma).fillna(False)
    seg_id = (bull != bull.shift()).cumsum()
    stats = {s: {"bull": [], "bear": []} for s in navs}
    for _, g in px.groupby(seg_id):
        if len(g) < 20 or g["close"].isna().any():
            continue
        years = len(g) / ANN
        for name, nav in navs.items():
            seg = nav.reindex(g.index).dropna()
            if len(seg) < 20:
                continue
            before = nav.loc[nav.index < g.index[0]].dropna()
            base = before.iloc[-1] if len(before) else CAPITAL
            r = ((seg.iloc[-1] / base) ** (1 / years) - 1) * 100
            stats[name]["bull" if bull.loc[g.index[0]] else "bear"].append(r)
    rows = {}
    for name, d in stats.items():
        rows[name] = {
            "多头段年化均值%": np.mean(d["bull"]) if d["bull"] else np.nan,
            "空头段年化均值%": np.mean(d["bear"]) if d["bear"] else np.nan,
            "多头段数": len(d["bull"]),
            "空头段数": len(d["bear"]),
        }
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------- 定投附录
def sim_aip(px, monthly=AIP_MONTHLY):
    units = 0.0
    invested = 0.0
    flows = []
    seen = None
    o_arr = px["open"].to_numpy()
    for i, t in enumerate(px.index):
        o = o_arr[i]
        if t.to_period("M") != seen and not np.isnan(o):
            seen = t.to_period("M")
            s = int(monthly / (o * (1 + COST)) // LOT) * LOT
            units += s
            invested += s * o * (1 + COST)
            flows.append((t, -s * o * (1 + COST)))
    terminal = units * px["close"].iloc[-1] * (1 - COST)
    flows.append((px.index[-1], terminal))
    return invested, terminal, flows


def xirr(flows):
    dates = pd.to_datetime([f[0] for f in flows])
    amounts = np.array([float(f[1]) for f in flows])
    t = (dates - dates[0]).days.to_numpy() / 365.0

    def npv(rate):
        return float((amounts / (1 + rate) ** t).sum())

    lo, hi = -0.95, 10.0
    if npv(lo) * npv(hi) > 0:
        return np.nan
    f_lo = npv(lo)
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def aip_block(code):
    px = load_etf(code).loc[window_start(load_etf(code)):]
    invested, terminal, flows = sim_aip(px)
    rate = xirr(flows) * 100
    # 对照：同额资金期初一次性买入（长拿），同样计买卖成本
    o0 = px["open"].iloc[0]
    quantity = invested / (o0 * (1 + COST)) // LOT * LOT
    residual = invested - quantity * o0 * (1 + COST)
    lump_terminal = residual + quantity * px["close"].iloc[-1] * (1 - COST)
    lump_rate = xirr([(px.index[0], -invested), (px.index[-1], lump_terminal)]) * 100
    return {"投入(万)": invested / 1e4, "定投终值(万)": terminal / 1e4,
            "定投XIRR%": rate, "同额期初买入XIRR%": lump_rate}


# ---------------------------------------------------------------- 敏感性
def sensitivity_510300():
    code = "510300"
    px_all = load_etf(code)
    px = px_all.loc[window_start(px_all):]
    rows = {}
    res, t = sim_grid(px, reanchor=False)
    rows["网格 固定中枢"] = metrics(res["nav"], res["exposure"], t)
    for step in (0.02, 0.03, 0.05):
        res, t = sim_grid(px, step=step)
        rows[f"网格 step={step:.0%}"] = metrics(res["nav"], res["exposure"], t)
    res, t = sim_grid(px, intraday_path="high_first")
    rows["网格 O-H-L-C"] = metrics(res["nav"], res["exposure"], t)
    for w in (120, 200, 250):
        target = build_trend_target(px_all, window=w, start=px.index[0])
        res, t = sim_target(px, target)
        rows[f"趋势 MA{w}"] = metrics(res["nav"], res["exposure"], t)
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------- 主流程
def run_etf(code, pe, daily_out=None, ledger_out=None):
    px_all = load_etf(code)
    px = px_all.loc[window_start(px_all):]
    res = {}
    trades = {}
    ledgers = {name: [] for name in STRATS}
    res["长拿"], trades["长拿"] = sim_buyhold(px, ledger=ledgers["长拿"])
    res["网格"], trades["网格"] = sim_grid(px, ledger=ledgers["网格"])
    res["趋势"], trades["趋势"] = sim_target(
        px, build_trend_target(px_all, start=px.index[0]), ledger=ledgers["趋势"])
    if pe is not None:
        res["估值"], trades["估值"] = sim_target(px, build_val_target(px, pe), ledger=ledgers["估值"])
    if daily_out is not None:
        daily_out.update(res)
    if ledger_out is not None:
        ledger_out.extend({"strategy": name, **trade}
                          for name, records in ledgers.items() for trade in records)
    navs = {k: v["nav"] for k, v in res.items()}
    met = pd.DataFrame({k: metrics(v["nav"], v["exposure"], trades[k])
                        for k, v in res.items()}).T
    met = experience.add_benchmark_comparison(met, res)
    return met, annual_returns(navs), regime_table(navs, px, history=px_all)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_met, all_reg, all_ann, aip_rows = {}, {}, {}, {}
    for asset in ASSETS:
        code, idx = asset.etf_code, asset.index_code
        daily, ledger = {}, []
        pe = load_pe(idx) if asset.valuation is not None else None
        met, ann, reg = run_etf(code, pe, daily, ledger)
        pd.concat(daily, names=["strategy", "date"]).reset_index().to_csv(
            OUT / f"daily_{code}.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(ledger).sort_values("date", kind="stable").to_csv(
            OUT / f"trades_{code}.csv", index=False, encoding="utf-8-sig")
        # 三个标的均报告另一条固定日内路径，避免只检查沪深300。
        px_all = load_etf(code)
        px = px_all.loc[window_start(px_all):]
        path_rows = {}
        for path in ("low_first", "high_first"):
            grid, count = sim_grid(px, intraday_path=path)
            path_rows[path] = metrics(grid.nav, grid.exposure, count)
        pd.DataFrame(path_rows).T.round(2).to_csv(
            OUT / f"grid_paths_{code}.csv", encoding="utf-8-sig")
        all_met[asset.name], all_ann[asset.name], all_reg[asset.name] = met, ann, reg
        met.round(2).to_csv(OUT / f"metrics_{code}.csv", encoding="utf-8-sig")
        ann.round(4).to_csv(OUT / f"annual_{code}.csv", encoding="utf-8-sig")
        reg.round(2).to_csv(OUT / f"regime_{code}.csv", encoding="utf-8-sig")
        aip_rows[asset.name] = aip_block(code)

    sens = pd.DataFrame()
    if any(asset.etf_code == "510300" for asset in ASSETS):
        sens = sensitivity_510300()
        sens.round(2).to_csv(OUT / "sensitivity_510300.csv", encoding="utf-8-sig")
    aip = pd.DataFrame(aip_rows).T
    aip.round(2).to_csv(OUT / "aip.csv", encoding="utf-8-sig")
    cache_files = sorted(PE_CACHE.glob("*.csv"))
    manifest = {
        "revision": "2026-10-13-execution-fix",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "assets_config_sha256": hashlib.sha256(Path(asset_config.__file__).read_bytes()).hexdigest(),
        "experience_sha256": hashlib.sha256(Path(experience.__file__).read_bytes()).hexdigest(),
        "data_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cache_files},
        "cutoff": "2026-09-30", "capital": CAPITAL, "one_way_cost": COST,
        "lot_in_adjusted_price_space": LOT, "annual_trading_days": ANN,
        "grid": {"step": GRID_STEP, "levels_each_side": GRID_N,
                 "initial_units": GRID_N, "monthly_reanchor": True,
                 "main_path": "O-L-H-C", "alternative_path": "O-H-L-C",
                 "t_plus_one": True, "gap_fill": "open"},
        "trend": {"ma": 200, "pre_window_history": True,
                  "signal": "month_end_close", "execution": "next_open"},
        "valuation": {"lo": VAL_LO, "hi": VAL_HI, "min_points": VAL_MIN_POINTS,
                      "window": "expanding", "pe_date_strictly_before_execution": True},
        "metrics_base": "initial_capital_including_first_day_pnl",
        "versions": {"pandas": pd.__version__, "numpy": np.__version__},
        "windows": {asset.etf_code: {"start": str(window_start(load_etf(asset.etf_code)).date()),
                                     "end": str(load_etf(asset.etf_code).index[-1].date())} for asset in ASSETS},
    }
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    for name, met in all_met.items():
        print(f"\n## {name} 主指标")
        print(met.round(2).to_markdown())
    for name, reg in all_reg.items():
        print(f"\n## {name} 市场状态（按收盘 vs MA200 分段）")
        print(reg.round(2).to_markdown())
    print("\n## 定投附录（月投1万，XIRR 口径）")
    print(aip.round(2).to_markdown())
    if not sens.empty:
        print("\n## 510300 参数敏感性（仅报告，不选优）")
        print(sens.round(2).to_markdown())
    for name, ann in all_ann.items():
        print(f"\n## {name} 分年收益%")
        print((ann * 100).round(1).to_markdown())


if __name__ == "__main__":
    main()
