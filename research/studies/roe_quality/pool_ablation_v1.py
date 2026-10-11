# -*- coding: utf-8 -*-
# pool_ablation_v1 —— 质量池规则消融（本地粗口径，五变体 + CRN 随机抽样，不下单）
#
# 问题（2026-10-09 登记）：FULL 池的各条硬规则各贡献多少？逐条删除，看池规模、
#   裸持全池超额与随机 20 只抽样（共同随机数）的差异。
# 变体（单条删除，不叠加）：
#   FULL        全部原始规则（= curve_v1 原样口径：不剔 ST/解禁/停牌，偏差声明沿用）
#   no_roe      删单季ROE年化>12%（连同其构成性前提"单季盈利>0"一并删除）
#   no_growth   删净利同比>10%
#   no_value    删PE<30与PB<5（连同其构成性前提 eps_ttm 可得、bps>0 一并豁免）
#   no_vol      删vol60<35%
#   -UNLOCK 留待第二批（本地无解禁数据；届时先补解禁源再入组）。
# 曲线一（裸持）：全池等权，S=T 前一交易日收盘、E=下期重建前一交易日，缺价不计入
#   当期均值——无抽样噪声，直接量"删条件后池子本身的质量变化"。
# 曲线二（随机20）：capacity_v1 口径——持有仍留池者保留、空槽补位；CRN=每个重建日
#   每槽位预生成均匀随机数 u_k，跨变体共享同一 u 矩阵，在候选列表（按代码升序，
#   确定性）上按分位取股、命中已取则顺延下一个 u——池重叠段同槽位常选中同一只
#   股票，变体差异只剩池成分，抽样噪声配对抵消；20 组种子=20 组独立 u 序列。
# 数据：筛选口径直接 import pool_curve_v1（同一 load_fin/load_bars/可见性/阈值）；
#   先扫五变体财务屏并集补拉缺失日线（-ROE/-GROWTH 会放入 FULL 拉数时未覆盖的
#   股票，不补会产生幸存者偏差）；各变体在相同"有日线"宇宙上建池。
# 指标：年化超额/期超额夏普/超额最大回撤/绝对最大回撤（vs 中证800）、平均池规模、
#   相对 FULL 的同种子配对差（正差种子数、中位/P10/P90）、逐窗口配对差 t
#   （种子均值序列）、分年度累计贡献（防单年行情依赖）。
# 判读（运行前声明）：删条件后裸持年化超额下降<0.5pp 且回撤不恶化→该条件
#   列为"可删候选"；下降>1.5pp→有实质贡献；中间→观察。五个变体一次筛查属多重
#   比较，按探索性解读；改动平台规格须另过 PIT 精算确认。
# 运行：.conda\python.exe pool_ablation_v1.py fetch   （建池+补日线，可能数分钟）
#       .conda\python.exe pool_ablation_v1.py run     （曲线+CRN+统计，约1-2分钟）
import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from pool_curve_v1 import (CACHE, HERE, ROE_MIN, NP_YOY_MIN, PE_MAX, PB_MAX,
                           VOL_MAX, VOL_BARS, load_fin, load_bars, _deadline,
                           _prev_trade_day, build_rebuild_dates, _sq, fetch_bars)

VARIANTS = ("FULL", "no_roe", "no_growth", "no_value", "no_vol")
TOP_N = 20
SEEDS = range(20)
SEED0 = 20261009
POOL_ST = CACHE / "pools_ablation.pkl"
DETAIL = HERE / "reports" / "pool_ablation_v1_detail.csv"
_screen_cache = {}


