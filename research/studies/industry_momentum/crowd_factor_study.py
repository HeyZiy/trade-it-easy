# -*- coding: utf-8 -*-
# crowd_factor_study —— 量价热度因子检验（ETF 适配器）
# 面板中的 score/rank 及候选集依赖旧评分；使用前重建面板并复核口径。
# 方法：复用 single_factor 统计核，计算分组收益、IC、HAC t 与换手。
# 旧结果及裁决已移除；使用前核对评分口径、重建派生样本并重新验证。
# 旧收益、缓存 score/rank 与版本优劣不能作为修正评分的结论。

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.tools.single_factor.single_factor_test import (evaluate_period,
                                                             membership_turnover,
                                                             summarize_ic)

STUDY = Path(__file__).resolve().parent
PANEL = STUDY / 'reports' / 'crowd_fwd_v1' / 'panel.pkl'
OUT = STUDY / 'reports' / 'crowd_factor_v1'

VARIANTS = [('crowd', 'crowd'), ('share_pct', 'sp'), ('price_pct', 'cp')]
RUNS = [
    # (名称, 子集, horizon交易日, groups, min_ic_stocks)
    ('full_h20', 'full', 20, 5, 15),
    ('full_h60', 'full', 60, 5, 15),
    ('cand_h20', 'cand', 20, 3, 8),
    ('cand_h60', 'cand', 60, 3, 8),
]


def run_one(sig: pd.DataFrame, col: str, k: int, groups: int, min_ic: int):
    """sig=非重叠信号日截面集合；逐信号日过工具 evaluate_period。"""
    rows = []
    prev_members = {g: None for g in range(1, groups + 1)}
    for t, g in sig.groupby('date'):
        by_code = g.set_index('code')
        factor = by_code[col].astype(float)
        future = by_code[f'fwd{k}'].astype(float)
        stats, members = evaluate_period(factor, future, groups, min_ic)
        for grp in range(1, groups + 1):
            current = set(members.index[members == grp])
            stats['G%d_membership_turnover' % grp] = membership_turnover(
                prev_members[grp], current)
            prev_members[grp] = current or None
        stats['signal'] = t
        rows.append(stats)
    table = pd.DataFrame(rows).set_index('signal')
    return table


def report_one(name: str, table: pd.DataFrame, groups: int, hac_lags: int):
    lines = [f'\n### {name} | 完整期数 {len(table)}（IC 非缺测 '
             f'{int(table["ic"].notna().sum())}）']
    ic_sum = summarize_ic(table['ic'], hac_lags)
    lines.append(f"IC {ic_sum['mean']:+.4f} | ICIR {ic_sum['icir']:+.3f} | "
                 f"t_iid {ic_sum['t_iid']:+.2f} | t_hac {ic_sum['t_hac']:+.2f} | "
                 f"IC>0 占比 {ic_sum['positive_fraction'] * 100:.0f}%"
                 if ic_sum['n'] else 'IC 全缺测')
    cols = ['G%d' % g for g in range(1, groups + 1)]
    valid = table[cols].count()
    means = table[cols].mean()
    lines.append('组均值(期收益): ' + ' | '.join(
        f'G{g} {means[f"G{g}"] * 100:+.2f}%（n={int(valid[f"G{g}"])}）'
        for g in range(1, groups + 1)))
    hl = table['high_minus_low'].dropna()
    if len(hl):
        lines.append(f'高减低 均值 {hl.mean() * 100:+.2f}%/期 | '
                     f'为负占比 {(hl < 0).mean() * 100:.0f}%')
    lines.append(f'平均配对覆盖 {table["coverage"].mean() * 100:.1f}%')
    yearly = table.groupby(pd.DatetimeIndex(table.index).year)['ic']
    ysum = ['%s %+.3f(t%s)' % (y, s['mean'] if s['n'] else float('nan'),
                               f"{'%.2f' % s['t_iid']}" if s['n'] else 'NA')
            for y, rows in yearly for s in [summarize_ic(rows, hac_lags)]]
    lines.append('年度IC: ' + ' | '.join(ysum))
    return '\n'.join(lines), ic_sum


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    panel = pd.read_pickle(PANEL)
    summary_rows, report_lines = [], ['\n# 量价热度因子检验（工具口径）']
    for name, subset, k, groups, min_ic in RUNS:
        base = panel[panel['is_cand']] if subset == 'cand' else panel
        sig = base[base['pos'] % k == 0]
        for variant, col in VARIANTS:
            table = run_one(sig, col, k, groups, min_ic)
            key = f'{name}_{variant}'
            table.to_csv(OUT / f'periods_{key}.csv', encoding='utf-8-sig')
            text, ic_sum = report_one(key, table, groups, 3)
            report_lines.append(text)
            summary_rows.append({
                'run': key, 'subset': subset, 'horizon': k, 'variant': variant,
                'periods': len(table), 'ic_periods': int(table['ic'].notna().sum()),
                'ic_mean': ic_sum['mean'], 'icir': ic_sum['icir'],
                't_iid': ic_sum['t_iid'], 't_hac': ic_sum['t_hac'],
                'ic_positive_pct': ic_sum['positive_fraction'],
                'hl_mean': table['high_minus_low'].mean(),
                'coverage_mean': table['coverage'].mean(),
            })
            print(f'{key}: IC={ic_sum["mean"]:+.4f} t_hac={ic_sum["t_hac"]:+.2f} '
                  f'(期 {len(table)})', flush=True)
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT / 'summary.csv', index=False, encoding='utf-8-sig')
    (OUT / 'report.txt').write_text('\n'.join(report_lines), encoding='utf-8')
    print('\n'.join(report_lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
