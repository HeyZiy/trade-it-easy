# -*- coding: utf-8 -*-
# crowd_fwd_v1 —— 量价热度分组前瞻收益本地实验 v1（2026-10-06）
#
# 问题：量价热度（成交额占池比重 250 日分位与自身收盘价 250 日分位的均值，生产
#   build_rows 口径）对池成员的后续收益有没有区分度？≥90 禁买这道闸在买入
#   候选集里拦的到底是不是差票？两个分量谁在带信号？
# 口径（全部复用生产引擎原函数，零重写）：
#   - 池 = filter_by_name + build_pool（成熟/流动/相关去重）每 20 交易日重建，
#     bars 按 as_of 截断传入（build_pool 不自行截断，调用方负责——生产同式）；
#   - crowd/score/rank = build_rows 逐日输出（判定核原函数；price 用当日收盘
#     代现价，与回测 14:55≈收盘口径一致）；
#   - 两分量（share/price 分位）按 build_rows 同式镜像：分母 = 当期池成员
#     amount 之和（未上市/缺数按 0），窗口 = 自身最近 ≤250 个有效观测、
#     不足 60 返回 None；镜像结果与 build_rows 的 composite 逐日对账，不一致
#     即报错退出（镜像漂移防护）。
# 数据：_cache/etf_daily_full.pkl（crowd_fwd_fetch 产出，qfq+amount，新浪在市表）。
# 已知偏差（报告须携带）：
#   - 幸存者偏差：名单=当前在市表，已退市 ETF 不在——与生产的前视一致，
#     与历史真池有差；对"量价热度排序是否单调"影响有限，对绝对水平有抬升；
#   - 名称用当前名（重命名后视，小）；qfq 只折算份额折算跳变（|ret|>25%），
#     不做分红调整（行业 ETF 分红罕见）；
#   - 前瞻收益非重叠采样：k 日收益每 k 个交易日取一个样本，杜绝重叠虚增 n。
# 产出：reports/crowd_fwd_v1/ 下 panel.pkl + 表 A-E CSV + cases.csv + README.md。

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.etf.industry_momentum import (CROWD_LOOKBACK, CROWD_MIN_OBS,
                                       build_pool, build_rows, filter_by_name,
                                       _pct_in_window)

HERE = Path(__file__).resolve().parent
CACHE = HERE / '_cache'
OUT = HERE / 'reports' / 'crowd_fwd_v1'
HORIZONS = [5, 10, 20, 60]
GRID_START = '2020-07-01'        # 池重建网格起点（此前行业 ETF 池太小）
REBUILD_EVERY = 20
BANDS = [('<50', 0.0, 50.0), ('50-70', 50.0, 70.0), ('70-80', 70.0, 80.0),
         ('80-90', 80.0, 90.0), ('>=90', 90.0, 101.0)]


def band_of(crowd):
    if crowd is None or crowd != crowd:
        return 'None'
    for label, lo, hi in BANDS:
        if lo <= crowd < hi:
            return label
    return '>=90'


def rolling_pct(s: pd.Series, window: int, min_obs: int) -> pd.Series:
    """自身窗口分位（镜像 _pct_in_window）：在有效值序列上滚动，取值 = 窗口内
    严格小于当前值的比例×100（round 0.1）；不足 min_obs 个有效观测为 NaN。"""
    def pct(w):
        last = w[-1]
        return round(float((w < last).sum()) / len(w) * 100.0, 1)
    return s.rolling(window, min_periods=min_obs).apply(pct, raw=True)


