"""建仓 score 分档尸检：高分建仓是否预示更差的前瞻收益（2026-10-27 触发：纳指 +0.8598 建仓 4 日 -4.2%）。

数据：fit_signals.csv（2016-01-04~2026-09-30，聚宽导出，score/rank/decision_price），
fit_market.csv（close_raw/close_post 复权因子）。同引擎重放口径与 study_score_cap.py 一致：
每日目标 = eligible==1 中 rank 最小者；目标变化日以 decision_price×复权因子 卖出。

预注册判线（跑前写死）：
  H1 成立（高分反转）：最高档（entry score 前 20% 分位）的每笔收益均值
     比中间三档（20~80% 分位）低 1 个百分点以上，且 Welch t < -2；
  否则（单调递增/无差异/样本不足）销案，不修改生产规则。
辅助观察（不参与判线）：whipsaw 率（持有 ≤5 交易日且亏损）随档位的走势。
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
WHIPSAP_MAX_DAYS = 5
GAP_PP = 1.0   # 最高档 vs 中间三档的每笔收益均值差阈值（百分点）
T_CRIT = -2.0


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    sig = pd.read_csv(HERE / "fit_signals.csv", parse_dates=["date"])
    mkt = pd.read_csv(HERE / "fit_market.csv", parse_dates=["date"])
    mkt["adj"] = mkt["close_post"] / mkt["close_raw"]
    return sig, mkt.set_index(["date", "code"])["adj"]


def daily_target(sig: pd.DataFrame) -> pd.Series:
    ok = sig[sig["eligible"].eq(1)]
    idx = ok.groupby("date")["rank"].idxmin()
    t = ok.loc[idx].set_index("date")["code"]
    return t.sort_index()


def price_on(sig: pd.DataFrame, adj, date, code: str) -> float | None:
    row = sig[(sig["date"] == date) & (sig["code"] == code)]
    if row.empty:
        return None
    p = float(row["decision_price"].iloc[0])
    a = adj.get((date, code))
    return p * (float(a) if a is not None and math.isfinite(a) else 1.0)


def build_runs(sig: pd.DataFrame, adj) -> pd.DataFrame:
    target = daily_target(sig)
    dates = list(target.index)
    runs, start = [], 0
    for i in range(1, len(dates) + 1):
        if i == len(dates) or target.iloc[i] != target.iloc[start]:
            entry_date, exit_date, code = dates[start], dates[i - 1], target.iloc[start]
            entry_score = float(sig[(sig["date"] == entry_date)
                                    & (sig["code"] == code)]["score"].iloc[0])
            p_in = price_on(sig, adj, entry_date, code)
            # 卖出发生在目标变化日（若有），否则用运行段最后一日
            sell_date = dates[i] if i < len(dates) else exit_date
            p_out = price_on(sig, adj, sell_date, code) or price_on(sig, adj, exit_date, code)
            hold_days = dates.index(sell_date) - dates.index(entry_date) if i < len(dates) \
                else len(dates) - 1 - start
            if p_in and p_out:
                runs.append(dict(entry=entry_date, code=code, score=entry_score,
                                 ret=(p_out / p_in - 1) * 100, days=hold_days))
            start = i
    return pd.DataFrame(runs)


def welch_t(a: pd.Series, b: pd.Series) -> float:
    na, nb = len(a), len(b)
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(va / na + vb / nb)
    return float((a.mean() - b.mean()) / se) if se > 0 else 0.0


def main() -> None:
    sig, adj = load()
    runs = build_runs(sig, adj)
    n = len(runs)
    print(f"建仓事件 {n} 笔；{runs['entry'].min().date()} ~ {runs['entry'].max().date()}")
    print(f"entry score 分布: p50={runs['score'].quantile(.5):.3f} "
          f"p80={runs['score'].quantile(.8):.3f} p95={runs['score'].quantile(.95):.3f} "
          f"p99={runs['score'].quantile(.99):.3f} max={runs['score'].max():.3f}")
    pct_nasdaq = (runs["score"] < 0.8598).mean() * 100
    print(f"纳指案例 +0.8598 处于历史建仓分位的 {pct_nasdaq:.1f}%\n")

    runs["bin"] = pd.qcut(runs["score"], 5, labels=["Q1最低", "Q2", "Q3", "Q4", "Q5最高"])
    runs["whipsaw"] = runs["days"].le(WHIPSAP_MAX_DAYS) & runs["ret"].lt(0)
    runs["win"] = runs["ret"].gt(0)

    print(f"{'档':<6} {'n':>4} {'score范围':>16} {'每笔均值%':>9} {'中位%':>7} "
          f"{'胜率%':>6} {'whipsaw%':>9} {'均持有日':>8}")
    stats = {}
    for b, g in runs.groupby("bin", observed=True):
        stats[b] = g
        print(f"{str(b):<6} {len(g):>4} [{g['score'].min():.3f},{g['score'].max():.3f}]"
              f"{g['ret'].mean():>12.2f} {g['ret'].median():>7.2f} "
              f"{g['win'].mean()*100:>6.1f} {g['whipsaw'].mean()*100:>9.1f} "
              f"{g['days'].mean():>8.1f}")

    top = stats["Q5最高"]["ret"]
    mid = pd.concat([stats["Q2"]["ret"], stats["Q3"]["ret"], stats["Q4"]["ret"]])
    gap = top.mean() - mid.mean()
    t = welch_t(top, mid)
    print(f"\n判线检验: Q5均值 {top.mean():.2f}% vs 中间三档 {mid.mean():.2f}%，"
          f"差 {gap:+.2f}pp，Welch t = {t:.2f}")
    h1 = gap < -GAP_PP and t < T_CRIT
    print(f"H1（高分反转）{'成立' if h1 else '不成立 → 销案，不改生产规则'}")

    # 单调性观察
    means = [stats[b]["ret"].mean() for b in ["Q1最低", "Q2", "Q3", "Q4", "Q5最高"]]
    print("各档均值序列:", " → ".join(f"{m:+.2f}" for m in means))


if __name__ == "__main__":
    main()
