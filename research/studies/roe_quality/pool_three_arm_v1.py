# -*- coding: utf-8 -*-
# pool_three_arm_v1 —— 质量池选股规则三臂对照（本地离线初筛，不下单）
#
# 问题（2026-10-09 登记）：固定质量池后，池内补位规则本身是否带来增量？
#   臂 random：空槽随机补位，20 种子——"排序到底有没有价值"的裁判；
#   臂 skip20：现行 v3a 采纳分数（跳过最近 20 日的 100 日动量）——在任基线；
#   臂 r120：v1 原版 120 日动量（平台 momentum 列）——上下文参照；
#   臂 m12_2：经典 12-2 动量，P(S-21)/P(S-252)-1——新登记变体；
#   臂 ep：盈利收益率 1/PE 降序（负 PE 自然沉底，不作特判）——新登记变体。
#
# 池与规则：池 = 平台 v1 members 表（pool_rotation_v1_members.csv，130 信号日，
#   池构成与分数无关）。规则沿用 pool_capacity_v1：每 20 交易日重建（S=信号日
#   前一交易日，E=S 后第 20 个交易日），持有仍在池者保留（离线统一缓冲带，
#   不设执行层 20/30 名次缓冲），空槽按各臂规则从当期池内未持有者补位；一期
#   收益 = 持仓 close(E)/close(S)-1 均值，缺价持仓不计入当期均值，无持仓期
#   记 0（现金）。资金约束、整手、费率、滑点不在此层。
# 分数时点：全部按 S 收盘计分；r120 直接用平台 momentum 列，另以本地复算 r120
#   与平台列做 Spearman 对照（预期≈1.00，防口径错位）。
# 头条口径：共同窗口——自首个"四只计分臂均可补满 20 只"的窗口起（m12_2 受
#   行情缓存 2015-06 起点限制，早期窗口天然缺 252 日历史，不可比），同时报全
#   窗口参照数并注明薄仓窗口数。
# 滞后敏感性：买入起点右移 1 个交易日（S→T 收盘），分数与卖出日不变，复算头
#   条——慢分数优势若因此消失即不可信（v3b 执行滞后教训）。
# 边界：12-2 与 E/P 为新登记分数，本层只做初筛与配对差检验；离线无费用、
#   整手、名次缓冲，数字不等于执行收益；清单外不再扫描其他分数；离线初筛成立后才
#   进聚宽执行回测，届时直接对照v3a判读好坏。
# 运行：.conda\python.exe pool_three_arm_v1.py（读 _cache/pool/ 与 members CSV，分钟级）
import random
import statistics as stats
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache" / "pool"
MEMBERS = HERE / "pool_rotation_v1_members.csv"
DETAIL = HERE / "reports" / "pool_three_arm_v1_detail.csv"
SCORED_ARMS = ("r120", "skip20", "m12_2", "ep")
ALL_ARMS = ("random",) + SCORED_ARMS
SEEDS = range(20)
TOP_N = 20
BASE_SEED = 9900             # random.Random(BASE_SEED + seed)，与 capacity 的 4200 系分开
LAG_SHIFT = 1                # 滞后敏感性：买入起点右移的交易日数
ANN = 245.0 / 20             # 每 20 交易日一期的年化系数


def load():
    m = pd.read_csv(MEMBERS, dtype={"code": str})
    m["code6"] = m.code.str[:6]
    m["signal_day"] = pd.to_datetime(m.signal_day)
    dates = sorted(pd.Timestamp(d) for d in m.signal_day.unique())
    pools = {d: set(g.code6) for d, g in m.groupby("signal_day")}
    bars = {}
    for code in sorted(set().union(*pools.values())):
        f = CACHE / ("bars_hfq_%s.pkl" % code)
        if f.exists():
            d = pd.read_pickle(f)
            d["date"] = pd.to_datetime(d.date)
            bars[code] = d.set_index("date").close
    idx = pd.read_pickle(CACHE / "idx_000906.pkl")
    idx.index = pd.to_datetime(idx.index).astype("datetime64[ns]")
    pe = {(d, c): float(p) for d, c, p in zip(m.signal_day, m.code6, m.pe)}
    mom = {(d, c): float(v) for d, c, v in zip(m.signal_day, m.code6, m.momentum)}
    return dates, pools, bars, idx, pe, mom


def period_windows(dates, idx):
    """[(T, S, E, Slag)]：T=信号日，S=T 前一交易日（计分/买入基准），E=S 后 20 个
    交易日，Slag=买入起点右移 LAG_SHIFT 个交易日的滞后敏感性起点。"""
    out = []
    for i in range(len(dates) - 1):
        T = dates[i]
        pos = idx.index.searchsorted(T)
        out.append((T, idx.index[pos - 1], idx.index[pos - 1 + 20],
                    idx.index[pos - 1 + LAG_SHIFT]))
    return out


def close_at(series, dt, back=0):
    """个股自身日历上 dt 往前第 back 根收盘；dt 缺位或数值无效返回 None。"""
    i = series.index.searchsorted(dt, side="right") - 1
    j = i - back
    if i < 0 or j < 0 or series.index[i] != dt:
        return None
    v = series.iloc[j]
    return float(v) if pd.notna(v) and v > 0 else None


