# -*- coding: utf-8 -*-
"""估值对照（经典因子第三战）：双闸的行业选择是否只是估值效应。

研究问题：低温×盈利广度正单元是否集中于低估值分位、高温亏损是否
集中于高估值分位？若控制估值后双闸读数消失，机制叙事让位于
"买便宜卖贵"的估值解释。

预注册设计（2026-10-26 定稿，先于取数）：
- 样本：温度计 352 次有温度行业事件（events.csv，同前两战）。
- pe_pct：valuation_panel.pkl 的 t 日 PE 分位（面板构建已 shift(1)，
  仅用 t-1 前历史；min_obs=250，2016 年内事件为 NaN 自然落空）。
- 检验 C1：pe_pct 与 temp/breadth/excess_h10 的 Spearman（混杂总量）。
- 检验 C2：pe_pct 三分位（边界一次计算）的 excess_h10——估值梯度。
- 检验 C3：正单元（低温×赞助）的 pe_pct 分布（中位/均值 vs 其余事件）
  及正单元内 pe 高低半区的 excess_h10。
- 检验 C4（裁决）：pe_pct 三分位内"正单元 vs 其余事件"的 excess_h10
  差。估值解释成立 = C2 单调 且 C4 在 ≥2/3 层失去原符号或差值缩水过
  半；机制解释成立 = C4 各层保号。
- 边界：股本回溯近似（多年未变动只有一条快照记录，首条记录前按
  不变回填，增发股历史市值被高估）；分位 min_obs=250 导致早期缺值；
  事件重叠、时间聚集、样本内。

从仓库根运行：python research/studies/industry_momentum/study_valuation.py
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EVENTS_CSV = HERE / 'results' / 'temperature' / 'events.csv'
SPONSOR_CSV = HERE / 'results' / 'sponsorship' / 'events.csv'
PANEL_CACHE = HERE / '_cache' / 'valuation_panel.pkl'
OUT_DIR = HERE / 'results' / 'valuation'

TEMP_HIGH = 2 / 3
BREADTH_SPLIT = 0.5


def run_self_tests() -> int:
    n = 0

    def check(name, cond):
        nonlocal n
        assert cond, f'自检失败: {name}'
        n += 1

    # 面板索引与分位语义抽查：pe_pct ∈ [0,1]、2016 年内为 NaN（min_obs 未满）
    with open(PANEL_CACHE, 'rb') as f:
        panel = pickle.load(f)
    df = panel['512800']
    check('索引为 int 交易日', str(df.index[0]).isdigit() and len(str(df.index[0])) == 8)
    pct16 = df.loc[(df.index >= 20160101) & (df.index <= 20161231), 'pe_pct']
    check('2016 分位全缺（min_obs 未满）', pct16.notna().sum() == 0)
    valid = df['pe_pct'].dropna()
    check('分位在[0,1]', valid.min() >= 0 and valid.max() <= 1)
    # 单元格均值函数行为
    s = pd.DataFrame({'excess_h10': [1.0, 2.0, np.nan, -1.0]})
    m, c = _cell_mean(s)
    check('cell_mean 跳过 NaN', abs(m - 0.67) < 1e-9 and c == 3)  # round(2/3, 2)
    print(f'自检 {n} 项通过', flush=True)
    return n


def _cell_mean(df: pd.DataFrame):
    x = df['excess_h10'].dropna()
    return (round(float(x.mean()), 2), int(len(x))) if len(x) else (None, 0)


def main():
    n_tests = run_self_tests()

    ev = pd.read_csv(EVENTS_CSV, encoding='utf-8-sig', dtype={'code': str})
    ev['code'] = ev['code'].str.zfill(6)
    ev['date_int'] = ev['date_int'].astype(int)
    ev = ev[ev['temp_pct'].notna()].copy()
    sp = pd.read_csv(SPONSOR_CSV, encoding='utf-8-sig', dtype={'code': str})
    sp['code'] = sp['code'].str.zfill(6)
    sp['date_int'] = sp['date_int'].astype(int)
    ev = ev.merge(sp[['code', 'date_int', 'breadth_sq']], on=['code', 'date_int'],
                  how='left')
    ev['excess_h10'] = pd.to_numeric(ev['excess_h10'], errors='coerce') * 100

    with open(PANEL_CACHE, 'rb') as f:
        panel = pickle.load(f)
    pe_pct = []
    for _, r in ev.iterrows():
        df = panel.get(r['code'])
        v = df['pe_pct'].get(int(r['date_int']), np.nan) if df is not None else np.nan
        pe_pct.append(float(v) if pd.notna(v) else np.nan)
    ev['pe_pct'] = pe_pct

    ev = ev[ev['pe_pct'].notna()].copy()
    ev['is_pos'] = (ev['temp_pct'] < TEMP_HIGH) & (ev['breadth_sq'] >= BREADTH_SPLIT)

    out = {'events_with_pe_pct': int(len(ev)),
           'pos_cells': int(ev['is_pos'].sum()), 'self_tests_passed': n_tests}

    # C1 混杂总量
    out['C1_spearman'] = {
        'pe_vs_temp': round(float(ev[['pe_pct', 'temp_pct']].corr('spearman').iloc[0, 1]), 3),
        'pe_vs_breadth': round(float(ev[['pe_pct', 'breadth_sq']].corr('spearman').iloc[0, 1]), 3),
        'pe_vs_excess': round(float(ev[['pe_pct', 'excess_h10']].corr('spearman').iloc[0, 1]), 3)}

    # C2 估值三分位梯度
    q1, q2 = ev['pe_pct'].quantile([1 / 3, 2 / 3])
    out['C2_pe三分位梯度'] = {}
    for name, m in (('低', ev['pe_pct'] <= q1), ('中', (ev['pe_pct'] > q1) & (ev['pe_pct'] <= q2)),
                    ('高', ev['pe_pct'] > q2)):
        mu, cnt = _cell_mean(ev[m])
        out['C2_pe三分位梯度'][name] = {'n': int(m.sum()), 'excess_h10': mu}

    # C3 正单元的估值分布 + 单元内估值切分
    pos = ev[ev['is_pos']]
    out['C3_正单元估值分布'] = {
        '正单元_pe中位': round(float(pos['pe_pct'].median()), 3),
        '其余事件_pe中位': round(float(ev.loc[~ev['is_pos'], 'pe_pct'].median()), 3),
        '正单元_pe均值': round(float(pos['pe_pct'].mean()), 3),
        '其余事件_pe均值': round(float(ev.loc[~ev['is_pos'], 'pe_pct'].mean()), 3)}
    med_pe = pos['pe_pct'].median()
    hi_pe, n_hi = _cell_mean(pos[pos['pe_pct'] > med_pe])
    lo_pe, n_lo = _cell_mean(pos[pos['pe_pct'] <= med_pe])
    out['C3_正单元内估值切分'] = {'贵半区': hi_pe, 'n': n_hi, '便宜半区': lo_pe, 'n2': n_lo}

    # C4 裁决：估值层内 正单元 vs 其余
    out['C4_pe三分位内正单元差'] = {}
    for name, m in (('低', ev['pe_pct'] <= q1), ('中', (ev['pe_pct'] > q1) & (ev['pe_pct'] <= q2)),
                    ('高', ev['pe_pct'] > q2)):
        s = ev[m]
        mu_pos, n_pos = _cell_mean(s[s['is_pos']])
        mu_rest, n_rest = _cell_mean(s[~s['is_pos']])
        out['C4_pe三分位内正单元差'][name] = {
            'n': int(m.sum()), '正单元': mu_pos, 'n_pos': n_pos,
            '其余': mu_rest, 'n_rest': n_rest,
            '差_pp': round(mu_pos - mu_rest, 2) if mu_pos is not None and mu_rest is not None else None}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ev.to_csv(OUT_DIR / 'events_valuation.csv', index=False)
    with open(OUT_DIR / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=float))
    print(f'\n结果写入 {OUT_DIR}')


if __name__ == '__main__':
    main()
