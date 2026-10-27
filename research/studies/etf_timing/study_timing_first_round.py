# -*- coding: utf-8 -*-
"""单 ETF 时序信号第一轮：六族信号 × 37 只 ETF × 同窗买入持有基准。

口径与判据见本目录 README.md（先定后测）。要点：t 日收盘算信号、仓位对
t→t+1 生效；单边 2bp；公共窗口 = 数据起点后 250 根；分数族复用
src/etf/momentum.py 单点引擎；温度族仅 22 只行业可算，其余记 n/a。

从仓库根运行：python research/studies/etf_timing/study_timing_first_round.py
"""
from __future__ import annotations

import json
import pickle
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
PANEL = ROOT / 'research' / 'studies' / 'industry_momentum' / '_cache' \
    / 'temperature_panel.pkl'
RESULTS = HERE / 'results' / 'first_round'

INDUSTRY22 = {
    '515170', '512660', '512690', '512400', '159928', '515220', '512010',
    '515210', '512170', '159870', '512800', '159996', '512000', '512200',
    '512480', '159611', '515880', '516110', '512720', '512980', '515030',
    '515790',
}

WARMUP = 250
FEE = 0.0002
VOL_TARGET, VOL_BAND, VOL_WIN = 0.20, 0.2, 20
TEMP_EXIT = 0.9


def strong_entry_pos(scores: np.ndarray, q: float) -> np.ndarray:
    """空仓时需 score>0 且 ≥自身正分数 expanding q 分位才入场；出场=score≤0。

    正分数样本 <60 个时入场退回裸翻正（次新期口径）；分位只含截至当日历史。
    """
    from bisect import insort
    pos = np.zeros(len(scores))
    held = False
    hist: list[float] = []
    for i, v in enumerate(scores):
        if not np.isfinite(v):
            pos[i] = 1.0 if held else 0.0
            continue
        if v > 0:
            insort(hist, v)
            if held:
                pos[i] = 1.0
            elif len(hist) < 60 or v >= hist[min(len(hist) - 1,
                                                  int(np.ceil(q * len(hist))) - 1)]:
                held = True
                pos[i] = 1.0
        else:
            held = False
    return pos


def confirm_exit_pos(scores: np.ndarray, k: int = 3) -> np.ndarray:
    """score≤0 连续 k 日才离场；入场=score>0（lM v3_3_2 的确认期搬到单资产门）。"""
    pos = np.zeros(len(scores))
    held = False
    neg_run = 0
    for i, v in enumerate(scores):
        if not np.isfinite(v):
            pos[i] = 1.0 if held else 0.0
            continue
        if v > 0:
            neg_run = 0
            held = True
        else:
            neg_run += 1
            if held and neg_run >= k:
                held = False
        pos[i] = 1.0 if held else 0.0
    return pos


def positions(px: pd.Series, temp: pd.Series | None) -> dict[str, pd.Series]:
    """六族仓位序列（0/1 或分数），索引对齐 px；t 行仓位作用于 t→t+1。"""
    out = {}
    out['ma200'] = (px > px.rolling(200).mean()).astype(float)
    out['mom12'] = (px > px.shift(250)).astype(float)
    scores = np.full(len(px), np.nan)
    vals = px.values
    for i in range(25, len(px)):
        scores[i] = momentum_score(vals[i - 25:i], vals[i])
    out['score25'] = pd.Series(scores, index=px.index)
    out['score25'] = (out['score25'] > 0).astype(float).where(
        ~pd.Series(scores, index=px.index).isna(), np.nan)
    out['score25_s80'] = pd.Series(strong_entry_pos(scores, 0.8), index=px.index)
    out['score25_s50'] = pd.Series(strong_entry_pos(scores, 0.5), index=px.index)
    out['score25_c3'] = pd.Series(confirm_exit_pos(scores, 3), index=px.index)

    logret = np.log(px / px.shift(1))
    sigma = logret.rolling(VOL_WIN).std() * np.sqrt(250)
    frac = (VOL_TARGET / sigma).clip(upper=1.0)
    pos = pd.Series(1.0, index=px.index)
    cur = 1.0
    for i in range(len(frac)):
        tgt = frac.iloc[i]
        if pd.notna(tgt) and abs(tgt - cur) > VOL_BAND:
            cur = tgt
        pos.iloc[i] = cur
    out['voltarget'] = pos
    out['ma200_x_vol'] = (out['ma200'] * pos).where(
        out['ma200'].notna() & pos.notna())

    if temp is not None:
        t = temp.reindex(px.index)
        out['tempgate'] = (~(t >= TEMP_EXIT)).astype(float).where(t.notna(), 1.0)
    return out


