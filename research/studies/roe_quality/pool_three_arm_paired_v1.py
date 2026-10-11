# -*- coding: utf-8 -*-
# pool_three_arm_paired_v1 —— 三臂对照的配对统计深检（读 pool_three_arm_v1_detail.csv，不重算）
#
# 实验A：m12_2 vs 20 个随机种子的逐期配对比较
#   - 胜负：均值差为正的种子数、复利终值更高的种子数、逐窗口胜多种子数分布；
#   - 置信区间：对跨种子平均差序列 d̄(t) 做 Politis-Romano 平稳自助（循环取块、
#     几何块长，均值 8 期≈1.6 年，另报 4/12 期稳健性）——不把相邻期当独立观测；
#   - 行情阶段依赖：按自然年拆分均值与累计贡献，标出最大贡献年。
# 实验B：m12_2 vs skip20 分年度配对比较
#   - 全样本自助 CI 与 p，再按年拆分：期数、均值差、正差窗口数、累计贡献；
#   - 解读：优势集中于个别年份→视为行情巧合；多数年份小正差→增信。
# 口径：共同窗口（四只计分臂均满 20 只的最早窗口起，与 v1 头条一致，从明细自动
#   判定）；ret 列（S 收盘起算）为主，ret_lag 仅附均值对照。
# 运行：.conda\python.exe pool_three_arm_paired_v1.py
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DETAIL = HERE / "reports" / "pool_three_arm_v1_detail.csv"
SUMMARY = HERE / "reports" / "pool_three_arm_paired_v1_summary.csv"
SCORED = ("r120", "skip20", "m12_2", "ep")
TOP_N = 20
N_SEED = 20
B_BOOT = 5000
SEED = 20261009


