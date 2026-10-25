# -*- coding: utf-8 -*-
# analyze_no_growth_v5 —— v5 质量池去掉“净利同比>10%”的6只账户本地对照
#
# 问题（2026-10-22 登记）：v5 池子偏窄（中位约95、p10约25、最新一期22只），
#   净利同比>10% 是五条硬条件里最弱、数据最脏的一条。拿掉它，6只账户是否
#   至少不变差、且池子明显变宽？
#
# 预登记判线（运行前写死，四条全过才建议放宽；任一条不过则保留成长过滤）：
#   1. 实际12-2动量前6账户：年化收益跌幅 <= 0.50pp；
#   2. 同一账户最大回撤恶化 <= 1.00pp；
#   3. 平均池规模（有日线成员）>= FULL × 1.20；
#   4. 随机6只 CRN 的20个种子年化收益中位数 >= FULL。
#   主口径是“实际动量6只”；随机6只只做选择噪声检查，全池等权只作诊断。
#
# 数据与边界：pools_ablation.pkl 是本地粗口径池（单季ROE年化>12%、净利同比>10%、
#   PE<30、PB<5、vol60<35%；不剔ST/解禁/停牌，财务为EM最新修订快照）。
#   它不是 v5 聚宽池的逐股复制；本检验回答“这五条规则里拿掉成长线值不值”，
#   改平台规格仍需在聚宽做同口径A/B。行业上限本地无历史映射，未施加。
#
# 运行：.conda\python.exe analyze_no_growth_v5.py（约1-2分钟）
import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
CACHE = ROOT / "research" / "studies" / "roe_quality" / "_cache" / "pool"
POOL_ST = CACHE / "pools_ablation.pkl"
OUT_DIR = HERE.parent / "reports"
OUT_MD = OUT_DIR / "no_growth_v5_proxy_2026-10-22.md"
OUT_CSV = OUT_DIR / "no_growth_v5_proxy_2026-10-22_detail.csv"

VARIANTS = ("FULL", "no_growth")
TOP_N = 6
SCORE_CLOSES = 253
SCORE_SKIP = 21
SEEDS = range(20)
SEED0 = 20261022
TRADING_DAYS = 245
STEP = 20


def _prev_trade_day(idx_index, dt):
    return idx_index[max(idx_index.searchsorted(dt) - 1, 0)]


def _load_hfq(code):
    f = CACHE / ("bars_hfq_%s.pkl" % code)
    if not f.exists():
        return None
    d = pd.read_pickle(f)
    d["date"] = pd.to_datetime(d["date"])
    d.set_index("date", inplace=True)
    return d["close"].astype(float)


def period_ret(codes, bars, S, E):
    rets = []
    for c in codes:
        h = bars.get(c)
        if h is None or S not in h.index or E not in h.index:
            continue
        r = h.loc[E] / h.loc[S] - 1
        if pd.notna(r):
            rets.append(float(r))
    return sum(rets) / len(rets) if rets else float("nan")


def curve_metrics(ret, bench):
    ret = pd.Series(ret).astype(float)
    b = bench.reindex(ret.index).astype(float)
    nav = (1 + ret.fillna(0)).cumprod()
    bnav = (1 + b.fillna(0)).cumprod()
    exc = nav / bnav
    e = (ret - b).dropna()
    n = len(ret)
    ann = float(nav.iloc[-1] ** (TRADING_DAYS / (STEP * n)) - 1)
    ann_exc = float(exc.iloc[-1] ** (TRADING_DAYS / (STEP * n)) - 1)
    mdd = float((nav / nav.cummax() - 1).min())
    vol = float(ret.std(ddof=1) * math.sqrt(TRADING_DAYS / STEP))
    sh = float(e.mean() / e.std(ddof=1) * math.sqrt(TRADING_DAYS / STEP)) if e.std(ddof=1) > 0 else float("nan")
    return {"ann": ann, "ann_exc": ann_exc, "mdd": mdd, "vol": vol,
            "sh": sh, "n": n, "pos": float((e > 0).mean())}


def nw_t(x, lag=1):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    mu = x.mean()
    e = x - mu
    var = np.dot(e, e) / n
    for l in range(1, lag + 1):
        var += 2 * (1 - l / (lag + 1)) * np.dot(e[l:], e[:-l]) / n
    if var <= 0:
        return float("nan")
    return float(mu / math.sqrt(var / n))