def screen_member(recs, period, variant):
    """镜像 curve_v1.financial_pass，按变体删条件；返回 (是否过, eps_ttm)。

    判定结果与 financial_pass 的 AND 结构一致，仅去掉被消融的条件及其构成性
    前提；不回写 recs（curve_v1 版会把 eps_ttm 缓存进记录，本版用返回值）。
    """
    cur = recs.get(period)
    if cur is None:
        return False, None
    if variant != "no_growth":
        if pd.isna(cur["np_yoy"]) or cur["np_yoy"] <= NP_YOY_MIN:
            return False, None
    if variant != "no_value":
        eps_ttm = 0.0
        for k in range(4):
            e = _sq(recs, "eps", period - k)
            if e is None or pd.isna(e):
                return False, None
            eps_ttm += e
        if eps_ttm == 0:
            return False, None
    else:
        eps_ttm = None
    if variant != "no_roe":
        roe_cum, np_cum = cur["roe"], cur["np"]
        if pd.isna(roe_cum) or roe_cum <= 0.5 or pd.isna(np_cum):
            return False, None
        np_sq = _sq(recs, "np", period)
        if np_sq is None or pd.isna(np_sq) or np_sq <= 0:
            return False, None
        if 4.0 * np_sq * (roe_cum / 100.0) / np_cum <= ROE_MIN:
            return False, None
    return True, eps_ttm


def screen_cached(code, recs, period):
    key = (code, period)
    if key not in _screen_cache:
        _screen_cache[key] = {v: screen_member(recs, period, v) for v in VARIANTS}
    return _screen_cache[key]


def build_latest(dates, idx_days, fin):
    """每重建日的 (asof, 最新可见报告期)；可见性与规则无关，只算一次。"""
    out = {}
    for T in dates:
        asof = _prev_trade_day(idx_days, T)
        vis = [p for p in {p for recs in fin.values() for p in recs}
               if _deadline(p) <= asof]
        out[T] = (asof, max(vis) if vis else None)
    return out


def build_pool(variant, fin, latest_by_T, bars_raw, bars_hfq):
    pools = {}
    for T, (asof, latest) in latest_by_T.items():
        members = set()
        if latest is not None:
            for code, recs in fin.items():
                ok, eps_ttm = screen_cached(code, recs, latest)[variant]
                if not ok:
                    continue
                if variant != "no_value":
                    raw = bars_raw.get(code)
                    if raw is None or asof not in raw.index:
                        continue
                    bps = recs[latest]["bps"]
                    if pd.isna(bps) or bps <= 0:
                        continue
                    pe = raw.loc[asof] / eps_ttm
                    pb = raw.loc[asof] / bps
                    if pd.isna(pe) or pd.isna(pb) or pe >= PE_MAX or pb >= PB_MAX:
                        continue
                if variant != "no_vol":
                    h = bars_hfq.get(code)
                    if h is None or asof not in h.index:
                        continue
                    pos = h.index.get_loc(asof)
                    if pos < VOL_BARS - 1:
                        continue
                    w = h.iloc[pos - VOL_BARS + 1: pos + 1]
                    if w.isna().sum() > 0:
                        continue
                    ret = w.pct_change().dropna()
                    if ret.std() * math.sqrt(250) >= VOL_MAX:
                        continue
                members.add(code)
        pools[T] = members
    return pools


def load_bars_map(codes):
    bars_raw, bars_hfq = {}, {}
    for code in sorted(codes):
        r, h = load_bars(code)
        if r is not None:
            bars_raw[code] = r
        if h is not None:
            bars_hfq[code] = h
    return bars_raw, bars_hfq


