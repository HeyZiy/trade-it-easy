# -*- coding: utf-8 -*-
# inset_rank_probe_v1 —— 买入候选集内排序力探针：神谕上限 / R²消融 / 52周新高
#
# 预声明设计（2026-10-18，一次跑答三问，不扫参数、不加额外变体）：
#   背景：study_jq v2 已测集内 IC≈+0.03、top3−正分集均值≈+0.59pp/胜率47%——
#   排序层（score 的 R² 惩罚所在层）无可测区分力；本探针在同一口径下量化
#   (a) 这层空间的神谕上限（事后最优3只−集均值），(b) R²乘法是否伤害排序
#   （消融：只用年化斜率排序），(c) 与斜率正交的 52 周新高能否提供增量。
#
#   数据：_cache/etf_daily_full.pkl + etf_universe.pkl（新浪在市快照，
#     幸存者偏差与 crowd_fwd_v1 相同）；不重取数。
#   池：生产 filter_by_name + build_pool（当前常量，CORR_DEDUPE=0.85），
#     每 20 交易日重建；网格起点 2020-07-01（与 crowd_fwd_v1 同，此前池太小）。
#   信号日 = 重建日收盘；build_rows 现价用最新收盘（无实时，回测口径）。
#   候选集 = 生产 score>0 成员（现行买入准入，无 40% 线；md 第三节）。
#   因子（同集内排序对比，同分按代码升序——与生产 rank 并列规则一致）：
#     score：生产原分（ann×R²，含跳水清零，4 位小数）；
#     ann  ：同一回归的年化斜率（R²≡1 消融；跳水票 score=0 不在集内，
#            故集内 ann 恒正，消融只改排序不改集合）；
#     hi250：现价 / 最近 250 个有效收盘（含信号日）最大值，有效观测
#            <120 记 NaN（George-Hwang 52 周新高接近度，与斜率信息正交）。
#   前瞻收益：qfq 收盘 T→T+20（主口径）、T→T+60（辅助）；网格 20 日间隔，
#     fwd20 天然非重叠；fwd60 末端不足记 NaN（censored），统计降权。
#   指标（集内）：
#     IC：Spearman(factor, fwd)，需 ≥8 有效配对且两侧 ≥2 唯一值（镜像 MIN_IC=8）；
#     top3−集均值（pp）：top3 按因子降序取 3 只，3 只前瞻须全有效否则 NaN；
#       集均值 = 前瞻有效成员均值；
#     神谕：按实现 fwd 排序的事后最优 3 只−集均值（同一严格性）；
#     配对差：(score_top3−集均值)−(ann_top3−集均值) 逐期差（R² 是否伤害）；
#     重叠：score top3 与 ann top3、与 hi250 top3 的成员重叠数。
#   统计：均值/中位/胜率/naive t/Newey-West t（Bartlett，lag=3，预声明不调）。
#   验证：镜像 score（ann×R² 含跳水清零与 4 位舍入）与 build_rows 生产分
#     逐行对账，出现不一致即中止退出（不产出结果）。
#   边界：单一样本段 2020-07~2026-09；收盘近似（无开盘/滑点/费用，与
#     crowd_fwd_v1、study_jq 本地镜像一致）；幸存者偏差；fwd60 重叠，
#     t 值为未作多重扫描校正的描述统计。
#
# 运行：.conda\python.exe research/studies/industry_momentum/inset_rank_probe_v1.py
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.etf.industry_momentum import (SCORE_DAYS, DIVE_RATIO, build_pool,
                                       build_rows, filter_by_name)

HERE = Path(__file__).resolve().parent
CACHE = HERE / '_cache'
OUT = HERE / 'reports' / 'inset_rank_probe_v1'
GRID_START = '2020-07-01'
REBUILD_EVERY = 20
HORIZONS = (20, 60)
MIN_IC = 8
HI_LOOKBACK = 250     # 52周新高窗口（有效收盘）
HI_MIN_OBS = 120      # 窗口内最少有效观测
TOP_N = 3
NW_LAG = 3

FACTORS = ('score', 'ann', 'hi250')


# ── 生产评分镜像（闭式 WLS，等价 np.polyfit(w=linspace(1,2))，见 study_jq）──
def mirror_score_parts(close_tail, price):
    """返回 (ann, r2, score4) 或 None（数据不足/非正价，同生产返回 0 路径）。

    score4 = round(ann*r2, 4)，跳水清零与生产 momentum_score 完全一致。"""
    prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                       float(price))
    if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0) \
            or not np.isfinite(prices).all():
        return None
    logp = np.log(prices)
    x = np.arange(len(logp), dtype=float)
    w = np.linspace(1.0, 2.0, len(logp))
    ew = w ** 2
    mx = float(np.average(x, weights=ew))
    my = float(np.average(logp, weights=ew))
    xc, yc = x - mx, logp - my
    slope = float((ew * xc * yc).sum() / (ew * xc ** 2).sum())
    resid = yc - xc * slope
    ss_tot = float((ew * yc ** 2).sum())
    r2 = float(np.clip(1.0 - (ew * resid ** 2).sum() / ss_tot, 0.0, 1.0)) \
        if ss_tot > 0 else 0.0
    ann = math.expm1(slope * 250.0)
    if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
           prices[-3] / prices[-4]) < DIVE_RATIO:
        return ann, r2, 0.0        # 消融保留 ann 原值；score 同生产清零
    return ann, r2, round(ann * r2, 4)