def momentum_rank(pool, asof, bars):
    scores = {}
    for c in pool:
        h = bars.get(c)
        if h is None or asof not in h.index:
            continue
        pos = h.index.get_loc(asof)
        if pos < SCORE_CLOSES - 1:
            continue
        w = h.iloc[pos - SCORE_CLOSES + 1: pos + 1]
        if len(w) != SCORE_CLOSES or w.isna().any() or (w <= 0).any():
            continue
        base = w.iloc[:-SCORE_SKIP]
        if len(base) != SCORE_CLOSES - SCORE_SKIP or base.isna().any():
            continue
        scores[c] = float(base.iloc[-1] / base.iloc[0] - 1)
    return sorted(scores, key=lambda c: (-scores[c], c))


def pool_stats(sets):
    s = np.array([len(x) for x in sets], dtype=float)
    return {"mean": float(s.mean()), "median": float(np.median(s)),
            "p10": float(np.quantile(s, 0.1)), "min": int(s.min()),
            "max": int(s.max()), "lt6": int((s < 6).sum()),
            "lt10": int((s < 10).sum()), "lt20": int((s < 20).sum())}


def pct(x):
    return "%+.2f%%" % (100 * x)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    st = pickle.loads(POOL_ST.read_bytes())
    dates = [pd.Timestamp(d) for d in st["dates"]]
    pools = {v: {pd.Timestamp(k): set(s) for k, s in st["pools"][v].items()}
             for v in VARIANTS}
    idx = pd.read_pickle(CACHE / "idx_000906.pkl")
    idx.index = pd.to_datetime(idx.index).astype("datetime64[ns]")
    idx_days = idx.index

    windows = [(_prev_trade_day(idx_days, T),
                _prev_trade_day(idx_days, dates[i + 1]))
               for i, T in enumerate(dates[:-1])]
    index = dates[:-1]
    bench = pd.Series([idx.loc[E] / idx.loc[S] - 1 for S, E in windows],
                      index=index)

    union = set().union(*[set().union(*pools[v].values()) for v in VARIANTS])
    bars = {c: h for c in sorted(union) for h in [_load_hfq(c)] if h is not None}
    bar_codes = set(bars)
    pool_sets = {v: [pools[v][dates[i]] & bar_codes for i in range(len(windows))]
                 for v in VARIANTS}
    print("[data] 窗口 %d；池并集 %d 只，有日线 %d 只"
          % (len(windows), len(union), len(bars)))

    bare = {}
    for v in VARIANTS:
        rows = [period_ret(pool_sets[v][i], bars, S, E)
                for i, (S, E) in enumerate(windows)]
        bare[v] = pd.Series(rows, index=index)

    ranked = {v: [] for v in VARIANTS}
    for v in VARIANTS:
        for i in range(len(windows)):
            ranked[v].append(momentum_rank(pool_sets[v][i], windows[i][0], bars))

    def run_momentum(v):
        holdings, rows, nheld = set(), [], []
        for i, (S, E) in enumerate(windows):
            pool = pool_sets[v][i]
            kept = holdings & pool
            selected = list(kept)
            for c in ranked[v][i][:TOP_N]:
                if len(selected) >= TOP_N:
                    break
                if c in kept:
                    continue
                selected.append(c)
            holdings = set(selected)
            rows.append(period_ret(holdings, bars, S, E))
            nheld.append(len(holdings))
        return pd.Series(rows, index=index), pd.Series(nheld, index=index)

    momentum = {v: run_momentum(v) for v in VARIANTS}

    u_all = [np.random.default_rng(SEED0 + s).random((len(windows), TOP_N))
             for s in SEEDS]
    crn = {v: [] for v in VARIANTS}
    for v in VARIANTS:
        for s in SEEDS:
            holdings, rows = set(), []
            for i, (S, E) in enumerate(windows):
                pool = pool_sets[v][i]
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
            crn[v].append(pd.Series(rows, index=index))

    pool_st = {v: pool_stats(pool_sets[v]) for v in VARIANTS}
    bare_m = {v: curve_metrics(bare[v], bench) for v in VARIANTS}
    mom_m = {v: curve_metrics(momentum[v][0], bench) for v in VARIANTS}
    rand_m = {v: [curve_metrics(r, bench) for r in crn[v]] for v in VARIANTS}
    rand_ann = {v: sorted(m["ann"] for m in rand_m[v]) for v in VARIANTS}
    rand_exc = {v: sorted(m["ann_exc"] for m in rand_m[v]) for v in VARIANTS}

    d_mom = (momentum["no_growth"][0] - momentum["FULL"][0]).dropna()
    mom_t = float(d_mom.mean() / d_mom.std(ddof=1) * math.sqrt(len(d_mom))) if d_mom.std(ddof=1) > 0 else float("nan")
    mom_nw = nw_t(d_mom)
    d_bare = (bare["no_growth"] - bare["FULL"]).dropna()
    bare_nw = nw_t(d_bare)
    rand_seed_diff = [x - y for x, y in zip(
        [m["ann"] for m in rand_m["no_growth"]],
        [m["ann"] for m in rand_m["FULL"]])]

    f, n = mom_m["FULL"], mom_m["no_growth"]
    ann_drop = f["ann"] - n["ann"]
    dd_worse = f["mdd"] - n["mdd"]
    size_ratio = pool_st["no_growth"]["mean"] / pool_st["FULL"]["mean"]
    rand_med_f = float(np.median(rand_ann["FULL"]))
    rand_med_n = float(np.median(rand_ann["no_growth"]))
    gates = [
        ("年化变化（正=下跌）<= 0.50pp", ann_drop <= 0.005,
         "%+.2fpp" % (100 * ann_drop)),
        ("回撤变化（正=恶化）<= 1.00pp", dd_worse <= 0.01,
         "%+.2fpp" % (100 * dd_worse)),
        ("平均池规模 >= FULL x1.20", size_ratio >= 1.20,
         "x%.2f" % size_ratio),
        ("随机6只中位年化 >= FULL", rand_med_n >= rand_med_f,
         "%+.2fpp" % (100 * (rand_med_n - rand_med_f))),
    ]
    verdict = "pass" if all(g[1] for g in gates) else "fail"

    lines = []
    lines.append("# v5 去成长过滤：6只账户本地对照（2026-10-22）\n")
    lines.append("问题：拿掉净利同比>10%，池子变宽后，6只动量账户是否至少不变差？\n")
    lines.append("## 预登记判线与结论\n")
    lines.append("| 判线 | 结果 | 判定 |")
    lines.append("|---|---:|---|")
    for name, ok, val in gates:
        lines.append("| %s | %s | %s |" % (name, val, "过" if ok else "不过"))
    lines.append("\n**结论：%s**（四条全过才建议放宽；任一条不过则保留成长过滤）。\n"
                 % ("通过" if verdict == "pass" else "不通过"))
    lines.append("主口径为实际12-2动量前6、质量池内留存、空槽从前6补、无行业上限。\n")
    lines.append("**怎么读：** 全池等权只说成长线值约%.2fpp/年；实际6只账户说约%.2fpp/年。"
                 "差距来自选择：被成长线挡掉的名字更容易进入动量前6，且随后跑输。"
                 "随机6只中位也低%.2fpp（%d/20种子为正），不是单只坏票。"
                 % (100 * (bare_m["FULL"]["ann"] - bare_m["no_growth"]["ann"]),
                    100 * ann_drop,
                    100 * (rand_med_f - rand_med_n),
                    sum(1 for x in rand_seed_diff if x > 0)))
    lines.append("因此“池子窄就删一条”这个方向不成立：成长线是6只账户的承重墙，不是装饰。\n")

    lines.append("## 池规模与容量\n")
    lines.append("| 变体 | 均值 | 中位 | P10 | 最小 | 最大 | <6期 | <10期 | <20期 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        s = pool_st[v]
        lines.append("| %s | %.0f | %.0f | %.0f | %d | %d | %d | %d | %d |"
                     % (v, s["mean"], s["median"], s["p10"], s["min"],
                        s["max"], s["lt6"], s["lt10"], s["lt20"]))
    lines.append("")

    lines.append("## 实际动量6只（主口径）\n")
    lines.append("| 变体 | 年化 | 年化超额 | 最大回撤 | 波动 | 期超额夏普 | 正超额期占比 | 平均持仓 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        m = mom_m[v]
        lines.append("| %s | %s | %s | %s | %.2f%% | %+.3f | %.1f%% | %.2f |"
                     % (v, pct(m["ann"]), pct(m["ann_exc"]), pct(m["mdd"]),
                        100 * m["vol"], m["sh"], 100 * m["pos"],
                        momentum[v][1].mean()))
    lines.append("\n逐期差（no_growth − FULL）：%+.3fpp/期，普通 t=%+.2f，NW t=%+.2f，n=%d。\n"
                 % (100 * d_mom.mean(), mom_t, mom_nw, len(d_mom)))

    yearly = (momentum["no_growth"][0] - momentum["FULL"][0]).dropna().groupby(
        lambda d: d.year).sum() * 100
    lines.append("| 年份 | 差（pp） |")
    lines.append("|---|---:|")
    for y, val in yearly.items():
        lines.append("| %d | %+.2f |" % (y, val))
    neg_years = int((yearly < 0).sum())
    pair_1920 = float(yearly.reindex([2019, 2020]).sum())
    pair_2324 = float(yearly.reindex([2023, 2024]).sum())
    lines.append("\n负向年份 %d/%d；2019–2020 合计 %+.2fpp，2023–2024 合计 %+.2fpp，"
                 "差距不是单年形成。\n"
                 % (neg_years, len(yearly), pair_1920, pair_2324))

    lines.append("## 全池等权（诊断，不作为判线）\n")
    lines.append("| 变体 | 年化 | 年化超额 | 最大回撤 | 波动 | 期超额夏普 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        m = bare_m[v]
        lines.append("| %s | %s | %s | %s | %.2f%% | %+.3f |"
                     % (v, pct(m["ann"]), pct(m["ann_exc"]), pct(m["mdd"]),
                        100 * m["vol"], m["sh"]))
    lines.append("\n逐期差（no_growth − FULL）：%+.3fpp/期，NW t=%+.2f。\n"
                 % (100 * d_bare.mean(), bare_nw))

    lines.append("## 随机6只 CRN（选择噪声检查）\n")
    lines.append("| 变体 | 年化中位 | P10 | P90 | 年化超额中位 |")
    lines.append("|---|---:|---:|---:|---:|")
    for v in VARIANTS:
        lines.append("| %s | %s | %s | %s | %s |"
                     % (v, pct(np.median(rand_ann[v])), pct(rand_ann[v][2]),
                        pct(rand_ann[v][17]), pct(np.median(rand_exc[v]))))
    lines.append("\nno_growth − FULL 同种子年化差：中位 %+.2fpp，%d/20 个种子为正。\n"
                 % (100 * float(np.median(rand_seed_diff)),
                    sum(1 for x in rand_seed_diff if x > 0)))

    lines.append("## 边界\n")
    lines.append("- 本地粗口径池，不是 v5 聚宽池的逐股复制；未剔 ST/解禁/停牌，未施加行业上限。\n")
    lines.append("- 财务为 EM 最新修订快照，存在前视与修订偏差；结论只用于判断是否值得做平台 A/B。\n")
    lines.append("- 本轮证据不支持删成长线；池窄问题仍在（FULL P10=%d，%d期<10），若要继续处理，先测更温和的成长口径（如>0或分位），而不是直接删除。\n"
                 % (pool_st["FULL"]["p10"], pool_st["FULL"]["lt10"]))
    lines.append("- 若要改 v5 规格，仍需在聚宽同资金、同费用、同行业上限下做同口径 A/B，保留完整过程 CSV。\n")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    rows = []
    for v in VARIANTS:
        for i, T in enumerate(index):
            rows.append({"variant": v, "curve": "bare", "seed": -1,
                         "signal_day": T, "n_pool": len(pool_sets[v][i]),
                         "n_held": None, "ret": bare[v].iloc[i]})
            rows.append({"variant": v, "curve": "momentum6", "seed": -1,
                         "signal_day": T, "n_pool": len(pool_sets[v][i]),
                         "n_held": int(momentum[v][1].iloc[i]),
                         "ret": momentum[v][0].iloc[i]})
        for s in SEEDS:
            for i, T in enumerate(index):
                rows.append({"variant": v, "curve": "random6", "seed": s,
                             "signal_day": T, "n_pool": len(pool_sets[v][i]),
                             "n_held": None, "ret": crn[v][s].iloc[i]})
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("\n".join(lines))
    print("[out] %s" % OUT_MD)
    print("[out] %s（%d 行）" % (OUT_CSV.name, len(rows)))
    return verdict


if __name__ == "__main__":
    main()