def boot_ci(x, mean_block=8.0, b=B_BOOT, seed=SEED):
    """Politis-Romano 平稳自助：几何块长（期望 mean_block 期）、循环取块。

    返回 (点估计%, CI下限%, CI上限%, 双侧p近似)。
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    p = 1.0 / mean_block
    rng = np.random.default_rng(seed + int(mean_block * 10))
    boots = np.empty(b)
    for i in range(b):
        idx = np.empty(n, dtype=int)
        pos, start = 0, int(rng.integers(n))
        while pos < n:
            length = int(rng.geometric(p))
            for k in range(length):
                if pos >= n:
                    break
                idx[pos] = (start + k) % n
                pos += 1
            start = int(rng.integers(n))   # 每块重新随机起点（平稳自助）
        boots[i] = x[idx].mean()
    lo, hi = np.percentile(boots, [2.5, 97.5])
    pval = 2 * min((boots <= 0).mean(), (boots >= 0).mean())
    return float(x.mean()) * 100, float(lo) * 100, float(hi) * 100, min(float(pval), 1.0)


def yearly(d):
    """按自然年汇总：期数、均值%/期、正差窗口数、累计贡献%。"""
    g = d.groupby(d.index.year)
    return pd.DataFrame({
        "n": g.size(),
        "mean_pct": g.mean() * 100,
        "npos": g.apply(lambda s: int((s > 0).sum())),
        "cum_pct": g.sum() * 100,
    })


def main():
    det = pd.read_csv(DETAIL, parse_dates=["signal_day"])
    ok = None
    for arm in SCORED:
        days = set(det.loc[(det.arm == arm) & (det.n_eligible >= TOP_N), "signal_day"])
        ok = days if ok is None else (ok & days)
    c0 = min(ok)
    sub = det[det.signal_day >= c0]

    m12 = sub[sub.arm == "m12_2"].set_index("signal_day")
    skip = sub[sub.arm == "skip20"].set_index("signal_day")
    rand = [sub[(sub.arm == "random") & (sub.seed == s)].set_index("signal_day")["ret"]
            for s in range(N_SEED)]
    randmat = pd.concat(rand, axis=1)
    randm = randmat.mean(axis=1)

    da = m12["ret"] - randm          # 实验A：vs 跨种子随机平均
    db = m12["ret"] - skip["ret"]    # 实验B：vs skip20
    print("[口径] 共同窗口 %d 个，%s 起" % (len(da), c0.date()))
    rho1 = np.corrcoef(da[:-1], da[1:])[0, 1]
    rho2 = np.corrcoef(da[:-2], da[2:])[0, 1]
    print("[自相关] 实验A差序列 lag1=%+.3f lag2=%+.3f → 平稳自助块长均值取 8 期，另报 4/12 期"
          % (rho1, rho2))

    print()
    print("=== 实验A：m12_2 vs 20 个随机种子（逐期配对） ===")
    seed_means, wins_mean, wins_comp = [], 0, 0
    for rs in rand:
        d = m12["ret"] - rs
        seed_means.append(d.mean() * 100)
        wins_mean += int(d.mean() > 0)
        wins_comp += int((1 + m12["ret"]).prod() > (1 + rs).prod())
    sms = sorted(seed_means)
    print("均值差>0 种子：%d/20；复利终值更高：%d/20；"
          "种子均值差 中位 %+.3f%%/期  P10 %+.3f%%  P90 %+.3f%%"
          % (wins_mean, wins_comp, float(np.median(seed_means)), sms[2], sms[-3]))
    per_win = (randmat.values < m12["ret"].values[:, None]).sum(axis=1)
    print("逐窗口胜多种子：中位 %d/20  P25 %d  P75 %d"
          % (float(np.median(per_win)), float(np.percentile(per_win, 25)),
             float(np.percentile(per_win, 75))))
    m, lo, hi, pv = boot_ci(da)
    print("跨种子平均差 d̄：均值 %+.3f%%/期  CI[块长8] [%+.3f%%, %+.3f%%]  p≈%.3f"
          % (m, lo, hi, pv))
    rows = [("A_dbar_vs_random_mean", m, lo, hi, pv)]
    for blk in (4.0, 12.0):
        m, lo, hi, pv = boot_ci(da, mean_block=blk)
        print("  块长均值 %.0f 期：%+.3f%%  [%+.3f%%, %+.3f%%]  p≈%.3f" % (blk, m, lo, hi, pv))
        rows.append(("A_dbar_vs_random_block%.0f" % blk, m, lo, hi, pv))
    ya = yearly(da)
    print("按年拆分（均值%/期、正差窗口、累计贡献%）：")
    print(ya.round(3).to_string())
    top = ya.cum_pct.abs().idxmax()
    print("最大贡献年：%d（%+.2f%%），全期累计 %+.2f%% → %s"
          % (top, ya.loc[top, "cum_pct"], ya.cum_pct.sum(),
             "优势集中于此年" if abs(ya.loc[top, "cum_pct"]) > abs(ya.cum_pct.sum())
             else "贡献不完全集中于单年"))
    randm_lag = sub[sub.arm == "random"].set_index("signal_day")["ret_lag"] \
        .groupby(level=0).mean()
    print("滞后对照（买入右移1日）：d̄ 均值 %+.3f%%/期"
          % ((m12["ret_lag"] - randm_lag).mean() * 100))

    print()
    print("=== 实验B：m12_2 vs skip20 分年度 ===")
    m, lo, hi, pv = boot_ci(db)
    print("全样本：均值 %+.3f%%/期  CI[块长8] [%+.3f%%, %+.3f%%]  p≈%.3f" % (m, lo, hi, pv))
    rows.append(("B_m12_2_vs_skip20_mean", m, lo, hi, pv))
    yb = yearly(db)
    print(yb.round(3).to_string())
    print("正差年份：%d/%d；最大贡献年 %d（%+.2f%%）"
          % (int((yb.mean_pct > 0).sum()), len(yb),
             yb.cum_pct.abs().idxmax(), yb.loc[yb.cum_pct.abs().idxmax(), "cum_pct"]))
    print("滞后对照：均值 %+.3f%%/期" % ((m12["ret_lag"] - skip["ret_lag"]).mean() * 100))

    out = []
    for part, table in (("A_vs_random", ya), ("B_vs_skip20", yb)):
        t = table.reset_index().rename(columns={"signal_day": "year"})
        t.insert(0, "part", part)
        out.append(t)
    pd.concat(out, ignore_index=True).to_csv(SUMMARY, index=False, encoding="utf-8-sig")
    print("[明细] %s" % SUMMARY.name)


if __name__ == "__main__":
    main()
