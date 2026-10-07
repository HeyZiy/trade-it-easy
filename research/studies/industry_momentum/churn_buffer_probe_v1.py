# -*- coding: utf-8 -*-
# churn_buffer_probe_v1 —— score 0 轴震荡的绞肉税测量（只读本地缓存，2026-10-06）
# 本脚本仍保留旧评分/旧样本口径；使用时先修正评分或重建样本，再重跑。
# 方法：重放 score≤0 出场路径，测量确认期与缓冲对换手的影响。
# 旧结果及裁决已移除；使用前核对评分口径、重建派生样本并重新验证。
# 旧收益、缓存 score/rank 与版本优劣不能作为修正评分的结论。

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PANEL = HERE / 'reports' / 'crowd_fwd_v1' / 'panel.pkl'
CACHE = HERE / '_cache' / 'etf_daily_full.pkl'


def run_cycles(dates, scores, closes, mode):
    """状态机：score>0 进入，按 mode 出场。返回 (cycles, gaps)。
    cycles = [(entry_date, exit_date, 持仓期收益)]；
    gaps  = [(exit_date, reentry_date, 空仓期收益)]——正=卖低买高（绞肉税）。"""
    cyc, gaps = [], []
    in_pos = False
    entry_i = 0
    exit_i = None
    neg_run = 0
    for i, s in enumerate(scores):
        if not in_pos:
            if s > 0:
                if exit_i is not None:
                    gaps.append((dates[exit_i], dates[i],
                                 float(closes[i] / closes[exit_i] - 1.0)))
                    exit_i = None
                in_pos = True
                entry_i = i
                neg_run = 0
            continue
        # 持仓中，判出场
        if mode == 'base':
            fire = s <= 0
        elif mode == 'band':
            fire = s < -0.05
        else:  # confirm
            neg_run = neg_run + 1 if s <= 0 else 0
            fire = neg_run >= 3
        if fire:
            cyc.append((dates[entry_i], dates[i],
                        float(closes[i] / closes[entry_i] - 1.0)))
            in_pos = False
            exit_i = i
    return cyc, gaps


def summarize(name, cyc, gaps):
    rets = np.array([c[2] for c in cyc])
    g = np.array([x[2] for x in gaps])
    if len(rets):
        print(f'{name:>8}: {len(rets):4d} cycles | 持仓期均值 {rets.mean()*100:+.2f}% | '
              f'中位 {np.median(rets)*100:+.2f}% | 胜率 {(rets>0).mean()*100:.1f}%')
    else:
        print(f'{name:>8}: 0 cycles')
    if len(g):
        print(f'         : {len(g):4d} gaps(空仓缺口) | 缺口均值 {g.mean()*100:+.2f}% | '
              f'中位 {np.median(g)*100:+.2f}% | 缺口>0 占比 {(g>0).mean()*100:.1f}% | '
              f'缺口>2% 占比 {(g>0.02).mean()*100:.1f}% | 合计 {g.sum()*100:+.1f}pp')


def main():
    panel = pd.read_pickle(PANEL)
    daily = pd.read_pickle(CACHE)
    sub = panel[panel['score'].notna()][['date', 'code', 'score']]
    print(f'面板 member-day {len(sub)}（score 非缺失）', flush=True)

    results = {m: ([], []) for m in ('base', 'band', 'confirm')}
    n_codes = 0
    for code, g in sub.sort_values(['code', 'date']).groupby('code'):
        df = daily.get(code)
        if df is None or df.empty:
            continue
        close_of = df['close']
        g = g[g['date'].isin(close_of.index)]
        if len(g) < 10:
            continue
        n_codes += 1
        dates = g['date'].tolist()
        scores = g['score'].astype(float).tolist()
        closes = [float(close_of.loc[d]) for d in dates]
        for m in ('base', 'band', 'confirm'):
            cyc, gaps = run_cycles(dates, scores, closes, m)
            results[m][0].extend(cyc)
            results[m][1].extend(gaps)
    print(f'有序列的票 {n_codes} 只', flush=True)
    for m in ('base', 'band', 'confirm'):
        summarize(m, results[m][0], results[m][1])
    nb = len(results['base'][0])
    for m in ('band', 'confirm'):
        saved = nb - len(results[m][0])
        print(f'{m}: 削减 {saved} cycles（-{saved/nb*100:.0f}%）', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
