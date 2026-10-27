# -*- coding: utf-8 -*-
"""score25 状态细分：反转频率 + 状态条件的前瞻收益。

问题：符号门（score>0 持有）只用了分数的 1 比特。本脚本先量"每天多少只
反转"，再按预注册状态细分看 5/10/20 日前瞻收益是否分层：
- 刚转正：今日>0 且昨日≤0
- 大正：>0 且 ≥ 自身正分数 expanding 分位 80%
- 老正：其余正数日
- 跳水清零：score==0（momentum_score 的 dive 否决）
- 刚转负：今日<0 且昨日>0
- 老负：其余负数日

口径：pool37 乘法复权收盘算分（同 study_timing_first_round，引擎单点
src/etf/momentum.py）；分位只用截至当日的自身历史（无前视）；前瞻收益为
毛收益路径（不含费），窗口重叠、时间聚集，描述性对照不做显著性断言。

从仓库根运行：python research/studies/etf_timing/study_score_states.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from src.etf.momentum import momentum_score  # noqa: E402

MARKET = ROOT / 'research' / 'studies' / 'industry_momentum' / '_cache' \
    / 'pool37_event_adjusted_20260930' / 'market.csv'
RESULTS = HERE / 'results' / 'score_states'

INDUSTRY22 = {
    '515170', '512660', '512690', '512400', '159928', '515220', '512010',
    '515210', '512170', '159870', '512800', '159996', '512000', '512200',
    '512480', '159611', '515880', '516110', '512720', '512980', '515030',
    '515790',
}
STRONG_PCT = 0.8
HORIZONS = (5, 10, 20)


def score_series(px: pd.Series) -> pd.Series:
    vals = px.values
    out = np.full(len(vals), np.nan)
    for i in range(25, len(vals)):
        out[i] = momentum_score(vals[i - 25:i], vals[i])
    return pd.Series(out, index=px.index)


def states_for(sc: pd.Series) -> pd.Series:
    """六态标注；分位只用截至当日（含）的自身正分数历史。"""
    sign = np.sign(sc.values)
    states = np.full(len(sc), '', dtype=object)
    pos_hist = []
    for i in range(len(sc)):
        v = sc.iloc[i]
        if pd.isna(v):
            continue
        prev_pos = i > 0 and pd.notna(sc.iloc[i - 1]) and sc.iloc[i - 1] > 0
        if v == 0:
            states[i] = '跳水清零'
        elif v > 0:
            if not prev_pos:
                states[i] = '刚转正'
            elif len(pos_hist) >= 60 and v >= pd.Series(pos_hist).quantile(STRONG_PCT):
                states[i] = '大正'
            else:
                states[i] = '老正'
            pos_hist.append(v)
        else:
            states[i] = '刚转负' if prev_pos else '老负'
    return pd.Series(states, index=sc.index)


def main():
    mk = pd.read_csv(MARKET, dtype={'code': str})
    mk['code'] = mk['code'].str.zfill(6)
    mk['date'] = pd.to_datetime(mk['date']).dt.strftime('%Y%m%d').astype(int)

    frames, flip_days = [], {}
    for code, g in mk.groupby('code'):
        px = g.sort_values('date').set_index('date')['event_close'].astype(float)
        px = px[px > 0]
        if len(px) < 300:
            continue
        sc = score_series(px)
        st = states_for(sc)
        sign = np.sign(sc)
        flips = (sign.diff().abs() > 0) & sign.notna() & sign.shift(1).notna()
        flip_days[code] = flips
        fwd = {h: px.shift(-h) / px - 1 for h in HORIZONS}
        frames.append(pd.DataFrame({
            'date': px.index, 'code': code,
            'cls': '行业' if code in INDUSTRY22 else '跨资产',
            'state': st.values,
            **{f'fwd{h}': fwd[h].values for h in HORIZONS}}))

    df = pd.concat(frames, ignore_index=True)
    df = df[df['state'] != '']

    # 1) 反转频率：逐日全市场反转只数（在市只数 = 该日有状态的只数）
    flip_mat = pd.DataFrame(flip_days).fillna(False)
    per_day = flip_mat.sum(axis=1)
    active = df.groupby('date')['code'].nunique()
    per_day = per_day.reindex(active.index).fillna(0)
    stats = {
        '在市ETF_期末': int(active.iloc[-1]),
        '日均反转只数': float(per_day.mean()),
        '日均反转比例': float((per_day / active).mean()),
        '单只平均反转间隔_日': float(active.sum() / per_day.sum()),
    }
    print('== 反转频率 ==')
    for k, v in stats.items():
        print(f'{k}: {v:.2f}' if isinstance(v, float) else f'{k}: {v}')

    # 2) 状态 × 前瞻收益
    RESULTS.mkdir(parents=True, exist_ok=True)
    for scope, sub in (('全部', df), ('行业', df[df['cls'] == '行业']),
                       ('跨资产', df[df['cls'] == '跨资产'])):
        print(f'\n== {scope}：状态 × 前瞻收益（%，均值/中位 | n）==')
        tab = pd.DataFrame({
            f'{h}日': sub.groupby('state')[f'fwd{h}'].agg(
                lambda s: f'{s.mean()*100:+.2f}/{s.median()*100:+.2f}')
            for h in HORIZONS})
        tab['n'] = sub.groupby('state').size()
        order = ['刚转正', '大正', '老正', '跳水清零', '刚转负', '老负']
        print(tab.reindex([o for o in order if o in tab.index]).to_string())
    df.to_csv(RESULTS / 'states_daily.csv', index=False, encoding='utf-8-sig')
    with open(RESULTS / 'flip_stats.json', 'w', encoding='utf-8') as f:
        import json
        json.dump(stats, f, ensure_ascii=False, indent=2, default=float)
    print(f'\n结果写入 {RESULTS}')


if __name__ == '__main__':
    main()