def evaluate(px: pd.Series, pos: pd.Series) -> dict:
    """公共窗口内：仓位对 t→t+1 生效，费后净值对同窗基准。"""
    ret = px.pct_change().fillna(0.0)
    eff = pos.reindex(px.index).ffill().fillna(1.0)
    # 费用：仓位在 t 日变动即付出（对 t→t+1 收益生效的简化，基准侧对称摊一次建仓费）
    chg = eff.diff().abs()
    chg.iloc[0] = eff.iloc[0]
    strat_r = ret * eff.shift(1).fillna(1.0) - chg * FEE
    bench_r = ret - FEE
    start = WARMUP
    if start >= len(strat_r) - 20:
        return None
    s = (1 + strat_r.iloc[start:]).cumprod()
    b = (1 + bench_r.iloc[start:]).cumprod()
    n = len(s)
    ann = lambda nav: nav.iloc[-1] ** (250 / len(nav)) - 1
    dd = lambda nav: (nav / nav.cummax() - 1).min()
    half = n // 2
    exc_h1 = (ann((1 + strat_r.iloc[start:start + half]).cumprod())
              - ann((1 + bench_r.iloc[start:start + half]).cumprod())) * 100
    exc_h2 = (ann((1 + strat_r.iloc[start + half:]).cumprod())
              - ann((1 + bench_r.iloc[start + half:]).cumprod())) * 100
    return {'ann': ann(s) * 100, 'bench_ann': ann(b) * 100,
            'dd': dd(s) * 100, 'bench_dd': dd(b) * 100,
            'calmar': ann(s) / abs(dd(s)), 'bench_calmar': ann(b) / abs(dd(b)),
            'excess': (ann(s) - ann(b)) * 100, 'dd_delta': (dd(s) - dd(b)) * 100,
            'exc_h1': exc_h1, 'exc_h2': exc_h2,
            'exposure': float(eff.iloc[start:].mean()), 'n_days': n,
            'switches': int((eff.diff().abs() > 0).iloc[start:].sum())}


def main():
    mk = pd.read_csv(MARKET, dtype={'code': str})
    mk['code'] = mk['code'].str.zfill(6)
    mk['date'] = pd.to_datetime(mk['date']).dt.strftime('%Y%m%d').astype(int)
    with open(PANEL, 'rb') as f:
        panel = pickle.load(f)
    temps = {c: p['temp_pct'] for c, p in panel.items()}

    rows = []
    for code, g in mk.groupby('code'):
        px = g.sort_values('date').set_index('date')['event_close'].astype(float)
        px = px[px > 0]
        if len(px) < WARMUP + 100:
            continue
        temp = temps.get(code)
        fams = positions(px, temp)
        for fam, pos in fams.items():
            if pos.isna().all():
                continue
            m = evaluate(px, pos)
            if m is None:
                continue
            rows.append({'code': code, 'class': '行业' if code in INDUSTRY22 else '跨资产',
                         'family': fam, **m})
    df = pd.DataFrame(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    df.to_csv(RESULTS / 'per_asset.csv', index=False, encoding='utf-8-sig')

    df['ins_pass'] = (df['calmar'] > df['bench_calmar']) & (df['excess'] >= -1)
    df['atk_pass'] = (df['excess'] > 2) & (df['dd_delta'] > -5)

    print('== 对 score25 裸门的头对头 ==')
    base = df[df['family'] == 'score25'].set_index('code')
    for fam in ('score25_s80', 'score25_s50', 'score25_c3'):
        sub = df[df['family'] == fam].set_index('code')
        common = base.index.intersection(sub.index)
        d_ann = sub.loc[common, 'ann'] - base.loc[common, 'ann']
        calmar_up = (sub.loc[common, 'calmar'] > base.loc[common, 'calmar']).mean()
        print(f"{fam}: 中位Δ年化 {d_ann.median():+.2f}pp | Calmar 改善占比 {calmar_up:.0%} "
              f"| 往返合计 {int(sub.loc[common,'switches'].sum())} vs {int(base.loc[common,'switches'].sum())}")

    def agg(g) -> pd.Series:
        return pd.Series({
            'n': len(g),
            '中位超额pp': g['excess'].median(),
            'Calmar改善占比': (g['calmar'] > g['bench_calmar']).mean(),
            '保险过线占比': g['ins_pass'].mean(),
            '进攻过线占比': g['atk_pass'].mean(),
            '中位回撤变化pp': g['dd_delta'].median(),
            '中位暴露': g['exposure'].median(),
            '前半中位pp': g['exc_h1'].median(),
            '后半中位pp': g['exc_h2'].median(),
            '半场同号占比': ((np.sign(g['exc_h1']) == np.sign(g['exc_h2'])).mean()),
        })

    print('== 全部资产 ==')
    print(df.groupby('family').apply(agg, include_groups=False).round(3).to_string())
    for cls in ('行业', '跨资产'):
        sub = df[df['class'] == cls]
        if sub.empty:
            continue
        print(f'\n== {cls} ==')
        print(sub.groupby('family').apply(agg, include_groups=False).round(3).to_string())

    summary = {'families_all': df.groupby('family').apply(agg, include_groups=False).to_dict('index'),
               'families_by_class': {c: df[df['class'] == c].groupby('family')
                                     .apply(agg, include_groups=False).to_dict('index')
                                     for c in ('行业', '跨资产')}}
    with open(RESULTS / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=float)
    print(f'\n结果写入 {RESULTS}（逐资产 {len(df)} 行）')


if __name__ == '__main__':
    main()
