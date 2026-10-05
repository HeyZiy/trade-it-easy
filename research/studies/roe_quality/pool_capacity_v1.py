# -*- coding: utf-8 -*-
# pool_capacity_v1 —— 质量池个人化容量实验：随机 N 只子样（本地，不下单）
#
# 研究问题：全量池（121 只等权）超额 +7.5%/年已双重验证，但个人资金拿不动全量。
#   持有池内随机 N 只、随池滚动，能保留多少超额？随机就是买入顺序——与
#   "池内无排序信息"结论自洽，任何"聪明顺序"须另行对抗本随机对照。
#
# 持有规则（部署形态）：每 20 交易日重建时，持有中仍在池的保留；空槽从
#   当期池内未持有的成员随机补齐；期间不主动交易。一期收益 = 持仓
#   close(E)/close(S)-1 均值（S=重建前一交易日，E=下期重建前一交易日，
#   与 A 曲线同口径可比）。资金约束、整手、费率不在此层。
#
# 预注册判读：N=20 的种子中位年化超额(vs 800) ≥ 全量一半(约 3.7%) 且
#   P10 > 0 → 个人化成立，v1 = 随机 N；否则加大 N 或承认系统是指数形态。
#   种子分布本身就是"单次实现 vs 池均值"的运气度量。
#
# 运行：python pool_capacity_v1.py  （读 _cache/pool/ 缓存，分钟级）
import pickle
import random
import statistics as stats
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache" / "pool"
VOL_BARS = 62
NS = (10, 20, 30)
SEEDS = range(20)            # 每组 N 的 20 个种子
BASE_SEED = 4200             # random.Random(BASE_SEED + N*100 + seed)


def load():
    st = pickle.loads((CACHE / "pools_full.pkl").read_bytes())
    dates = [pd.Timestamp(d) for d in st["dates"]]
    pools = {pd.Timestamp(k): set(v) for k, v in st["pools"].items()}
    bars = {}
    for code in sorted(set().union(*pools.values())):
        f = CACHE / f"bars_hfq_{code}.pkl"
        if f.exists():
            d = pd.read_pickle(f)
            d["date"] = pd.to_datetime(d["date"])
            bars[code] = d.set_index("date")["close"]
    idx = pd.read_pickle(CACHE / "idx_000906.pkl")
    return dates, pools, bars, idx


def period_windows(dates, idx):
    """[(S, E)]：每期信号日到下期信号日前一交易日。"""
    out = []
    for i in range(len(dates) - 1):
        T, Tn = dates[i], dates[i + 1]
        pos = idx.index.searchsorted(T)
        out.append((idx.index[pos - 1], idx.index[pos - 1 + 20]))
    return out


def run_portfolio(dates, pools, bars, idx, n, seed):
    rng = random.Random(4200 + n * 100 + seed)
    wins = period_windows(dates, idx)
    holdings = set()
    rows = []
    for i, (S, E) in enumerate(wins):
        T = dates[i]
        pool = {c for c in pools[T] if c in bars}
        kept = holdings & pool
        slots = n - len(kept)
        if slots > 0:
            cands = sorted(pool - kept)
            if cands:
                kept |= set(rng.sample(cands, min(slots, len(cands))))
        holdings = kept
        rets = []
        for c in holdings:
            s = bars[c]
            if S in s.index and E in s.index:
                r = s.loc[E] / s.loc[S] - 1
                if pd.notna(r):
                    rets.append(r)
        if not rets:
            rows.append(float('nan'))
            continue
        rows.append(sum(rets) / len(rets))
    return pd.Series(rows, index=dates[:-1])


def annualized_excess(ret, bench):
    """bench：与 ret 同索引的基准窗口收益序列（close(E)/close(S)-1）。"""
    nav = (1 + ret.fillna(0)).cumprod()
    bnav = (1 + bench.reindex(ret.index).fillna(0)).cumprod()
    exc = nav / bnav
    e = ret - bench.reindex(ret.index)
    sharpe = e.mean() / e.std() * (245.0 / 20) ** 0.5 if e.std() > 0 else float('nan')
    ann = exc.iloc[-1] ** ((245.0 / 20) / len(ret)) - 1
    return ann, sharpe


def main():
    dates, pools, bars, idx = load()
    wins = period_windows(dates, idx)
    # 全量基准（同一窗口口径）
    full_rets = []
    for i, (S, E) in enumerate(wins):
        members = [c for c in pools[dates[i]] if c in bars]
        rets = [bars[c].loc[E] / bars[c].loc[S] - 1 for c in members
                if S in bars[c].index and E in bars[c].index]
        rets = [r for r in rets if pd.notna(r)]
        full_rets.append(sum(rets) / len(rets) if rets else float('nan'))
    full = pd.Series(full_rets, index=dates[:-1])
    bench = pd.Series([idx.loc[E] / idx.loc[S] - 1 for S, E in wins],
                      index=dates[:-1])
    full_ann, full_sh = annualized_excess(full, bench)
    print("全量: 年化超额 %+.2f%% 期超额夏普 %+.3f" % (full_ann * 100, full_sh))
    for n in NS:
        anns, shs = [], []
        for seed in SEEDS:
            ret = run_portfolio(dates, pools, bars, idx, n, seed)
            a, s = annualized_excess(ret, bench)
            anns.append(a)
            shs.append(s)
        anns_s = sorted(anns)
        p10 = anns_s[int(0.1 * len(anns_s))]
        p90 = anns_s[int(0.9 * len(anns_s))]
        pos_sh = sum(1 for x in shs if x > 0) / len(shs)
        print("N=%2d: 年化超额 中位 %+.2f%%  均值 %+.2f%%  P10 %+.2f%%  P90 %+.2f%%"
              "  夏普>0 占比 %.0f%%"
              % (n, stats.median(anns) * 100, stats.mean(anns) * 100,
                 p10 * 100, p90 * 100, pos_sh * 100))


if __name__ == "__main__":
    main()