def local_scores(arm, T, S, pool, bars, pe, mom):
    """当期池内候选 {code: score}；random 返回 None。"""
    if arm == "random":
        return None
    out = {}
    for c in sorted(pool):
        s = bars.get(c)
        if s is None:
            continue
        if arm == "r120":
            v = mom.get((T, c), float("nan"))
            if pd.notna(v):
                out[c] = v
        elif arm == "skip20":
            a, b = close_at(s, S, 20), close_at(s, S, 120)
            if a is not None and b is not None:
                out[c] = a / b - 1
        elif arm == "m12_2":
            a, b = close_at(s, S, 21), close_at(s, S, 252)
            if a is not None and b is not None:
                out[c] = a / b - 1
        elif arm == "ep":
            p = pe.get((T, c), float("nan"))
            if pd.notna(p) and p != 0:
                out[c] = 1.0 / p
    return out


def fill(arm, held, pool, scores, rng):
    """持有仍在池者保留，空槽按各臂规则补位。"""
    kept = held & pool
    slots = TOP_N - len(kept)
    if slots > 0:
        cands = sorted(pool - kept)
        if arm == "random":
            if cands:
                kept |= set(rng.sample(cands, min(slots, len(cands))))
        else:
            ranked = sorted((c for c in cands if c in scores),
                            key=lambda c: (-scores[c], c))
            kept |= set(ranked[:slots])
    return kept


def period_ret(held, bars, S, E):
    rets = []
    for c in held:
        s = bars.get(c)
        if s is None:
            continue
        a, b = close_at(s, S), close_at(s, E)
        if a is not None and b is not None:
            r = b / a - 1
            if pd.notna(r):
                rets.append(r)
    return sum(rets) / len(rets) if rets else float("nan")


def run_arm(arm, wins, pools, bars, scores_by_win, seed=0):
    """返回 DataFrame(index=信号日, 列 n_eligible/n_held/ret/ret_lag)。

    ret_lag：买入起点右移 LAG_SHIFT 个交易日（S→T 收盘），卖出日 E 不变。
    """
    rng = random.Random(BASE_SEED + seed) if arm == "random" else None
    holdings, rows = set(), []
    for i, (T, S, E, Slag) in enumerate(wins):
        pool = pools[T] & set(bars)
        scores = scores_by_win[arm][i] if arm != "random" else None
        n_eligible = len(pool) if arm == "random" else len(scores)
        holdings = fill(arm, holdings, pool, scores, rng)
        rows.append({
            "signal_day": T,
            "n_eligible": n_eligible,
            "n_held": len(holdings),
            "ret": period_ret(holdings, bars, S, E),
            "ret_lag": period_ret(holdings, bars, Slag, E),
        })
    return pd.DataFrame(rows).set_index("signal_day")


def headline(ret, bench):
    """年化超额、期超额夏普、无持仓期数；NaN 期按现金（0）计入复利。"""
    b = bench.reindex(ret.index)
    nav = (1 + ret.fillna(0)).cumprod()
    bnav = (1 + b.fillna(0)).cumprod()
    exc = nav / bnav
    ann = exc.iloc[-1] ** (ANN / len(ret)) - 1
    e = ret - b
    sh = e.mean() / e.std() * (245.0 / 20) ** 0.5 if e.std() > 0 else float("nan")
    return ann, sh, float(exc.iloc[-1] - 1), int(ret.isna().sum())


def paired_diff(a, b):
    """逐窗口配对差 a-b（两者均有效的窗口），返回 (均值%, t, n)。"""
    d = (a - b).dropna()
    if len(d) < 2 or d.std(ddof=1) == 0:
        return float("nan"), float("nan"), len(d)
    t = d.mean() / d.std(ddof=1) * np.sqrt(len(d))
    return d.mean() * 100, t, len(d)