def cmd_fetch():
    fin = load_fin()
    idx = pd.read_pickle(CACHE / "idx_000906.pkl")
    idx.index = pd.to_datetime(idx.index).astype("datetime64[ns]")
    dates = build_rebuild_dates(idx)
    latest_by_T = build_latest(dates, idx.index, fin)
    print("[fetch] 重建日 %d 个" % len(dates))
    union = set()
    for T, (asof, latest) in latest_by_T.items():
        if latest is None:
            continue
        for code, recs in fin.items():
            if any(ok for ok, _ in screen_cached(code, recs, latest).values()):
                union.add(code)
    print("[fetch] 五变体财务屏并集 %d 只" % len(union))
    missing = [c for c in sorted(union) if load_bars(c)[1] is None]
    print("[fetch] 缺日线 %d 只，开始补拉" % len(missing))
    if missing:
        fetch_bars(missing)
    bars_raw, bars_hfq = load_bars_map(union)
    pools = {v: build_pool(v, fin, latest_by_T, bars_raw, bars_hfq) for v in VARIANTS}
    POOL_ST.write_bytes(pickle.dumps({
        "dates": [d.isoformat() for d in dates],
        "pools": {v: {k.isoformat(): sorted(s) for k, s in pools[v].items()}
                  for v in VARIANTS}}))
    for v in VARIANTS:
        sizes = [len(s) for s in pools[v].values()]
        print("[池规模] %-9s 均值 %4.0f 中位 %4d 最小 %3d 最大 %4d"
              % (v, sum(sizes) / len(sizes), sorted(sizes)[len(sizes) // 2],
                 min(sizes), max(sizes)))


def period_ret(codes, bars, S, E):
    rets = []
    for c in codes:
        h = bars.get(c)
        if h is None or S not in h.index or E not in h.index:
            continue
        r = h.loc[E] / h.loc[S] - 1
        if pd.notna(r):
            rets.append(r)
    return sum(rets) / len(rets) if rets else float("nan")


def metrics(ret, bench):
    """年化超额、期超额夏普、超额最大回撤、绝对最大回撤（NaN 期按现金计）。"""
    b = bench.reindex(ret.index)
    nav = (1 + ret.fillna(0)).cumprod()
    bnav = (1 + b.fillna(0)).cumprod()
    exc = nav / bnav
    ann = exc.iloc[-1] ** (245.0 / (20 * len(ret))) - 1
    e = ret - b
    sh = e.mean() / e.std() * math.sqrt(245 / 20) if e.std() > 0 else float("nan")
    return ann, sh, float((exc / exc.cummax() - 1).min()), float((nav / nav.cummax() - 1).min())


def cmd_run():
    st = pickle.loads(POOL_ST.read_bytes())
    dates = [pd.Timestamp(d) for d in st["dates"]]
    pools = {v: {pd.Timestamp(k): set(s) for k, s in st["pools"][v].items()}
             for v in VARIANTS}
    idx = pd.read_pickle(CACHE / "idx_000906.pkl")
    idx.index = pd.to_datetime(idx.index).astype("datetime64[ns]")
    idx_days = idx.index
    windows = [(_prev_trade_day(idx_days, T), _prev_trade_day(idx_days, dates[i + 1]))
               for i, T in enumerate(dates[:-1])]
    index = dates[:-1]
    bench = pd.Series([idx.loc[E] / idx.loc[S] - 1 for S, E in windows], index=index)

    all_codes = set().union(*[set().union(*pools[v].values()) for v in VARIANTS])
    bars = load_bars_map(all_codes)[1]
    print("[run] 窗口 %d 个；股票 %d 只（有日线 %d）" % (len(windows), len(all_codes), len(bars)))

    bare = {}
    for v in VARIANTS:
        rows = []
        for i, (S, E) in enumerate(windows):
            members = [c for c in pools[v][dates[i]] if c in bars]
            rows.append(period_ret(members, bars, S, E))
        bare[v] = pd.Series(rows, index=index)

    u_all = [np.random.default_rng(SEED0 + s).random((len(windows), TOP_N)) for s in SEEDS]
    crn = {}
    for v in VARIANTS:
        per_seed = []
        for s in SEEDS:
            holdings, rows = set(), []
            for i, (S, E) in enumerate(windows):
                pool = {c for c in pools[v][dates[i]] if c in bars}
                kept = holdings & pool
                cands = sorted(pool - kept)
                slots = TOP_N - len(kept)
                if slots > 0 and cands:
                    taken = set()
                    for k in range(TOP_N):
                        if len(taken) >= slots:
                            break
                        r = min(int(u_all[s][i][k] * len(cands)), len(cands) - 1)
                        if cands[r] not in taken:
                            taken.add(cands[r])
                    kept |= taken
                holdings = kept
                rows.append(period_ret(sorted(holdings), bars, S, E))
            per_seed.append(pd.Series(rows, index=index))
        crn[v] = per_seed

    print("[裸持全池] 年化超额 / 期超额夏普 / 超额回撤 / 绝对回撤 / 平均池规模")
    for v in VARIANTS:
        ann, sh, mdd, ndd = metrics(bare[v], bench)
        n_mean = sum(len(pools[v][dates[i]] & set(bars)) for i in range(len(windows))) / len(windows)
        print("  %-9s %+.2f%%  %+.3f  %+.1f%%  %+.1f%%  %4.0f"
              % (v, ann * 100, sh, mdd * 100, ndd * 100, n_mean))
    print("[随机20·CRN] 年化超额（种子中位，P10/P90） | vs FULL 正差种子数、中位差")
    ann_crn = {v: [metrics(r, bench)[0] for r in crn[v]] for v in VARIANTS}
    for v in VARIANTS:
        a = sorted(ann_crn[v])
        if v == "FULL":
            print("  %-9s %+.2f%% (%+.2f%%/%+.2f%%)  基线" % (v, a[10] * 100, a[2] * 100, a[17] * 100))
            continue
        d = sorted(x - y for x, y in zip(ann_crn[v], ann_crn["FULL"]))
        print("  %-9s %+.2f%% (%+.2f%%/%+.2f%%)  %d/20  %+.2fpp"
              % (v, a[10] * 100, a[2] * 100, a[17] * 100,
                 sum(1 for x in d if x > 0), d[10] * 100))
    print("[逐窗口配对差 vs FULL]（CRN 种子均值序列，20日窗口）")
    for v in VARIANTS[1:]:
        d = pd.concat([crn[v][s] - crn["FULL"][s] for s in SEEDS], axis=1).mean(axis=1).dropna()
        t = d.mean() / d.std(ddof=1) * math.sqrt(len(d)) if d.std(ddof=1) > 0 else float("nan")
        print("  %-9s %+.3fpp/期  t=%+.2f  n=%d" % (v, d.mean() * 100, t, len(d)))
    print("[分年度累计贡献 vs FULL]（CRN 种子均值差，%）")
    cols = {}
    for v in VARIANTS[1:]:
        d = pd.concat([crn[v][s] - crn["FULL"][s] for s in SEEDS], axis=1).mean(axis=1)
        g = d.groupby(d.index.year)
        cols[v] = (g.sum() * 100).round(2)
    ytab = pd.DataFrame(cols)
    print(ytab.to_string())
    for v in VARIANTS[1:]:
        d = bare[v] - bare["FULL"]
        gs = (d.groupby(d.index.year).sum() * 100)
        top = gs.abs().idxmax()
        print("  裸持 %-9s 全期累计差 %+.2f%%；最大贡献年 %d（%+.2f%%）"
              % (v, d.sum() * 100, top, gs.loc[top]))

    rows = []
    for v in VARIANTS:
        for i, T in enumerate(index):
            rows.append({"variant": v, "curve": "bare", "seed": -1, "signal_day": T,
                         "n_pool": len(pools[v][dates[i]] & set(bars)), "n_held": None,
                         "ret": bare[v].iloc[i]})
        for s in SEEDS:
            for i, T in enumerate(index):
                rows.append({"variant": v, "curve": "crn", "seed": s, "signal_day": T,
                             "n_pool": len(pools[v][dates[i]] & set(bars)),
                             "n_held": None, "ret": crn[v][s].iloc[i]})
    pd.DataFrame(rows).to_csv(DETAIL, index=False, encoding="utf-8-sig")
    print("[明细] %s（%d 行）" % (DETAIL.name, len(rows)))
    print("[边界] 本地粗口径（不剔 ST/解禁/停牌、EM 快照可见性近似）；结论为探索性，"
          "改平台规格须过 pool_audit 式 PIT 精算；-UNLOCK 留待第二批。")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "fetch":
        cmd_fetch()
    else:
        cmd_run()
