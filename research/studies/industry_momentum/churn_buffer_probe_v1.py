# -*- coding: utf-8 -*-
# churn_buffer_probe_v1 —— score 0 轴震荡的绞肉税测量（只读本地缓存，2026-10-06）
#
# 问题：v3_3 的 score≤0 出场产生 104 笔（排名版 59 笔），0 轴震荡的反复进出
#   是系统性亏钱（卖低买高）还是噪声？缓冲（负带/确认日）值不值得一次平台
#   单变量？
# 测法：用 crowd_fwd_v1 面板（生产 momentum_score 逐日 score，与 build_rows
#   对账 0 不一致）+ 本地 qfq 收盘，对每只票的 member-day 序列跑状态机：
#   entry = score>0 的首个 member-day，exit = 此后触发出场条件的首日，
#   cycle 收益 = close(exit)/close(entry) - 1（qfq 比值，与平台复权口径一致）。
#   三个口径：base（score≤0 即卖）/ band（score<-0.05 才卖）/ confirm（连续
#   3 个 member-day score≤0 才卖）。
# 判读（事先约定）：base 的 cycle 均值系统性为负、且 band/confirm 能把均值
#   抬近 0 或把笔数砍半以上 → 缓冲值得平台单变量；base ≈0 或缓冲无改善 →
#   缓冲就地结案。
# 局限：member-day 序列是无条件截面统计（非 v3_3 真实持仓路径），测的是 0 轴
#   穿越现象本身；平台单变量才是终审。面板池为 2020-07 起无种子口径，样本
#   比 v3_3 的 2024-09 窗宽，结论按"现象是否存在"读。
#
# 结果头注（2026-10-06 实测，108 票 / 1,317 cycles / 1,242 gaps）：
#   ① 绞肉税存在但温和：score≤0 卖出 → score>0 买回之间的空仓缺口，均值
#     **+0.30%**、中位 +0.61%、59.2% 为正——卖低买高的系统性漂移是真的，
#     但单次量级小。持仓期 cycle 均值 +0.39%（34% 胜率、右偏）——0 轴穿越
#     本身不亏钱，亏的是缺口。
#   ② 两种缓冲分出胜负：**负带（score<-0.05）被否**——每缺口税恶化到
#     +0.47%、cycle 中位恶化（-1.15%→-2.58%），等于把每次出场切得更深；
#     **确认 3 日是三轴全不劣化的免费改善**——cycles 削 28%（1317→945）、
#     单 cycle 捕获 +0.39%→+0.58%、每缺口税不变（+0.24%）。
#   ③ 量级判断：本地可见改善是摩擦级（每 cycle +0.19pp、笔数 -28%），组合
#     层换算大概率 ±1~2pp——值得搭车平台验证（v3_3_1：score 出场加 3 日
#     确认，-8% 限亏不延迟），不值得单独立项。

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