def nw_t(x, lag=NW_LAG):
    """Newey-West t（Bartlett 核），x 为逐期序列；不足 3 期返回 NaN。"""
    x = np.asarray([v for v in x if v == v], dtype=float)
    m = len(x)
    if m < 3:
        return float('nan')
    e = x - x.mean()
    s0 = float((e * e).sum())
    var = s0
    for j in range(1, min(lag, m - 1) + 1):
        sj = float((e[j:] * e[:-j]).sum())
        var += 2.0 * (1.0 - j / (lag + 1.0)) * sj
    se = math.sqrt(max(var, 0.0) / m)
    return float(x.mean() / se) if se > 0 else float('nan')


def naive_t(x):
    x = np.asarray([v for v in x if v == v], dtype=float)
    if len(x) < 3:
        return float('nan')
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(len(x))) if sd > 0 else float('nan')


def spearman_ic(factor, fwd):
    paired = pd.concat([factor.rename('f'), fwd.rename('r')], axis=1).dropna()
    if len(paired) < MIN_IC or paired['f'].nunique() < 2 \
            or paired['r'].nunique() < 2:
        return float('nan')
    return float(paired['f'].rank().corr(paired['r'].rank()))


def top_n(factor, n=TOP_N):
    """按 (-factor, code) 稳定排序取前 n 只——与生产 rank 并列规则一致。"""
    s = factor.dropna()
    order = sorted(s.index, key=lambda c: (-s[c], c))
    return order[:n]


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

    close_wide = pd.DataFrame({c: bars[c]['close'] for c in cand_codes})
    fwd_wide = {k: close_wide.shift(-k) / close_wide - 1.0 for k in HORIZONS}

    periods, mismatches, n_rows_checked = [], [], 0
    for gi, g in enumerate(grid):
        bars_g = {c: df.loc[:g] for c, df in bars.items()}
        members, _stats = build_pool(cands, bars_g, as_of=g)
        if not members:
            continue
        member_bars = {c: bars[c] for c in (m['code'] for m in members)}
        prices = {}
        for m in members:
            c = m['code']
            v = close_wide[c].loc[:g]
            if len(v):
                v = v.iloc[-1]
                if v == v and v > 0:
                    prices[c] = float(v)
        rows, _diag = build_rows(
            [{'code': m['code'], 'name': m['name']} for m in members],
            member_bars, prices, as_of=g)

        fac = {f: {} for f in FACTORS}
        prod_score = {}
        for r in rows:
            c = r.code
            sub = bars[c]
            sub = sub[sub.index <= g]
            close_tail = sub['close'].astype(float).values
            price = prices.get(c)
            if price is None:
                continue
            parts = mirror_score_parts(close_tail, price)
            if parts is None:
                continue
            ann, r2, score4 = parts
            n_rows_checked += 1
            if abs(score4 - r.score) > 1e-9:
                mismatches.append({'date': g, 'code': c, 'mirror': score4,
                                   'prod': r.score})
            fac['score'][c] = float(r.score)
            fac['ann'][c] = float(ann)
            closes = sub['close'].dropna().astype(float)
            win = closes.tail(HI_LOOKBACK)   # price=最后有效收盘，已在窗内
            if len(win) >= HI_MIN_OBS:
                fac['hi250'][c] = float(price / win.max())

        pos_set = {c for c in fac['score'] if fac['score'][c] > 0}
        rec = {'date': g, 'set_size': len(pos_set)}
        if len(pos_set) < TOP_N:
            periods.append(rec)
            continue
        set_idx = pd.Index(sorted(pos_set))
        for k in HORIZONS:
            f20 = fwd_wide[k].loc[g] if g in fwd_wide[k].index else None
            if f20 is None:
                continue
            r_fwd = f20.reindex(set_idx)
            valid = r_fwd.dropna()
            rec[f'set_mean_fwd{k}'] = float(valid.mean()) if len(valid) else float('nan')
            for f in FACTORS:
                fs = pd.Series(fac[f]).reindex(set_idx)
                rec[f'ic_{f}_fwd{k}'] = spearman_ic(fs, r_fwd)
                top = top_n(fs)
                tv = [r_fwd.get(c) for c in top]
                if all(v == v for v in tv) and len(valid) >= TOP_N:
                    rec[f'top{TOP_N}_minus_set_{f}_fwd{k}'] = \
                        float(np.mean(tv) - rec[f'set_mean_fwd{k}'])
                    rec[f'top{TOP_N}_codes_{f}_fwd{k}'] = ','.join(top)
            if rec.get(f'set_mean_fwd{k}') == rec.get(f'set_mean_fwd{k}') \
                    and len(valid) >= TOP_N:
                ov = sorted(valid.index, key=lambda c: (-valid[c], c))[:TOP_N]
                ovv = [valid[c] for c in ov]
                if all(v == v for v in ovv):
                    rec[f'oracle_minus_set_fwd{k}'] = \
                        float(np.mean(ovv) - rec[f'set_mean_fwd{k}'])
        a3 = rec.get(f'top{TOP_N}_codes_score_fwd20')
        b3 = rec.get(f'top{TOP_N}_codes_ann_fwd20')
        h3 = rec.get(f'top{TOP_N}_codes_hi250_fwd20')
        if a3 and b3:
            rec[f'overlap_score_ann'] = len(set(a3.split(',')) & set(b3.split(',')))
            sa = rec.get(f'top{TOP_N}_minus_set_score_fwd20')
            sb = rec.get(f'top{TOP_N}_minus_set_ann_fwd20')
            if sa == sa and sb == sb:
                rec['paired_score_minus_ann_fwd20'] = sa - sb
        if a3 and h3:
            rec[f'overlap_score_hi250'] = len(set(a3.split(',')) & set(h3.split(',')))
        periods.append(rec)
        print(f'{g} 集 {len(pos_set)} | 对账 {len(mismatches)} 不一致',
              flush=True)

    print(f'\n生产分对账：{len(mismatches)} / {n_rows_checked} 不一致',
          flush=True)
    if mismatches:
        pd.DataFrame(mismatches).to_csv(OUT / 'score_mismatch.csv',
                                        index=False, encoding='utf-8-sig')
        print('镜像与生产分不一致，中止——先修镜像再出结果', flush=True)
        return 1

    P = pd.DataFrame(periods)
    P.to_csv(OUT / 'periods.csv', index=False, encoding='utf-8-sig')

    # ── 汇总表 ──
    rows = []
    for k in HORIZONS:
        for f in FACTORS + ('oracle',):
            key = (f'top{TOP_N}_minus_set_{f}_fwd{k}' if f != 'oracle'
                   else f'oracle_minus_set_fwd{k}')
            if f in FACTORS:
                ics = P[f'ic_{f}_fwd{k}'].dropna()
                rows.append({'horizon': k, 'factor': f, 'metric': 'ic',
                             'n': len(ics),
                             'mean': round(ics.mean(), 4) if len(ics) else float('nan'),
                             'median': round(ics.median(), 4) if len(ics) else float('nan'),
                             'win_rate': round((ics > 0).mean() * 100, 1) if len(ics) else float('nan'),
                             't_naive': round(naive_t(ics), 2) if len(ics) else float('nan'),
                             't_nw': round(nw_t(ics), 2) if len(ics) else float('nan')})
            tm = P[key].dropna()
            rows.append({'horizon': k, 'factor': f,
                         'metric': f'top{TOP_N}_minus_set_pp',
                         'n': len(tm),
                         'mean': round(tm.mean() * 100, 2) if len(tm) else float('nan'),
                         'median': round(tm.median() * 100, 2) if len(tm) else float('nan'),
                         'win_rate': round((tm > 0).mean() * 100, 1) if len(tm) else float('nan'),
                         't_naive': round(naive_t(tm), 2) if len(tm) else float('nan'),
                         't_nw': round(nw_t(tm), 2) if len(tm) else float('nan')})
    pdif = P['paired_score_minus_ann_fwd20'].dropna()
    rows.append({'horizon': 20, 'factor': 'score_minus_ann',
                 'metric': 'paired_top3_diff_pp', 'n': len(pdif),
                 'mean': round(pdif.mean() * 100, 2) if len(pdif) else float('nan'),
                 'median': round(pdif.median() * 100, 2) if len(pdif) else float('nan'),
                 'win_rate': round((pdif > 0).mean() * 100, 1) if len(pdif) else float('nan'),
                 't_naive': round(naive_t(pdif), 2) if len(pdif) else float('nan'),
                 't_nw': round(nw_t(pdif), 2) if len(pdif) else float('nan')})
    S = pd.DataFrame(rows)
    S.to_csv(OUT / 'summary.csv', index=False, encoding='utf-8-sig')

    ov_sa = P['overlap_score_ann'].dropna()
    ov_sh = P['overlap_score_hi250'].dropna()
    print('\n=== 汇总（集内，2020-07~2026-09）===', flush=True)
    print(f'有效期数 {len(P)}，集规模均值 '
          f'{P["set_size"].mean():.1f}', flush=True)
    print(f'score top3 与 ann top3 重叠：均值 {ov_sa.mean():.2f}/{TOP_N}'
          f'（{len(ov_sa)} 期）', flush=True)
    print(f'score top3 与 hi250 top3 重叠：均值 {ov_sh.mean():.2f}/{TOP_N}'
          f'（{len(ov_sh)} 期）', flush=True)
    print(S.to_string(index=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
