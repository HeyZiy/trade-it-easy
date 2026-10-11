# -*- coding: utf-8 -*-
"""v4_1_5 vs v4_1_2 纯归因分解。

基线为 v4_1_2 定版导出（2026-10-11 下载）。v4_1_5 在 v4_1_2 基础上的唯一规则差异
是 [顺延补位+删除整轮暂停]，两版差异可纯归因于该改动，不再混杂取消留存线。

只读本地导出与行情缓存，不回测不下单。回答三个问题：
1) 两版分年收益差集中在哪些年份；
2) 组合路径从哪一轮开始分叉、共分叉多少轮；
3) v4_1_5 相对 v4_1_2 的新增/消失持仓的规模与损益分布。

运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_v415_vs_v412.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STUDY = HERE.parent
ROOT = STUDY.parents[2]
CACHE = ROOT / 'research/studies/roe_quality/_cache/pool'
EXPORTS = STUDY / 'exports'
OUT = STUDY / 'reports' / 'v415_vs_v412_pure_2026-10-11'
REPORT = STUDY / 'reports' / 'v415_vs_v412_pure_2026-10-11.md'

NAV12 = EXPORTS / 'pool_rotation_v4_1_2_nav.csv'
NAV15 = EXPORTS / 'pool_rotation_v4_1_5_nav.csv'
HP12 = EXPORTS / 'pool_rotation_v4_1_2_holding_periods.csv'
HP15 = EXPORTS / 'pool_rotation_v4_1_5_holding_periods.csv'
MEM12 = EXPORTS / 'pool_rotation_v4_1_2_members.csv'
MEM15 = EXPORTS / 'pool_rotation_v4_1_5_members.csv'


def require(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


def annual_returns(nav):
    nav = nav.copy()
    nav['date'] = pd.to_datetime(nav['date'])
    nav = nav.set_index('date').sort_index()
    equity = nav['equity']
    years = sorted(set(equity.index.year))
    rows, prev = [], None
    for year in years:
        end = equity[equity.index.year == year].iloc[-1]
        start = prev if prev is not None else 100000.0
        rows.append({'year': year, 'ret': end / start - 1.0})
        prev = end
    return pd.DataFrame(rows).set_index('year')


def hfq_close(code):
    path = CACHE / f'bars_hfq_{code[:6]}.pkl'
    if not path.exists():
        return None
    frame = pd.read_pickle(path)
    dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
    series = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(), index=dates)
    return series[~series.index.duplicated(keep='last')].sort_index()


def episode_return(code, entry, exit_):
    close = hfq_close(code)
    if close is None or pd.isna(exit_):
        return np.nan
    try:
        return float(close.asof(exit_) / close.asof(entry) - 1.0)
    except KeyError:
        return np.nan


def main():
    nav12, nav15 = pd.read_csv(NAV12), pd.read_csv(NAV15)
    hp12, hp15 = pd.read_csv(HP12), pd.read_csv(HP15)
    mem12, mem15 = pd.read_csv(MEM12), pd.read_csv(MEM15)
    annual = pd.DataFrame({'v4_1_2': annual_returns(nav12)['ret'],
                           'v4_1_5': annual_returns(nav15)['ret']})
    annual['diff_pp'] = (annual['v4_1_5'] - annual['v4_1_2']) * 100

    sel12 = mem12.loc[mem12.selected.eq(True), ['signal_day', 'code']].assign(v12=True)
    sel15 = mem15.loc[mem15.selected.eq(True), ['signal_day', 'code']].assign(v15=True)
    both = sel12.merge(sel15, on=['signal_day', 'code'], how='outer').fillna(False)
    per_day = both.groupby('signal_day').apply(lambda g: pd.Series({
        'v12': int(g.v12.sum()), 'v15': int(g.v15.sum()),
        'added': int((g.v15 & ~g.v12).sum()),
        'dropped': int((g.v12 & ~g.v15).sum())}), include_groups=False)
    forked = per_day[(per_day.added > 0) | (per_day.dropped > 0)]
    first_fork = forked.index[0] if len(forked) else ''

    key = ['code', 'entry_date']
    hp12k = hp12[key].assign(v12=True)
    hp15k = hp15[key].assign(v15=True)
    ep = hp12k.merge(hp15k, on=key, how='outer').fillna(False)
    added = ep[ep.v15 & ~ep.v12].drop(columns=['v12', 'v15'])
    dropped = ep[ep.v12 & ~ep.v15].drop(columns=['v12', 'v15'])
    added = added.merge(hp15[key + ['exit_date', 'holding_days', 'exit_reason']],
                        on=key, how='left')
    added['ret'] = [episode_return(c, pd.Timestamp(e), pd.Timestamp(x))
                    for c, e, x in zip(added.code, added.entry_date, added.exit_date)]
    added['year'] = pd.to_datetime(added.entry_date).dt.year

    OUT.mkdir(exist_ok=True)
    per_day.to_csv(OUT / 'per_round_diff.csv')
    added.sort_values('ret').to_csv(OUT / 'v15_added_episodes.csv', index=False)
    annual.round(4).to_csv(OUT / 'annual_diff.csv')

    valid = added.ret.dropna()
    lines = [
        '# v4_1_5 vs v4_1_2 差距分解（2026-10-11，纯归因顺延补位）', '',
        '基线为 v4_1_2 定版导出；v4_1_5 唯一规则差异是 [顺延补位+删除整轮暂停]，'
        '差异纯归因于该改动。顺延新增持仓收益用后复权收盘近似。', '',
        '## 分年收益差（v4_1_5 − v4_1_2，百分点）', '',
        annual.round(4).to_markdown(), '',
        f'## 组合分叉（v4_1_5 vs v4_1_2）', '',
        f'- 分叉轮数：{len(forked)}/{per_day.shape[0]}；首个分叉轮：{first_fork}',
        f'- v4_1_5 相对 v4_1_2 新增持仓：{len(added)} 笔（{added.code.nunique()} 只股票）；'
        f'消失持仓：{len(dropped)} 笔', '',
        '## 新增持仓的收益分布（后复权收盘近似）', '',
        f'- n={len(valid)}（缺失缓存 {added.ret.isna().sum()} 笔）',
        f'- 中位 {valid.median():+.2%}；均值 {valid.mean():+.2%}；'
        f'胜率 {(valid > 0).mean():.1%}', '',
        f'- 最差5笔：{", ".join(f"{r.code[:6]}({r.ret:+.0%})" for r in added.dropna(subset=["ret"]).nsmallest(5, "ret").itertuples())}',
        f'- 最好3笔：{", ".join(f"{r.code[:6]}({r.ret:+.0%})" for r in added.dropna(subset=["ret"]).nlargest(3, "ret").itertuples())}',
        f'- 亏损≤-15%笔数：{(valid <= -0.15).sum()}；亏损笔数：{(valid < 0).sum()}', '',
        '### 按入场年汇总', '',
        added.dropna(subset=['ret']).groupby('year')['ret'].agg(['count', 'median', 'mean']).round(4).to_markdown(), '',
        '## 基线说明', '',
        '基线为 v4_1_2 定版导出（收益216.36%可证）。早期报告 v415_vs_v41_decomp_2026-10-10 '
        '因本地缺 v4_1_2 导出而用 v4_1 基线，混杂 [取消留存线] 与 [顺延补位+删除暂停] '
        '两个变量，仅作存档对照。', '',
    ]
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print('分年差:', {int(y): round(v, 1) for y, v in annual.diff_pp.items()})
    print(f'分叉轮 {len(forked)}/{per_day.shape[0]} 首轮 {first_fork}; 新增持仓 {len(added)} 笔, 消失 {len(dropped)} 笔')
    print(f'新增持仓收益: 中位 {valid.median():+.2%} 均值 {valid.mean():+.2%} 胜率 {(valid>0).mean():.0%} '
          f'≤-15% {(valid<=-0.15).sum()}笔')
    print(added.dropna(subset=['ret']).groupby('year')['ret'].agg(['count', 'median']).round(3))


if __name__ == '__main__':
    main()
