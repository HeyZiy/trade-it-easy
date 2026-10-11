# -*- coding: utf-8 -*-
"""动量变体与入场过滤离线探针：B（完整动量）、C（近期确认）、D（高点约束）。

预声明设计（2026-10-11，不扫窗口、不加额外变体）：
- 数据：v4_1_2 导出 members（130期池快照），新浪后复权收盘缓存，中证800日历
  （2015-06-01起）。信号信息集截至信号日T的前一收盘T-1，与策略口径一致。
- 探针B（最近一个月信息是否有帮助）：
    m12_1 = C[T-22]/C[T-253]-1（在任12-1分数，先与members.momentum复算校验）
    m_full = C[T-1]/C[T-253]-1（不跳过最近1个月，分母与m12_1相同）
  每期各取前6名（同分按代码升序），配对比较等权前瞻收益
  （T+1收盘→T+1+h收盘，h=20/40/60）；另报两组合重叠数、换入/换出股票对照、
  各自前6名−全池均值。日历253日历史不足的前期剔除（约2016上半年）。
- 探针C（近期确认）：ret21 = C[T-1]/C[T-22]-1；blocked = ret21<=-10%。
- 探针D（高点约束）：dd120 = C[T-1]/max(C[T-120..T-1])-1；blocked = dd120<=-20%。
  C/D沿用行业上限探针模板：blocked候选 vs allowed（全体ranked成员与实际
  selected买入两层）的前瞻收益对比；v4_1_2不顺延补位，被挡即空仓，故同时
  对照现金（前瞻收益符号）；报selected被挡数量与受影响期数、60日内跌幅
  <=-15%发生率、分年拆分。
- 统计：以期为单位，naive t 与 Newey-West t（lag=期限//20）；不宣称窗口独立。
- 边界：收盘近似（无开盘/涨跌停/费用）；排名层观察而非组合模拟；停牌缺口为
  NaN剔除不填零；前瞻窗口止于缓存末端标记censored。
运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_momentum_variants_probe.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[2]
CACHE = ROOT / 'research/studies/roe_quality/_cache/pool'
OUT = STUDY / 'reports' / 'momentum_variants_probe_2026-10-11'
REPORT = STUDY / 'reports' / 'momentum_variants_probe_2026-10-11.md'
EXPORTS = STUDY / 'exports'
MEMBERS = EXPORTS / 'pool_rotation_v4_1_2_members.csv'
HORIZONS = (20, 40, 60)
TOP_N = 6
C_THRESHOLD = -0.10   # ret21<=-10% 挡下
D_THRESHOLD = -0.20   # 距120日最高回撤<=-20% 挡下
SEVERE = -0.15


def require(condition, message):
    if not condition:
        raise ValueError(message)


def missing_codes(codes6):
    """缺缓存的代码（多为退市股，新浪已无数据，2026-10-11实测补抓必然失败），
    直接剔除并报告，不做网络补抓（akshare内嵌V8多线程抓取会崩溃进程）。"""
    return [c for c in codes6 if not (CACHE / f'bars_hfq_{c}.pkl').exists()]


def nw_t(series, lag):
    values = series.dropna().to_numpy()
    n = len(values)
    if n < 3:
        return np.nan
    errors = values - values.mean()
    gamma = float((errors * errors).sum() / n)
    for l in range(1, lag + 1):
        gamma += 2 * (1 - l / (lag + 1)) * float((errors[l:] * errors[:-l]).sum() / n)
    se = math.sqrt(max(gamma, 0.0) / n)
    return float(values.mean() / se) if se > 0 else np.nan


def naive_t(series):
    values = series.dropna()
    if len(values) < 3 or values.std(ddof=1) == 0:
        return np.nan
    return float(values.mean() / (values.std(ddof=1) / math.sqrt(len(values))))


def clean(obj):
    """递归把非有限浮点替换为None，便于json allow_nan=False序列化。"""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return float(obj) if np.isfinite(obj) else None
    return obj


def summarize(diffs):
    """每期差值序列 -> 汇总dict（各期限）。"""
    out = {}
    for h in HORIZONS:
        s = diffs.get(h)
        if s is None or len(s.dropna()) < 3:
            out[str(h)] = dict(periods=int(len(s) if s is not None else 0), mean=np.nan,
                               median=np.nan, positive_share=np.nan,
                               naive_t=np.nan, nw_t=np.nan)
            continue
        s = s.dropna()
        out[str(h)] = dict(periods=int(len(s)), mean=float(s.mean()),
                           median=float(s.median()),
                           positive_share=float(s.gt(0).mean()),
                           naive_t=naive_t(s), nw_t=nw_t(s, h // 20))
    return out


def yearly(diffs, label):
    rows = []
    for h in HORIZONS:
        s = diffs.get(h)
        if s is None or s.empty:
            continue
        frame = s.dropna().to_frame('diff')
        frame['year'] = frame.index.year
        g = frame.groupby('year')['diff']
        for year, v in g:
            rows.append(dict(probe=label, horizon=h, year=int(year), periods=int(len(v)),
                             mean=float(v.mean()), positive=int(v.gt(0).sum())))
    return pd.DataFrame(rows)


def main():
    members = pd.read_csv(MEMBERS, encoding='utf-8-sig')
    require(not members.empty, 'members为空')
    members['signal_day'] = pd.to_datetime(members.signal_day)
    members['code6'] = members.code.str[:6]
    require(not members.duplicated(['signal_day', 'code']).any(), '同信号日成员重复')
    ranked = members[members['rank'].notna()].copy()
    print(f'ranked成员 {len(ranked)} 条 / {ranked.signal_day.nunique()} 期 / '
          f'{ranked.code6.nunique()} 只', flush=True)

    codes6 = sorted(ranked.code6.unique())
    failed = missing_codes(codes6)
    n_missing_rows = int(ranked.code6.isin(set(failed)).sum())
    print(f'缺缓存（退市等，剔除不补抓）：{n_missing_rows} 条成员记录 / {len(failed)} 只',
          flush=True)

    calendar_src = pd.read_pickle(CACHE / 'idx_000906.pkl')
    calendar = pd.DatetimeIndex(pd.to_datetime(calendar_src.index)).normalize().sort_values()
    require(calendar.is_unique, '基准日历重复')
    pos = {day: i for i, day in enumerate(calendar)}
    n_days = len(calendar)

    arrs = {}
    for code in codes6:
        path = CACHE / f'bars_hfq_{code}.pkl'
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        require(list(frame.columns) == ['date', 'close'], f'{code}行情结构错误')
        dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
        series = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(),
                           index=dates)
        require(series.index.is_unique and series.index.is_monotonic_increasing,
                f'{code}行情乱序')
        require(series.gt(0).all(), f'{code}行情含无效值')
        arrs[code] = series.reindex(calendar).to_numpy(dtype=float)

    rows = []
    for rec in ranked.itertuples():
        arr = arrs.get(rec.code6)
        base = pos.get(rec.signal_day)
        if arr is None or base is None:
            continue
        row = dict(signal_day=rec.signal_day, code=rec.code, code6=rec.code6,
                   rank=int(rec.rank), kept=bool(rec.kept), selected=bool(rec.selected),
                   entry_cap_blocked=bool(rec.entry_cap_blocked),
                   momentum_jq=rec.momentum)
        c_prev = arr[base - 1] if base >= 1 else np.nan
        c_22 = arr[base - 22] if base >= 22 else np.nan
        c_253 = arr[base - 253] if base >= 253 else np.nan
        row['m121'] = c_22 / c_253 - 1 if np.isfinite(c_22) and np.isfinite(c_253) else np.nan
        row['m_full'] = c_prev / c_253 - 1 if np.isfinite(c_prev) and np.isfinite(c_253) else np.nan
        row['ret21'] = c_prev / c_22 - 1 if np.isfinite(c_prev) and np.isfinite(c_22) else np.nan
        if base >= 120 and np.isfinite(c_prev):
            window = arr[base - 120:base]
            window = window[np.isfinite(window)]
            row['dd120'] = c_prev / window.max() - 1 if window.size else np.nan
        else:
            row['dd120'] = np.nan
        entry = arr[base + 1] if base + 1 < n_days else np.nan
        for h in HORIZONS:
            exit_close = arr[base + 1 + h] if base + 1 + h < n_days else np.nan
            if np.isfinite(entry) and np.isfinite(exit_close):
                row[f'r{h}'] = exit_close / entry - 1
            else:
                row[f'r{h}'] = np.nan
        rows.append(row)
    sig = pd.DataFrame(rows)
    require(not sig.empty, '无有效观测')
    OUT.mkdir(parents=True, exist_ok=True)
    sig.to_csv(OUT / 'member_signals.csv', index=False, encoding='utf-8-sig')

    # ---- 在任分数复算校验（m121 vs 平台momentum） ----
    check = sig[['signal_day', 'code', 'm121', 'momentum_jq']].dropna()
    check_abs = (check.m121 - check.momentum_jq).abs()
    top_overlap = []
    for day, frame in check.groupby('signal_day'):
        t1 = set(frame.sort_values(['m121', 'code'], ascending=[False, True]).head(TOP_N).code)
        t2 = set(frame.sort_values(['momentum_jq', 'code'], ascending=[False, True]).head(TOP_N).code)
        top_overlap.append(len(t1 & t2))
    validate = dict(rows=int(len(check)), median_abs_diff=float(check_abs.median()),
                    p90_abs_diff=float(check_abs.quantile(0.9)),
                    share_within_1e6=float(check_abs.le(1e-6).mean()),
                    top6_overlap_mean=float(np.mean(top_overlap)))
    print('[校验] m121复算 vs 平台momentum：', validate, flush=True)

    summary = dict(
        date='2026-10-11',
        design='预声明B/C/D三个探针，不扫窗口；口径见脚本docstring',
        ranked_rows=int(len(sig)), signal_days=int(sig.signal_day.nunique()),
        member_codes=int(sig.code6.nunique()), missing_codes=failed,
        missing_rows=n_missing_rows,
        b_rows_valid=int(sig.m_full.notna().sum()),
        c_blocked_def=f'ret21<={C_THRESHOLD:.0%}',
        d_blocked_def=f'dd120<={D_THRESHOLD:.0%}',
        m121_validation=validate,
    )

    # ================= 探针B =================
    bdata = sig[sig.m_full.notna() & sig.m121.notna()].copy()
    b_periods = bdata.signal_day.nunique()
    spearman = float(bdata[['m121', 'm_full']].corr(method='spearman').iloc[0, 1])
    diff_by_h = {h: [] for h in HORIZONS}
    overlap_rows, swap_rows = [], []
    top_minus_pool = {arm: {h: [] for h in HORIZONS} for arm in ('m121', 'm_full')}
    for day, frame in bdata.groupby('signal_day'):
        top = {}
        for arm in ('m121', 'm_full'):
            top[arm] = frame.sort_values([arm, 'code'], ascending=[False, True]).head(TOP_N)
        for h in HORIZONS:
            m121_ret, mfull_ret = top['m121'][f'r{h}'].mean(), top['m_full'][f'r{h}'].mean()
            if np.isfinite(m121_ret) and np.isfinite(mfull_ret):
                diff_by_h[h].append((day, mfull_ret - m121_ret))
            if np.isfinite(top['m121'][f'r{h}'].mean()):
                top_minus_pool['m121'][h].append(
                    (day, top['m121'][f'r{h}'].mean() - frame[f'r{h}'].mean()))
            if np.isfinite(top['m_full'][f'r{h}'].mean()):
                top_minus_pool['m_full'][h].append(
                    (day, top['m_full'][f'r{h}'].mean() - frame[f'r{h}'].mean()))
        overlap = len(set(top['m121'].code) & set(top['m_full'].code))
        overlap_rows.append(dict(signal_day=day, overlap=overlap))
        swapped_in = top['m_full'][~top['m_full'].code.isin(top['m121'].code)]
        swapped_out = top['m121'][~top['m121'].code.isin(top['m_full'].code)]
        for rec in swapped_in.itertuples():
            swap_rows.append(dict(signal_day=day, code=rec.code, side='in',
                                  rank_m121=int(rec.rank), m121=rec.m121,
                                  m_full=rec.m_full, r20=rec.r20, r40=rec.r40, r60=rec.r60))
        for rec in swapped_out.itertuples():
            swap_rows.append(dict(signal_day=day, code=rec.code, side='out',
                                  rank_m121=int(rec.rank), m121=rec.m121,
                                  m_full=rec.m_full, r20=rec.r20, r40=rec.r40, r60=rec.r60))
    b_diffs = {h: pd.Series({d: v for d, v in diff_by_h[h]}).sort_index()
               for h in HORIZONS}
    b_diffs = {h: s for h, s in b_diffs.items() if not s.empty}
    b_top_pool = {arm: {h: pd.Series({d: v for d, v in seq}).sort_index()
                        for h, seq in top_minus_pool[arm].items() if seq}
                  for arm in ('m121', 'm_full')}
    b_yearly = pd.concat([yearly(b_diffs, 'B_full_minus_m121'),
                          yearly(b_top_pool['m121'], 'B_m121_top_minus_pool'),
                          yearly(b_top_pool['m_full'], 'B_mfull_top_minus_pool')],
                         ignore_index=True)
    swap = pd.DataFrame(swap_rows)
    swap_summary = {}
    if not swap.empty:
        for h in HORIZONS:
            in_mean = swap[swap.side.eq('in')][f'r{h}'].mean()
            out_mean = swap[swap.side.eq('out')][f'r{h}'].mean()
            swap_summary[str(h)] = dict(n_in=int(swap.side.eq('in').sum()),
                                        n_out=int(swap.side.eq('out').sum()),
                                        in_mean=float(in_mean) if np.isfinite(in_mean) else None,
                                        out_mean=float(out_mean) if np.isfinite(out_mean) else None,
                                        in_minus_out=float(in_mean - out_mean)
                                        if np.isfinite(in_mean) and np.isfinite(out_mean) else None)
    b_diffs_frame = pd.DataFrame(
        [{**{'signal_day': d}, **{f'diff_r{h}': b_diffs[h].get(d, np.nan) for h in HORIZONS}}
         for d in sorted(set().union(*[set(s.index) for s in b_diffs.values()]))])
    b_diffs_frame.to_csv(OUT / 'b_period_diffs.csv', index=False, encoding='utf-8-sig')
    b_yearly.to_csv(OUT / 'b_yearly.csv', index=False, encoding='utf-8-sig')
    if not swap.empty:
        swap.to_csv(OUT / 'b_swapped.csv', index=False, encoding='utf-8-sig')
    overlap_frame = pd.DataFrame(overlap_rows)
    summary['probe_b'] = dict(
        periods=int(b_periods), spearman_m121_mfull=spearman,
        overlap_mean=float(overlap_frame.overlap.mean()),
        diff_full_minus_m121=summarize(b_diffs),
        m121_top_minus_pool=summarize(b_top_pool['m121']),
        mfull_top_minus_pool=summarize(b_top_pool['m_full']),
        swapped=swap_summary)
    print('[B] 完整动量−12-1：', json.dumps(clean(summary['probe_b']), ensure_ascii=False,
                                         default=str), flush=True)

    # ================= 探针C/D =================
    def run_filter_probe(flag_col, label):
        data = sig[sig[flag_col].notna()].copy()
        data['blocked'] = data[flag_col].astype(bool)
        diff_rows, sel_blocked = [], []
        for day, frame in data.groupby('signal_day'):
            allowed = frame[~frame.blocked]
            blocked = frame[frame.blocked]
            for h in HORIZONS:
                a, b = allowed[f'r{h}'].mean(), blocked[f'r{h}'].mean()
                if np.isfinite(a) and np.isfinite(b):
                    diff_rows.append(dict(signal_day=day, horizon=h,
                                          diff_allowed_minus_blocked=a - b,
                                          n_allowed=int(len(allowed)), n_blocked=int(len(blocked))))
            sel = frame[frame.selected & frame.blocked]
            for rec in sel.itertuples():
                sel_blocked.append(dict(signal_day=day, code=rec.code, rank=int(rec.rank),
                                        r20=rec.r20, r40=rec.r40, r60=rec.r60))
        diffs = pd.DataFrame(diff_rows)
        diffs.to_csv(OUT / f'{label}_period_diffs.csv', index=False, encoding='utf-8-sig')
        y = yearly({h: diffs[diffs.horizon.eq(h)].set_index('signal_day')
                    ['diff_allowed_minus_blocked'] for h in HORIZONS if not diffs.empty},
                   label)
        if y.empty:
            y = pd.DataFrame(columns=['probe', 'horizon', 'year', 'periods',
                                      'mean', 'positive'])
        y.to_csv(OUT / f'{label}_yearly.csv', index=False, encoding='utf-8-sig')
        diff_by_h = {h: diffs[diffs.horizon.eq(h)].set_index('signal_day')
                     ['diff_allowed_minus_blocked'] for h in HORIZONS}
        # 实际selected买入中被挡的名单与对照：同日同层allowed的selected均值
        sel_block_frame = pd.DataFrame(sel_blocked)
        sel_summary = dict(
            selected_rows=int(sig.selected.sum()),
            selected_blocked=int(len(sel_block_frame)),
            affected_periods=int(sel_block_frame.signal_day.nunique()) if not sel_block_frame.empty else 0)
        if not sel_block_frame.empty:
            for h in HORIZONS:
                vals = sel_block_frame[f'r{h}'].dropna()
                sel_summary[f'blocked_selected_r{h}_mean'] = float(vals.mean()) if len(vals) else None
            sel_block_frame.to_csv(OUT / f'{label}_selected_blocked.csv',
                                   index=False, encoding='utf-8-sig')
        # 60日大亏发生率
        ok60 = data[data.r60.notna()]
        severe = dict(
            blocked=float(ok60[ok60.blocked].r60.le(SEVERE).mean()) if ok60.blocked.any() else None,
            allowed=float(ok60[~ok60.blocked].r60.le(SEVERE).mean()) if (~ok60.blocked).any() else None,
            blocked_n=int(ok60.blocked.sum()), allowed_n=int((~ok60.blocked).sum()))
        return dict(
            rows=int(len(data)), blocked_rows=int(data.blocked.sum()),
            blocked_share=float(data.blocked.mean()),
            blocked_periods=int(data[data.blocked].signal_day.nunique()),
            diff_allowed_minus_blocked=summarize(diff_by_h),
            yearly_mean_r20={str(int(yr)): float(v) for yr, v in
                             y[y.horizon.eq(20)].set_index('year')['mean'].items()},
            yearly_mean_r60={str(int(yr)): float(v) for yr, v in
                             y[y.horizon.eq(60)].set_index('year')['mean'].items()},
            selected=sel_summary, severe60=severe), sel_block_frame

    sig['c_blocked'] = np.where(sig.ret21.notna(), sig.ret21 <= C_THRESHOLD, np.nan)
    sig['d_blocked'] = np.where(sig.dd120.notna(), sig.dd120 <= D_THRESHOLD, np.nan)
    probe_c, _ = run_filter_probe('c_blocked', 'c')
    probe_d, _ = run_filter_probe('d_blocked', 'd')
    summary['probe_c'] = probe_c
    summary['probe_d'] = probe_d
    print('[C] 近期确认：', json.dumps(clean(probe_c), ensure_ascii=False, default=str), flush=True)
    print('[D] 高点约束：', json.dumps(clean(probe_d), ensure_ascii=False, default=str), flush=True)

    (OUT / 'summary.json').write_text(
        json.dumps(clean(summary), ensure_ascii=False, indent=2, allow_nan=False,
                   default=str),
        encoding='utf-8')
    write_report(summary)
    print('[输出]', OUT, flush=True)


def fmt(d, key):
    v = d.get(key)
    return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f'{v:.2%}'


def sel_line(sel):
    if not sel.get('selected_blocked'):
        return '被挡的实际买入：0笔。'
    return (f"被挡的实际买入 {sel['selected_blocked']}/{sel['selected_rows']} 笔，事后 "
            f"r20 {fmt(sel, 'blocked_selected_r20_mean')} / "
            f"r40 {fmt(sel, 'blocked_selected_r40_mean')} / "
            f"r60 {fmt(sel, 'blocked_selected_r60_mean')}"
            f"（v4_1_2被挡即空仓，对照现金0收益）。")


def table(measures):
    rows = ['| 期限 | 期数 | 差值均值 | 中位数 | 正差占比 | naive t | NW t |',
            '| --- | --- | --- | --- | --- | --- | --- |']
    for h in HORIZONS:
        m = measures[str(h)]
        rows.append(f'| {h} | {m["periods"]} | {m["mean"]:.2%} | {m["median"]:.2%} | '
                    f'{m["positive_share"]:.1%} | {m["naive_t"]:.2f} | {m["nw_t"]:.2f} |')
    return '\n'.join(rows)


def write_report(s):
    b = s['probe_b']
    c, d = s['probe_c'], s['probe_d']
    lines = [
        '# 动量变体与入场过滤离线探针（2026-10-11）', '',
        f"数据：v4_1_2导出 {s['ranked_rows']} 条ranked成员、{s['signal_days']} 期、"
        f"{s['member_codes']} 只；缺缓存剔除（退市等） {len(s['missing_codes'])} 只。"
        f"预声明B/C/D三探针，不扫窗口。", '',
        '在任分数复算校验（m121 vs 平台momentum）：'
        f"{s['m121_validation']['rows']} 行可比，中位绝对差 "
        f"{s['m121_validation']['median_abs_diff']:.2e}"
        f"（新浪/聚宽复权因子差异量级），复算前6与平台分数前6平均重叠 "
        f"{s['m121_validation']['top6_overlap_mean']:.2f}/6。", '',
        '## 探针B：完整动量（不跳月）− 12-1，每期前6名配对', '',
        f"两分数Spearman相关 {b['spearman_m121_mfull']:.3f}；"
        f"B可比期数 {b['periods']}（253日历史不足的前期剔除）；"
        f"两组合前6名平均重叠 {b['overlap_mean']:.2f}/6。", '',
        table(b['diff_full_minus_m121']), '',
        '换入/换出（m_full前6独有 vs m12_1前6独有）：',
        '| 期限 | 换入n | 换入均值 | 换出n | 换出均值 | 换入−换出 |', '| --- | --- | --- | --- | --- | --- |',
    ]
    for h in HORIZONS:
        sw = b['swapped'].get(str(h), {})
        rows = [sw.get('n_in'), sw.get('in_mean'), sw.get('n_out'), sw.get('out_mean'),
                sw.get('in_minus_out')]
        cells = [('—' if v is None else (str(v) if not isinstance(v, float) else
                 (f'{v:.2%}' if 'mean' in k or 'minus' in k else str(v))))
                 for k, v in zip(['n_in', 'in_mean', 'n_out', 'out_mean', 'in_minus_out'], rows)]
        lines.append('| ' + ' | '.join([str(h)] + cells) + ' |')
    lines += [
        '', f"12-1前6−全池均值：", table(b['m121_top_minus_pool']), '',
        f"完整动量前6−全池均值：", table(b['mfull_top_minus_pool']), '',
        '## 探针C：近期确认（最近21日收益<=−10%挡下）', '',
        f"blocked {c['blocked_rows']} 条 / {c['rows']} 条（{c['blocked_share']:.1%}），"
        f"涉及 {c['blocked_periods']} 期。", '',
        'allowed − blocked 前瞻收益差（全体ranked成员）：', table(c['diff_allowed_minus_blocked']), '',
        sel_line(c['selected']),
        '60日内跌幅<=−15%发生率：blocked ' + fmt(c['severe60'], 'blocked')
        + f"（n={c['severe60']['blocked_n']}） vs allowed " + fmt(c['severe60'], 'allowed')
        + f"（n={c['severe60']['allowed_n']}）", '',
        '## 探针D：高点约束（距120日最高回撤<=−20%挡下）', '',
        f"blocked {d['blocked_rows']} 条 / {d['rows']} 条（{d['blocked_share']:.1%}），"
        f"涉及 {d['blocked_periods']} 期。", '',
        'allowed − blocked 前瞻收益差（全体ranked成员）：', table(d['diff_allowed_minus_blocked']), '',
        sel_line(d['selected']),
        '60日内跌幅<=−15%发生率：blocked ' + fmt(d['severe60'], 'blocked')
        + f"（n={d['severe60']['blocked_n']}） vs allowed " + fmt(d['severe60'], 'allowed')
        + f"（n={d['severe60']['allowed_n']}）", '',
        '## 边界', '',
        '收盘近似（无开盘/涨跌停/费用）；排名层观察而非组合模拟；停牌缺口剔除不填零；'
        '前瞻窗口T+1收盘→T+1+h收盘，止于缓存末端；B探针受日历起点2015-06-01限制；'
        '统计以期为单位，NW lag=期限//20，不宣称窗口独立。', '',
        '明细见 ' + str(OUT.name) + '/（member_signals、各探针period_diffs/yearly、summary.json）。',
    ]
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
