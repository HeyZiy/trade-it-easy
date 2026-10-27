# -*- coding: utf-8 -*-
"""基本面赞助检验：行业入场的亏损是否集中在"无盈利改善"的行业。

研究问题：温度计已证实行业入场亏损集中在 ETF 端成交占比高位（高温）。
本检验进一步问：高温/全体的行业入场中，有基本面赞助（成分股盈利改善）
的后续路径是否好于无赞助的。分得开→行业轮动以"价格趋势×基本面确认"
的形式有条件前途；分不开→行业价格趋势主要由资金驱动，关门。

预注册口径（2026-10-26 定稿，防止数据窥探）：
- 事件：results/temperature/events.csv 的行业入场事件（352 次有温度值），
  excess_h5/h10/h20 沿用该文件（行业固定窗口毛收益−原池当日合格第一名）。
- 成员：与温度计分母同源同规则（SW_MAP 时点成分，INDATE<=t<OUTDATE）。
- 点时：可见性 = ACTUAL_ANN_DATE（缺则 ANN_DATE）<= 入场日前一自然日；
  同报告期取最晚披露版本。与 src/quality_pool/feeds.py::_visible 同口径，
  此处独立实现。
- 每股盈利动量：最新可见报告期 P 的归母净利润 vs 去年同期 P-1y（同为
  点时可见版本），YoY = (x−y)/|y|×100。
  - 单季口径 sq：STATEMENT_TYPE=2（合并单季）
  - 累计口径 cum：STATEMENT_TYPE=1（合并累计）
  各股最新可见报告期可不同（披露节奏差异），YoY 均为同期匹配，跨股混合合法。
- 主指标 breadth = 成员中 YoY>0 的比例（可计算成员内）；主切分 >=0.5。
- 主基准选择规则（预先固定）：若事件的 sq 覆盖率（可计算成员/成员总数）
  中位数 >=0.7，主读数用 sq；否则切换 cum 并在结果中注明。事件级覆盖率
  <0.6 者不入主检验（计数报告）。
- 主读数：高温桶（temp_pct>=2/3）内 breadth 切分的 excess_h10 均值差；
  h5/h20 为副读数。胜率 = excess>0 比例。
- 稳健性（预先固定）：逐一剔除入场年份、逐一剔除单只 ETF、2023 年单列、
  breadth 与 temp_pct 的 Spearman 秩相关（排除"温度影子"）。
- 全程描述性对照，不做显著性断言；事件窗口重叠与时间聚集照旧。

从仓库根运行：
  D:\\App\\Conda\\python.exe research/studies/industry_momentum/study_sponsorship.py
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
sys.path.insert(0, str(HERE))

from research.studies.industry_momentum.temperature_build import SW_MAP  # noqa: E402  单一来源：ETF→申万映射

EVENTS_CSV = HERE / 'results' / 'temperature' / 'events.csv'
INCOME_CACHE = HERE / '_cache' / 'sponsorship_income.pkl'
MEMBERS_CACHE = HERE / '_cache' / 'temperature_members.pkl'
OUT_DIR = HERE / 'results' / 'sponsorship'

TYPE_CUM, TYPE_SQ = 1, 2          # 合并累计 / 合并单季（同 quality_pool 口径）
COVERAGE_EVENT_FLOOR = 0.6         # 事件级覆盖率下限
COVERAGE_PRIMARY_RULE = 0.7        # 中位数覆盖率决定主基准 sq/cum
BREADTH_SPLIT = 0.5                # 主切分
TEMP_HIGH = 2 / 3                  # 高温桶（与温度计研究一致）
TEMP_TOP = 0.9                     # 前十分位


def _date_int(series) -> np.ndarray:
    """任意日期形态 → int YYYYMMDD（无效为 -1，排序时置于最前不参与可见性）。

    数值形态（20241231 / 20241231.0）必须按格式解析：直接 pd.to_datetime
    会把整数当纳秒纪元。字符串（'2024-12-31' 等）走常规解析。
    """
    s = pd.Series(series).reset_index(drop=True)
    if pd.api.types.is_numeric_dtype(s):
        txt = s.astype('Int64').astype(str)
        ts = pd.to_datetime(txt, format='%Y%m%d', errors='coerce')
    else:
        ts = pd.to_datetime(s, errors='coerce')
    out = ts.dt.strftime('%Y%m%d').astype(float)
    return out.fillna(-1).astype(np.int64).to_numpy()


def _period_int(series) -> np.ndarray:
    return _date_int(series)


def _yoy(x: float, y: float) -> float:
    if y is None or y == 0 or not np.isfinite(x) or not np.isfinite(y):
        return np.nan
    return (x - y) / abs(y) * 100.0


def build_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    """单只股票的披露时间线 → 逐披露日快照表。

    输入列：REPORTING_PERIOD / ANN_DATE / ACTUAL_ANN_DATE / STATEMENT_TYPE /
    NET_PRO_EXCL_MIN_INT_INC。输出行 = 每个披露日后的可见状态：
    ann, sq_period, sq_yoy, cum_period, cum_yoy（最新报告期及同期匹配 YoY）。
    同报告期后披露覆盖先披露（更正/调整报），同披露日同报告期取文件顺序后者。
    """
    need = {'REPORTING_PERIOD', 'ANN_DATE', 'ACTUAL_ANN_DATE',
            'STATEMENT_TYPE', 'NET_PRO_EXCL_MIN_INT_INC'}
    if df is None or df.empty or not need <= set(df.columns):
        return pd.DataFrame()
    d = df.copy()
    act = (_date_int(d['ACTUAL_ANN_DATE'])
           if 'ACTUAL_ANN_DATE' in d.columns else np.full(len(d), -1))
    d['ann'] = np.where(act > 0, act, _date_int(d['ANN_DATE']))
    d['period'] = _period_int(d['REPORTING_PERIOD'])
    d['np'] = pd.to_numeric(d['NET_PRO_EXCL_MIN_INT_INC'], errors='coerce')
    d['st'] = pd.to_numeric(d['STATEMENT_TYPE'], errors='coerce')
    d = d[(d['ann'] > 0) & (d['period'] > 0) & d['st'].isin([TYPE_CUM, TYPE_SQ])]
    d = d.dropna(subset=['np'])
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values(['ann', 'period', 'st'], kind='stable')

    maps = {TYPE_CUM: {}, TYPE_SQ: {}}   # period -> 最新披露值
    rows = []
    for ann, grp in d.groupby('ann', sort=True):
        for _, r in grp.iterrows():
            maps[int(r['st'])][int(r['period'])] = float(r['np'])
        row = {'ann': int(ann)}
        for st, pfx in ((TYPE_SQ, 'sq'), (TYPE_CUM, 'cum')):
            m = maps[st]
            if not m:
                row[f'{pfx}_period'], row[f'{pfx}_yoy'] = -1, np.nan
                continue
            p = max(m)
            ya = p - 10000  # 去年同期：YYYYMMDD 直接减一年
            row[f'{pfx}_period'] = p
            row[f'{pfx}_yoy'] = _yoy(m[p], m.get(ya))
        rows.append(row)
    return pd.DataFrame(rows)


def members_at(members: dict, indices: list, date_int: int) -> set:
    out, seen = set(), set()
    for idx in indices:
        df = members[idx]
        m = (df['INDATE'] <= date_int)
        if 'OUTDATE' in df.columns:
            outd = df['OUTDATE']
            m &= outd.isna() | (outd > date_int)
        for c in df.loc[m, 'CON_CODE']:
            c6 = str(c).split('.')[0]
            if c6 not in seen:
                seen.add(c6)
                out.add(c6)
    return out


# ---------------------------------------------------------------- 自检测试

def run_self_tests() -> int:
    n_pass = 0

    def check(name, cond):
        nonlocal n_pass
        assert cond, f'自检失败: {name}'
        n_pass += 1

    base = pd.DataFrame({
        'REPORTING_PERIOD': [20241231, 20241231, 20251231, 20241231, 20251231],
        'ANN_DATE': ['2025-03-01', '2025-04-30', '2026-03-01', '2025-04-30', '2026-03-01'],
        'ACTUAL_ANN_DATE': [None, None, None, None, None],
        'STATEMENT_TYPE': [2, 2, 2, 1, 1],
        'NET_PRO_EXCL_MIN_INT_INC': [100.0, 120.0, 90.0, 100.0, 250.0],
    })
    snap = build_snapshots(base)
    # 快照边界：2025-03-01 时仅单季 2024Q4（累计未披露）
    r0 = snap[snap['ann'] == 20250301].iloc[0]
    check('点时边界：未披露不可见', r0['cum_period'] == -1)
    # 2024Q4 基期（2023Q4）未披露 → 单季 YoY 为 NaN（4/30 更正后仍无基期）
    r1 = snap[snap['ann'] == 20250430].iloc[0]
    check('基期缺失→NaN', np.isnan(r1['sq_yoy']))
    r2 = snap[snap['ann'] == 20260301].iloc[0]
    # 2025Q4 单季 90 vs 2024Q4(=120 可见最新版) → (90-120)/120 = -25%
    check('单季YoY同期匹配', abs(r2['sq_yoy'] - (-25.0)) < 1e-9)
    # 累计 2025FY 250 vs 2024FY 100 → +150%
    check('累计YoY', abs(r2['cum_yoy'] - 150.0) < 1e-9)

    # 负基期扭亏：y=-100, x=+50 → (50-(-100))/100 = +150%
    check('负基期abs分母', _yoy(50.0, -100.0) == 150.0)
    check('零基期→NaN', np.isnan(_yoy(50.0, 0.0)))

    # 成员时点过滤：000001 已于 20200101 剔除，000002 在窗口内
    mem = {'X': pd.DataFrame({'CON_CODE': ['000001.SZ', '000002.SZ'],
                              'INDATE': [20150101, 20200101],
                              'OUTDATE': [20200101, 20230101]})}
    m = members_at(mem, ['X'], 20210101)
    check('成员INDATE/OUTDATE', m == {'000002'})

    # searchsorted 语义：asof=入场前一日，不含当日披露
    anns = np.array([20250301, 20250430, 20260301])
    idx = np.searchsorted(anns, 20250430, side='right') - 1
    check('asof含当日及以前', idx == 1)
    idx2 = np.searchsorted(anns, 20250228, side='right') - 1
    check('asof早于首披露', idx2 == -1)

    print(f'自检 {n_pass} 项通过', flush=True)
    return n_pass


# ---------------------------------------------------------------- 主流程

def main():
    n_tests = run_self_tests()

    events = pd.read_csv(EVENTS_CSV, encoding='utf-8-sig')
    events['date_int'] = events['date_int'].astype(int)
    events = events.sort_values('date_int').reset_index(drop=True)
    with open(MEMBERS_CACHE, 'rb') as f:
        members = pickle.load(f)
    with open(INCOME_CACHE, 'rb') as f:
        income = pickle.load(f)
    print(f'事件 {len(events)} 条（有温度 {events["temp_pct"].notna().sum()}），'
          f'利润表 {len(income)} 只', flush=True)

    # 预构建每股快照表
    snaps = {}
    for code, df in income.items():
        s = build_snapshots(df)
        if not s.empty:
            snaps[code] = (s['ann'].to_numpy(),
                           s['sq_yoy'].to_numpy(dtype=float),
                           s['cum_yoy'].to_numpy(dtype=float))

    # 事件级聚合
    recs = []
    for _, ev in events.iterrows():
        t = int(ev['date_int'])
        asof = int((pd.Timestamp(str(t)) - pd.Timedelta(days=1)).strftime('%Y%m%d'))
        codes = members_at(members, SW_MAP[str(ev['code'])], t)
        sq, cum = [], []
        for c in codes:
            if c not in snaps:
                continue
            anns, sq_y, cum_y = snaps[c]
            i = np.searchsorted(anns, asof, side='right') - 1
            if i < 0:
                continue
            sq.append(sq_y[i])
            cum.append(cum_y[i])
        sq = np.array(sq, dtype=float)
        cum = np.array(cum, dtype=float)
        n = max(len(codes), 1)
        sq_c, cum_c = sq[np.isfinite(sq)], cum[np.isfinite(cum)]
        recs.append({
            'entry': ev['entry'], 'code': ev['code'], 'year': int(ev['year']),
            'date_int': t, 'temp_pct': ev['temp_pct'],
            'n_members': len(codes),
            'n_comp_sq': len(sq_c), 'coverage_sq': len(sq_c) / n,
            'breadth_sq': float((sq_c > 0).mean()) if len(sq_c) else np.nan,
            'median_sq_yoy': float(np.median(sq_c)) if len(sq_c) else np.nan,
            'n_comp_cum': len(cum_c), 'coverage_cum': len(cum_c) / n,
            'breadth_cum': float((cum_c > 0).mean()) if len(cum_c) else np.nan,
            'median_cum_yoy': float(np.median(cum_c)) if len(cum_c) else np.nan,
            'excess_h5': ev['excess_h5'], 'excess_h10': ev['excess_h10'],
            'excess_h20': ev['excess_h20'],
        })
    ev2 = pd.DataFrame(recs)

    # 主基准选择（预先固定规则）
    med_cov_sq = float(ev2['coverage_sq'].median())
    primary = 'sq' if med_cov_sq >= COVERAGE_PRIMARY_RULE else 'cum'
    bcol = f'breadth_{primary}'
    ev2['breadth'] = ev2[bcol]

    # 桶与切分
    ev2['bucket'] = np.where(ev2['temp_pct'].isna(), 'na',
                      np.where(ev2['temp_pct'] >= TEMP_TOP, 'top10',
                      np.where(ev2['temp_pct'] >= TEMP_HIGH, 'high', 'lowmid')))
    ev2['sponsored'] = np.where(ev2['breadth'].isna(), 'na',
                        np.where(ev2['breadth'] >= BREADTH_SPLIT, 'yes', 'no'))
    ok = (ev2['temp_pct'].notna()
          & ev2[f'coverage_{primary}'].ge(COVERAGE_EVENT_FLOOR))
    ev2['in_primary'] = ok

    def cell(df, h='excess_h10'):
        x = df[h].dropna() * 100
        if len(x) == 0:
            return {'n': 0}
        return {'n': int(len(x)), 'mean_pp': round(float(x.mean()), 2),
                'median_pp': round(float(x.median()), 2),
                'win_rate': round(float((x > 0).mean()), 3)}

    out = {'primary_basis': primary,
           'median_coverage_sq': round(med_cov_sq, 3),
           'median_coverage_cum': round(float(ev2['coverage_cum'].median()), 3),
           'events_total': int(len(ev2)),
           'events_with_temp': int(ev2['temp_pct'].notna().sum()),
           'events_in_primary': int(ok.sum()),
           'events_dropped_low_coverage': int(
               (ev2['temp_pct'].notna() & ~ev2[f'coverage_{primary}'].ge(
                   COVERAGE_EVENT_FLOOR)).sum()),
           'self_tests_passed': n_tests}

    # 主读数：高温桶内 breadth 切分
    hi = ev2[ok & (ev2['bucket'].isin(['high', 'top10']))]
    for h in ('excess_h5', 'excess_h10', 'excess_h20'):
        a = cell(hi[hi['sponsored'] == 'yes'], h)
        b = cell(hi[hi['sponsored'] == 'no'], h)
        out[f'high_temp_split_{h}'] = {
            'sponsored': a, 'not_sponsored': b,
            'diff_pp': (round(a['mean_pp'] - b['mean_pp'], 2)
                        if a.get('n') and b.get('n') else None)}

    # 2x2：温度桶 × 赞助
    cells = []
    for tb in ('lowmid', 'high', 'top10'):
        for sp in ('no', 'yes'):
            sub = ev2[ok & (ev2['bucket'] == tb) & (ev2['sponsored'] == sp)]
            row = {'temp_bucket': tb, 'sponsored': sp}
            for h in ('excess_h5', 'excess_h10', 'excess_h20'):
                c = cell(sub, h)
                row[f'{h}_n'] = c['n']
                row[f'{h}_mean_pp'] = c.get('mean_pp')
                row[f'{h}_win'] = c.get('win_rate')
            cells.append(row)
    cells_df = pd.DataFrame(cells)

    # 稳健性
    def diff_h10(df):
        a = df.loc[df['sponsored'] == 'yes', 'excess_h10'].dropna() * 100
        b = df.loc[df['sponsored'] == 'no', 'excess_h10'].dropna() * 100
        if len(a) == 0 or len(b) == 0:
            return None
        return round(float(a.mean() - b.mean()), 2)

    loyo = {}
    for y in sorted(hi['year'].unique()):
        loyo[int(y)] = diff_h10(hi[hi['year'] != y])
    loeo = {}
    for c in sorted(hi['code'].unique()):
        loeo[str(c)] = diff_h10(hi[hi['code'] != c])
    base_diff = out['high_temp_split_excess_h10']['diff_pp']
    loyo_vals = [v for v in loyo.values() if v is not None]
    robust = {
        'base_diff_pp': base_diff,
        'leave_one_year_out': loyo,
        'loyo_min': round(min(loyo_vals), 2) if loyo_vals else None,
        'loyo_max': round(max(loyo_vals), 2) if loyo_vals else None,
        'leave_one_etf_out': loeo,
        'loeo_same_sign_share': (
            round(float(np.mean([np.sign(v) == np.sign(base_diff)
                                 for v in loeo.values() if v is not None])), 3)
            if base_diff is not None else None),
        'year_2023': {
            'n': int((hi['year'] == 2023).sum()),
            'diff_pp': diff_h10(hi[hi['year'] == 2023]),
            'sponsored_n': int(((hi['year'] == 2023)
                                & (hi['sponsored'] == 'yes')).sum())},
        'spearman_breadth_temp': round(float(
            ev2.loc[ok, ['breadth', 'temp_pct']].corr(method='spearman')
            .iloc[0, 1]), 3),
        'spearman_breadth_excess_h10': round(float(
            ev2.loc[ok, ['breadth', 'excess_h10']].dropna()
            .corr(method='spearman').iloc[0, 1]), 3),
        'spearman_temp_excess_h10': round(float(
            ev2.loc[ok, ['temp_pct', 'excess_h10']].dropna()
            .corr(method='spearman').iloc[0, 1]), 3),
    }
    out['robustness'] = robust

    # 低温度组同切分（完整性）
    lo = ev2[ok & (ev2['bucket'] == 'lowmid')]
    out['low_temp_split_excess_h10'] = {
        'sponsored': cell(lo[lo['sponsored'] == 'yes']),
        'not_sponsored': cell(lo[lo['sponsored'] == 'no'])}
    # 全体事件 breadth 分位描述
    out['breadth_distribution'] = {
        'mean': round(float(ev2.loc[ok, 'breadth'].mean()), 3),
        'p25': round(float(ev2.loc[ok, 'breadth'].quantile(.25)), 3),
        'p50': round(float(ev2.loc[ok, 'breadth'].quantile(.5)), 3),
        'p75': round(float(ev2.loc[ok, 'breadth'].quantile(.75)), 3)}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ev2.to_csv(OUT_DIR / 'events.csv', index=False)
    cells_df.to_csv(OUT_DIR / 'cells.csv', index=False)
    with open(OUT_DIR / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
