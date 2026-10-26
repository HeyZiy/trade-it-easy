# -*- coding: utf-8 -*-
"""温度计条件检验：行业入场的后续亏损是否集中在 ETF/成分股成交额比高位。

样本：mixed37 固定窗口配对面板（fixed_forward_events.csv）中 22 只行业 ETF
的入场事件；excess_return = 行业固定窗口毛收益 − 原池当日合格第一名同窗收益。

温度值：temperature_build.py 面板的 temp_pct（截至入场前一日的 20 日比值
expanding 分位，min_obs=120）。主检验按分位三分桶 + 前十分位，看
excess_return 均值是否随温度递减、亏损是否集中在高桶。

已知边界：分母为申万时点成分（多指数并集去重），与 ETF 跟踪指数口径存在
ETF 间常数性偏差——横截只用自身历史分位；涨停为收盘=最高且涨幅过阈值的
近似（ST 5% 漏计）；事件窗口重叠、时间聚集，本检验为描述性条件对照，不做
显著性断言，稳定性用逐年剔除与分期呈现。

从仓库根运行：python research/studies/etf_rotation/study_temperature_gauge.py
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results' / 'temperature'
PANEL_CACHE = HERE / '_cache' / 'temperature_panel.pkl'
EVENTS = HERE / 'results' / 'mixed37_comparison' / 'fixed_forward_events.csv'


def load_joined() -> pd.DataFrame:
    with open(PANEL_CACHE, 'rb') as f:
        panel = pickle.load(f)
    ev = pd.read_csv(EVENTS, dtype={'code': str})
    ev['code'] = ev['code'].str.zfill(6)
    ev['date_int'] = pd.to_datetime(ev['entry']).dt.strftime('%Y%m%d').astype(int)

    rows = []
    for (entry, code), grp in ev.groupby(['entry', 'code']):
        if code not in panel:
            continue
        p = panel[code]
        day = grp['date_int'].iloc[0]
        if day not in p.index:
            continue
        r = p.loc[day]
        prev = p['ratio_20'].iloc[:p.index.get_loc(day)].tail(1)
        rows.append({
            'entry': entry, 'code': code, 'date_int': day,
            'temp_pct': r['temp_pct'],
            'ratio_20_prev': float(prev.iloc[0]) if len(prev) else np.nan,
            'n_members': r['n_members'], 'n_near_limit': r['n_near_limit'],
            'near_limit_share': (r['n_near_limit'] / r['n_members']
                                 if r['n_members'] else np.nan),
            'etf_amount': r['etf_amount'],
            **{f'excess_h{h}': g['excess_return'].iloc[0]
               for h, g in grp.groupby('horizon')},
            **{f'ind_h{h}': g['industry_return'].iloc[0]
               for h, g in grp.groupby('horizon')},
        })
    df = pd.DataFrame(rows)
    df['year'] = pd.to_datetime(df['entry']).dt.year
    return df


def bucketize(v: float) -> str:
    if pd.isna(v):
        return 'na'
    if v >= 0.9:
        return 'top10'
    if v >= 2 / 3:
        return 'high'
    if v >= 1 / 3:
        return 'mid'
    return 'low'


ORDER = ['low', 'mid', 'high', 'top10']


def bucket_stats(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    col = f'excess_h{horizon}'
    out = []
    for b in ORDER:
        s = df[df['bucket'] == b][col].dropna()
        if s.empty:
            continue
        out.append({
            'bucket': b, 'n': len(s),
            'mean_excess_pp': s.mean() * 100,
            'median_excess_pp': s.median() * 100,
            'win_rate': (s > 0).mean(),
            'mean_industry_pp': df[df['bucket'] == b][f'ind_h{horizon}'].mean() * 100,
        })
    return pd.DataFrame(out)


def stability(df: pd.DataFrame, horizon: int) -> dict:
    col = f'excess_h{horizon}'
    sub = df.dropna(subset=[col, 'temp_pct'])
    hi, lo = sub[sub['temp_pct'] >= 2 / 3][col], sub[sub['temp_pct'] < 1 / 3][col]
    diff_full = hi.mean() - lo.mean()
    loyo = {}
    for y in sorted(sub['year'].unique()):
        keep = sub[sub['year'] != y]
        h = keep[keep['temp_pct'] >= 2 / 3][col]
        l = keep[keep['temp_pct'] < 1 / 3][col]
        if len(h) >= 5 and len(l) >= 5:
            loyo[int(y)] = (h.mean() - l.mean()) * 100
    half = {}
    for name, m in (('2016-2020', sub['year'] <= 2020), ('2021-2026', sub['year'] >= 2021)):
        s = sub[m]
        h, l = s[s['temp_pct'] >= 2 / 3][col], s[s['temp_pct'] < 1 / 3][col]
        if len(h) >= 5 and len(l) >= 5:
            half[name] = {'n_hi': len(h), 'n_lo': len(l),
                          'diff_pp': (h.mean() - l.mean()) * 100}
    return {'high_minus_low_pp': diff_full * 100, 'leave_one_year_out_pp': loyo,
            'period_split': half}


def main():
    df = load_joined()
    df['bucket'] = df['temp_pct'].apply(bucketize)
    total_ev = len(df)
    with_temp = df['temp_pct'].notna().sum()
    print(f'事件 {total_ev}，有温度值 {with_temp}（缺温度=上市未满 {120} 日）')

    RESULTS.mkdir(parents=True, exist_ok=True)
    df.to_csv(RESULTS / 'events.csv', index=False, encoding='utf-8-sig')

    summary = {'n_events': int(total_ev), 'n_with_temp': int(with_temp)}
    for h in (5, 10, 20):
        stats = bucket_stats(df, h)
        stats.to_csv(RESULTS / f'buckets_h{h}.csv', index=False,
                     encoding='utf-8-sig')
        print(f'\n== 固定 {h} 日窗口：excess（百分点）按温度桶 ==')
        print(stats.to_string(index=False,
                              formatters={'mean_excess_pp': '{:+.2f}'.format,
                                          'median_excess_pp': '{:+.2f}'.format,
                                          'win_rate': '{:.1%}'.format,
                                          'mean_industry_pp': '{:+.2f}'.format}))
        summary[f'h{h}'] = {'buckets': stats.to_dict('records'),
                            'stability': stability(df, h)}

    # 混淆：入场日近涨停占比按桶分布；剔除高涨停日后复算 10 日主检验
    print('\n== 入场日近涨停占比（中位）按桶 ==')
    print(df.groupby('bucket')['near_limit_share'].median().to_string())
    lim_mask = df['near_limit_share'].fillna(0) < 0.05
    sub = df[lim_mask]
    print('\n== 剔除近涨停占比>=5% 的事件后，10 日主检验 ==')
    print(bucket_stats(sub, 10).to_string(index=False,
          formatters={'mean_excess_pp': '{:+.2f}'.format,
                      'median_excess_pp': '{:+.2f}'.format,
                      'win_rate': '{:.1%}'.format,
                      'mean_industry_pp': '{:+.2f}'.format}))
    summary['h10_excl_limit5'] = bucket_stats(sub, 10).to_dict('records')
    summary['h10_stability_excl_limit5'] = stability(sub, 10)

    # 高桶成分：哪些 ETF 贡献了高温事件
    hi = df[df['bucket'].isin(['high', 'top10'])]
    summary['high_bucket_by_etf'] = (
        hi.groupby('code').size().sort_values(ascending=False).to_dict())

    with open(RESULTS / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=float)
    print(f'\n结果写入 {RESULTS}')


if __name__ == '__main__':
    main()
