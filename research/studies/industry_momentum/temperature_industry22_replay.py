# -*- coding: utf-8 -*-
"""纯行业 22 只池 × 温度闸重放：固定行业池排名轮动的条件准入对照。

背景：固定 22 只行业池的独立排名轮动从未在平台跑过（EXPERIMENTS.md 仅登记
入口）；本脚本用六账户真实信号在本地重放该池（无闸基线即首次得到这一读数），
并加温度闸单变量对照。口径与对账同 temperature_gate_replay.py：合格=
status=='eligible'，行业池内排名由 score 降序现算（先对 37 只全体验证
score 序与平台 rank37 序一致），乘法复权空间、万二最低 5 元、整手、
决策价缺失继续持有/持币；温度缺失视为被闸；全行业被闸或无合格时持币。

从仓库根运行：python research/studies/industry_momentum/temperature_industry22_replay.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.industry_momentum.temperature_gate_replay import (
    INDUSTRY22, load_inputs, metrics, replay, yearly)

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results' / 'temperature' / 'industry22'


def check_rank_consistency(sig_by_day) -> float:
    """score 降序是否复现平台 rank37 升序（逐日全序列比对）。"""
    match, total = 0, 0
    for d, s in sig_by_day.items():
        e = s[(s['status'] == 'eligible') & s['rank37'].notna()]
        if e.empty:
            continue
        by_rank = e.sort_values('rank37')['code6'].tolist()
        by_score = e.sort_values('score', ascending=False)['code6'].tolist()
        total += 1
        match += int(by_rank == by_score)
    return match / max(total, 1)


def main():
    sig, px, fac, temp, dec, nav = load_inputs()
    days = sorted(sig['date'].unique())
    sig_by_day = {d: g for d, g in sig.groupby('date')}

    consist = check_rank_consistency(sig_by_day)
    print(f'score 降序 vs 平台 rank37 序一致率：{consist:.2%}')
    if consist < 0.99:
        raise SystemExit('score 排序与平台 rank 不一致，先修口径再跑')

    variants = [('纯行业22_无闸', dict(gate=None, pool='22')),
                ('资格闸<0.5', dict(gate=0.5, pool='22')),
                ('资格闸<2/3', dict(gate=2 / 3, pool='22')),
                ('资格闸<0.8', dict(gate=0.8, pool='22')),
                ('资格闸<0.9', dict(gate=0.9, pool='22')),
                ('仅入场闸<2/3', dict(gate=2 / 3, pool='22', gate_entry_only=True))]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out, navs = {}, {}
    for name, kw in variants:
        path, trades = replay(days, sig_by_day, px, fac, temp, **kw)
        nv = path['nav'].ffill()
        out[name] = {'metrics': metrics(nv), 'yearly': yearly(nv, path['date']),
                     'n_trades': len(trades),
                     'cash_days': int(path['holding'].isna().sum())}
        navs[name] = path
        print(f"\n== {name} ==")
        print({k: round(v, 2) for k, v in out[name]['metrics'].items()},
              f"| 交易 {len(trades)} | 持币天数 {out[name]['cash_days']}/{len(path)}")

    tab = pd.DataFrame({k: out[k]['yearly'] for k in
                        ('纯行业22_无闸', '资格闸<2/3', '资格闸<0.5')})
    print('\n== 分年收益（%，重放口径）==')
    print(tab.to_string())

    out['口径'] = {'池': '固定22只行业', '排名': 'score 降序现算（与 rank37 一致率 %.2f）' % consist,
                 '闸': 'temp_pct<阈值 才可当目标；缺失视为被闸；无合格/全被闸=持币',
                 '对账': '纯行业池无平台账户，无闸基线为本地首跑读数'}
    pd.DataFrame({k: v.set_index('date')['nav'] for k, v in navs.items()}).to_csv(
        RESULTS / 'nav_series.csv', encoding='utf-8-sig')
    with open(RESULTS / 'replay_stats.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(f'\n结果写入 {RESULTS}')


if __name__ == '__main__':
    main()