def main():
    dates, pools, bars, idx, pe, mom = load()
    wins = period_windows(dates, idx)
    index = [T for T, _, _, _ in wins]

    # 预计算各计分臂每窗口分数；本地复算 r120 与平台 momentum 做 Spearman 对照
    scores_by_win = {arm: [] for arm in SCORED_ARMS}
    local_r120, platform_r120 = [], []
    for T, S, _, _ in wins:
        for arm in SCORED_ARMS:
            scores_by_win[arm].append(local_scores(arm, T, S, pools[T], bars, pe, mom))
        # r120 口径核对：本地 P(S)/P(S-120)-1 vs 平台 momentum 列
        loc, plat = {}, {}
        for c in sorted(pools[T] & set(bars)):
            s = bars[c]
            a, b = close_at(s, S), close_at(s, S, 120)
            v = mom.get((T, c), float("nan"))
            if a is not None and b is not None:
                loc[c] = a / b - 1
            if pd.notna(v):
                plat[c] = v
        common = sorted(set(loc) & set(plat))
        if len(common) >= 10:
            ls = pd.Series([loc[c] for c in common])
            ps = pd.Series([plat[c] for c in common])
            local_r120.append(ls.corr(ps, method="spearman"))
            platform_r120.append(len(common))
    print("[口径核对] 本地复算 r120 vs 平台 momentum：Spearman 中位 %.4f（%d 个窗口，n=%d）"
          % (stats.median(local_r120), len(local_r120), int(stats.mean(platform_r120))))

    bench = pd.Series([idx.loc[E] / idx.loc[S] - 1 for _, S, E, _ in wins], index=index)
    runs = {}   # (arm, seed) -> DataFrame
    for arm in SCORED_ARMS:
        runs[(arm, -1)] = run_arm(arm, wins, pools, bars, scores_by_win)
    for seed in SEEDS:
        runs[("random", seed)] = run_arm("random", wins, pools, bars, scores_by_win, seed)

    # 共同窗口：首个四只计分臂均可补满 20 只的窗口起
    full = [i for i in range(len(wins))
            if all(len(scores_by_win[a][i]) >= TOP_N for a in SCORED_ARMS)]
    c0 = full[0] if full else 0
    print("[口径] 全窗口 %d 个（%s 起）；共同窗口自 %s 起（%d 个，m12_2 满员最早）"
          % (len(wins), index[0].date(), index[c0].date(), len(wins) - c0))

    def summary(col, lo, hi, tag):
        print("[头条·%s] 年化超额 / 期超额夏普 / 累计超额 / 无持仓期" % tag)
        for arm in ALL_ARMS:
            if arm == "random":
                rows = [headline(runs[("random", s)][col].iloc[lo:hi], bench.iloc[lo:hi])
                        for s in SEEDS]
                anns = sorted(r[0] for r in rows)
                shs = [r[1] for r in rows]
                print("  %-7s 中位 %+.2f%%  均值 %+.2f%%  P10 %+.2f%%  P90 %+.2f%%"
                      "  夏普(种子中位) %+.3f  无持仓(中位) %d"
                      % (arm, stats.median(anns) * 100, stats.mean(anns) * 100,
                         anns[int(0.1 * len(anns))] * 100, anns[int(0.9 * len(anns))] * 100,
                         stats.median(shs), stats.median([r[3] for r in rows])))
            else:
                ann, sh, cum, thin = headline(runs[(arm, -1)][col].iloc[lo:hi],
                                              bench.iloc[lo:hi])
                print("  %-7s %+.2f%%  %+.3f  %+.2f%%  %d"
                      % (arm, ann * 100, sh, cum * 100, thin))

    ref = runs[("skip20", -1)]["ret"].iloc[c0:]
    print()
    summary("ret", c0, len(wins), "共同窗口")
    print("[配对差 vs skip20]（20日窗口收益差，共同窗口）")
    for arm in ("r120", "m12_2", "ep"):
        m, t, n = paired_diff(runs[(arm, -1)]["ret"].iloc[c0:], ref)
        print("  %-7s %+.3f%%/期  t=%+.2f  n=%d" % (arm, m, t, n))
    diffs = []
    for s in SEEDS:
        d = (runs[("random", s)]["ret"].iloc[c0:] - ref).dropna()
        diffs.append(d.mean() * 100)
    ds = sorted(diffs)
    print("  %-7s 种子均值差 中位 %+.3f%%/期  P10 %+.3f%%  P90 %+.3f%%  >0 占比 %.0f%%"
          % ("random", stats.median(ds), ds[int(0.1 * len(ds))], ds[int(0.9 * len(ds))],
             sum(1 for x in ds if x > 0) / len(ds) * 100))
    print()
    summary("ret", 0, len(wins), "全窗口参照")
    print("[滞后敏感性] 买入起点右移 %d 日（T 收盘），E 不变，共同窗口" % LAG_SHIFT)
    for arm in SCORED_ARMS:
        ann, sh, cum, thin = headline(runs[(arm, -1)]["ret_lag"].iloc[c0:],
                                      bench.iloc[c0:])
        print("  %-7s %+.2f%%  %+.3f  %+.2f%%" % (arm, ann * 100, sh, cum * 100))
    rows = [headline(runs[("random", s)]["ret_lag"].iloc[c0:], bench.iloc[c0:])
            for s in SEEDS]
    anns = sorted(r[0] for r in rows)
    print("  %-7s 中位 %+.2f%%  均值 %+.2f%%" % ("random", stats.median(anns) * 100,
                                                stats.mean(anns) * 100))

    out = []
    for (arm, seed), frame in runs.items():
        f = frame.copy()
        f["arm"], f["seed"] = arm, seed
        out.append(f.reset_index())
    detail = pd.concat(out, ignore_index=True)
    detail.to_csv(DETAIL, index=False, encoding="utf-8-sig")
    print("[明细] %s（%d 行）" % (DETAIL.name, len(detail)))
    print("[边界] 离线无费用/整手/名次缓冲；12-2 与 E/P 仅本层初筛，初筛成立才进执行回测；"
          "random 为裁决臂——若其与 skip20 无可分差异，排序层无增量。")


if __name__ == "__main__":
    main()