def truth_components(bars, member_codes, t):
    """build_rows 原式逐字重算两分量（仅取证用，慢）：sub 截断 + zip 过滤。"""
    amts = {c: bars[c][bars[c].index <= t]['amount'].astype(float)
            for c in member_codes}
    rowsum = pd.DataFrame(amts).fillna(0.0).sum(axis=1)
    out = {}
    for c in member_codes:
        amt = amts[c]
        rec = {'n_share': 0, 'sp': None, 'cp': None}
        if not amt.empty:
            share = [v / s for v, s in zip(amt.values,
                                           rowsum.reindex(amt.index).values)
                     if s and s > 0 and v == v and v > 0]
            rec['n_share'] = len(share)
            rec['sp'] = _pct_in_window(share, CROWD_LOOKBACK)
        closes = [v for v in bars[c][bars[c].index <= t]['close'].astype(float).values
                  if v == v]
        rec['n_close'] = len(closes)
        rec['cp'] = _pct_in_window(closes, CROWD_LOOKBACK)
        out[c] = rec
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    daily = pd.read_pickle(CACHE / 'etf_daily_full.pkl')
    uni = pd.read_pickle(CACHE / 'etf_universe.pkl')
    cands = filter_by_name(uni.to_dict('records'))
    cand_codes = [c['code'] for c in cands if c['code'] in daily]
    name_of = {c['code']: c['name'] for c in cands}
    bars = {c: daily[c] for c in cand_codes}
    print(f'候选（过名称关且有数据）{len(cand_codes)} 只', flush=True)

    all_days = sorted(set().union(*[set(df.index) for df in bars.values()]))
    start_i = next(i for i, d in enumerate(all_days) if d >= GRID_START)
    grid = all_days[start_i::REBUILD_EVERY]
    print(f'交易日 {len(all_days)} 天，重建网格 {len(grid)} 点 '
          f'({grid[0]} ~ {grid[-1]})', flush=True)

    # 前瞻收益宽表（qfq 收盘）
    close_wide = pd.DataFrame({c: bars[c]['close'] for c in cand_codes})
    fwd = {k: close_wide.shift(-k) / close_wide - 1.0 for k in HORIZONS}
    pos_of = {d: i for i, d in enumerate(all_days)}

    # ── 时点池：每 20 交易日一次，bars 截断到重建日 ──
    periods = []                          # [(pool_start, pool_end, members)]
    pool_sizes = []
    for gi, g in enumerate(grid):
        bars_g = {c: df.loc[:g] for c, df in bars.items()}
        members, stats = build_pool(cands, bars_g, as_of=g)
        end = grid[gi + 1] if gi + 1 < len(grid) else all_days[-1]
        periods.append((g, end, [m['code'] for m in members]))
        pool_sizes.append((g, stats['mature'], stats['liquid'], len(members)))
    ps = pd.DataFrame(pool_sizes, columns=['rebuild', 'mature', 'liquid', 'members'])
    ps.to_csv(OUT / 'pool_sizes.csv', index=False, encoding='utf-8-sig')
    print(f'池规模：中位 {ps.members.median():.0f} / 最小 {ps.members.min()} / '
          f'最大 {ps.members.max()}', flush=True)

    # ── 逐日截面：build_rows 真值 + 两分量镜像对账 ──
    rows = []
    mismatches = []
    truth_cache = {}
    for pi, (p_start, p_end, members) in enumerate(periods):
        if not members:
            continue
        last_period = (pi == len(periods) - 1)
        member_bars = {c: bars[c] for c in members}
        # 期末=下一期起点，避免边界日重复计入两期
        day_list = [d for d in all_days if p_start <= d <= p_end]
        if not last_period:
            day_list = [d for d in day_list if d < p_end]
        A = pd.DataFrame({c: bars[c]['amount'] for c in members})
        rowsum = A.fillna(0.0).sum(axis=1)
        share_ok = rowsum.where(rowsum > 0)
        share_wide = A.div(share_ok, axis=0)
        sp_all, cp_all = {}, {}
        for c in members:
            sv = share_wide[c]
            sv = sv[(sv == sv) & (sv > 0)]
            # 先对全局日历 reindex+ffill 再取当期：分位=过滤列表尾值的静态结转，
            # 须跨期结转（最后一次有效观测可能落在本期开始之前）
            sp_all[c] = rolling_pct(sv, CROWD_LOOKBACK, CROWD_MIN_OBS) \
                .reindex(all_days).ffill()
            cv = bars[c]['close'].dropna()
            cp_all[c] = rolling_pct(cv, CROWD_LOOKBACK, CROWD_MIN_OBS) \
                .reindex(all_days).ffill()
        for t in day_list:
            prices = {}
            for c in members:
                v = close_wide[c].loc[:t]
                v = v.iloc[-1] if len(v) else float('nan')
                if v == v and v > 0:
                    prices[c] = float(v)
            rt, diag = build_rows([{'code': c, 'name': name_of[c]} for c in members],
                                  member_bars, prices, as_of=t)
            for r in rt:
                sp = sp_all[r.code].get(t, float('nan'))
                cp = cp_all[r.code].get(t, float('nan'))
                comps = [x for x in (sp, cp) if x == x]
                my_crowd = round(float(np.mean(comps)), 1) if len(comps) >= 2 else None
                bad = (my_crowd is None) != (r.crowd is None) or \
                      (my_crowd is not None and abs(my_crowd - r.crowd) > 0.05)
                if bad:
                    if t not in truth_cache:
                        truth_cache[t] = truth_components(member_bars, members, t)
                    tc = truth_cache[t][r.code]
                    mismatches.append({
                        'date': t, 'code': r.code, 'my_sp': sp if sp == sp else None,
                        'my_cp': cp if cp == cp else None, 'my_crowd': my_crowd,
                        'truth_sp': tc['sp'], 'truth_cp': tc['cp'],
                        'truth_crowd': r.crowd, 'n_share': tc['n_share'],
                        'n_close': tc['n_close']})
                f = {f'fwd{k}': float(fwd[k].at[t, r.code])
                     if r.code in fwd[k].columns and t in fwd[k].index
                     else float('nan') for k in HORIZONS}
                rows.append({'date': t, 'code': r.code, 'name': r.name,
                             'score': r.score, 'rank': r.rank, 'n': r.n,
                             'crowd': r.crowd, 'sp': sp if sp == sp else None,
                             'cp': cp if cp == cp else None, **f})
        print(f'{p_start}~{p_end} 池 {len(members)} | 累计观测 {len(rows)}',
              flush=True)
    print(f'分量镜像与 build_rows composite 对账：不一致 {len(mismatches)} / '
          f'{len(rows)}（明细 mirror_mismatch.csv）', flush=True)
    if mismatches:
        pd.DataFrame(mismatches).to_csv(OUT / 'mirror_mismatch.csv', index=False,
                                        encoding='utf-8-sig')

    panel = pd.DataFrame(rows)
    panel['exit_rank'] = panel['n'].apply(lambda n: math.ceil(0.40 * n))
    panel['is_cand'] = (panel['score'] > 0) & (panel['rank'] <= panel['exit_rank'])
    panel['band'] = panel['crowd'].apply(band_of)
    panel['year'] = panel['date'].str[:4]
    panel['pos'] = panel['date'].map(pos_of)
    panel.to_pickle(OUT / 'panel.pkl')

    # ── 统计表（k 日收益按每 k 交易日采样，非重叠）──
    def stats_table(df, horizons):
        out = []
        for k in horizons:
            sub = df[(df['pos'] % k == 0) & df[f'fwd{k}'].notna()]
            if sub.empty:
                continue
            out.append({'n': len(sub), 'horizon': k,
                        'mean_pct': round(sub[f'fwd{k}'].mean() * 100, 2),
                        'median_pct': round(sub[f'fwd{k}'].median() * 100, 2),
                        'win_rate': round((sub[f'fwd{k}'] > 0).mean() * 100, 1)})
        return out

    # 表 A：全体成员日 × 量价热度档
    tA = []
    for band in [b[0] for b in BANDS] + ['None']:
        sub = panel[panel['band'] == band]
        if sub.empty:
            continue
        for st in stats_table(sub, HORIZONS):
            tA.append({'band': band, **st})
    pd.DataFrame(tA).to_csv(OUT / 'tableA_all_by_band.csv', index=False,
                            encoding='utf-8-sig')

    # 表 B：量价热度十分位 × 20 日前瞻
    sub5 = panel[(panel['pos'] % 5 == 0) & panel['crowd'].notna()].copy()
    sub5['decile'] = pd.qcut(sub5['crowd'], 10, labels=False, duplicates='drop') + 1
    tB = []
    for d, g in sub5.groupby('decile', observed=True):
        f = g['fwd20'].dropna()
        tB.append({'decile': d, 'n': len(f),
                   'mean_pct': round(f.mean() * 100, 2),
                   'median_pct': round(f.median() * 100, 2),
                   'win_rate': round((f > 0).mean() * 100, 1),
                   'crowd_min': round(g['crowd'].min(), 1),
                   'crowd_max': round(g['crowd'].max(), 1)})
    pd.DataFrame(tB).to_csv(OUT / 'tableB_decile_fwd20.csv', index=False,
                            encoding='utf-8-sig')

    # 表 C：买入候选集（score>0 且 rank≤40% 线）× 量价热度档 —— 闸门的本职考题
    cand = panel[panel['is_cand']]
    tC = []
    for band in [b[0] for b in BANDS] + ['None']:
        sub = cand[cand['band'] == band]
        if sub.empty:
            continue
        for st in stats_table(sub, HORIZONS):
            tC.append({'band': band, **st})
    pd.DataFrame(tC).to_csv(OUT / 'tableC_candidates_by_band.csv', index=False,
                            encoding='utf-8-sig')

    # 表 D：两分量各自十分位 × 20 日前瞻（候选集，5 日采样）
    tD = []
    for comp, label in [('sp', 'share_pct'), ('cp', 'price_pct')]:
        s = cand[(cand['pos'] % 5 == 0) & cand[comp].notna()].copy()
        s['decile'] = pd.qcut(s[comp], 10, labels=False, duplicates='drop') + 1
        for d, g in s.groupby('decile', observed=True):
            f = g['fwd20'].dropna()
            tD.append({'component': label, 'decile': d, 'n': len(f),
                       'median_pct': round(f.median() * 100, 2),
                       'win_rate': round((f > 0).mean() * 100, 1)})
    for comp, label in [('sp', 'share_pct'), ('cp', 'price_pct'),
                        ('crowd', 'composite')]:
        s = cand[(cand['pos'] % 5 == 0) & cand[comp].notna()]
        rho = s[comp].corr(s['fwd20'], method='spearman')
        tD.append({'component': label, 'decile': 'spearman_all',
                   'n': len(s), 'median_pct': round(float(rho) * 100, 2),
                   'win_rate': ''})
    pd.DataFrame(tD).to_csv(OUT / 'tableD_components.csv', index=False,
                            encoding='utf-8-sig')

    # 表 E：分年 × 档（20 日前瞻中位数，20 日采样）
    tE = []
    for (y, band), g in panel[panel['pos'] % 20 == 0].groupby(['year', 'band']):
        f = g['fwd20'].dropna()
        if len(f) < 5:
            continue
        tE.append({'year': y, 'band': band, 'n': len(f),
                   'median_pct': round(f.median() * 100, 2),
                   'win_rate': round((f > 0).mean() * 100, 1)})
    pd.DataFrame(tE).to_csv(OUT / 'tableE_year_band.csv', index=False,
                            encoding='utf-8-sig')

    # cases：候选集里被 ≥90 拦下的买入（闸门实际生效的全部现场）
    cases = cand[cand['band'] == '>=90'].sort_values('date')
    cases.to_csv(OUT / 'cases_blocked.csv', index=False, encoding='utf-8-sig')

    print(f'观测 {len(panel)}（候选集 {len(cand)}，被拦 {len(cases)}）', flush=True)
    print('\n=== 表 C：候选集 × 量价热度档（闸门本职考题）===', flush=True)
    print(pd.DataFrame(tC).to_string(index=False), flush=True)
    print('\n=== 表 B：量价热度十分位 × fwd20 ===', flush=True)
    print(pd.DataFrame(tB).to_string(index=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
