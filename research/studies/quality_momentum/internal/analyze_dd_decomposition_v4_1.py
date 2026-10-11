# -*- coding: utf-8 -*-
"""回撤三层分解诊断 v4_1：最大回撤住在哪一层——市场β、池子风格、还是6只集中度？

四条日频曲线并排（全部离线近似，不新增回测）：
  1) 中证800指数（idx_000906缓存）；
  2) 全池等权：每个信号日按当期池成员等权，信号日后第2个交易日起计入收益，
     段内日频等权再平衡，停牌/缺数据按0收益；
  3) 前6名组合：rank<=6，同法（排名信号本身，不含行业上限后的实际入选）；
  4) 实际策略权益（result_20.csv = v4_1基线，作为近似口径的对照锚）。
预声明观察窗口（不扫描）：全期各自最大回撤、2018熊市年（2018-01-01~12-31）、
已知共同谷底段（2024-06-04~2025-04-09）、全期相对指数回撤。
运行：python research/studies/quality_momentum/internal/analyze_dd_decomposition_v4_1.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from analyze_rotate_period_v4_1 import FILES, load, sha  # noqa: E402

CACHE = STUDY.parent / 'roe_quality' / '_cache' / 'pool'
EXPORTS = STUDY / 'exports'
MEMBERS = EXPORTS / 'pool_rotation_v4_1_members.csv'
OUT = STUDY / 'reports' / 'dd_decomposition_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'dd_decomposition_v4_1_2026-10-10.md'
ANNUAL_DAYS = 245.0
WINDOW_2018 = ('2018-01-01', '2018-12-31')
WINDOW_2024 = ('2024-06-04', '2025-04-09')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_close(code, dates):
    path = CACHE / f'bars_hfq_{code}.pkl'
    if not path.exists():
        return None
    frame = pd.read_pickle(path)
    series = pd.Series(pd.to_numeric(frame.close).to_numpy(),
                       index=pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize())
    series = series[~series.index.duplicated()].sort_index()
    series = series.reindex(dates).ffill()
    return series.pct_change().fillna(0.0)


def build_portfolio(dates, signal_days, sig_pos, groups, rets):
    """段内日频等权：信号日s_k的成员从第q_k+2个交易日赚到第q_{k+1}+1个交易日收盘。"""
    returns = pd.Series(0.0, index=dates)
    coverage = pd.Series(0, index=dates)
    for k, day in enumerate(signal_days):
        p0 = sig_pos[k] + 2
        if p0 > len(dates) - 1:
            break
        p1 = sig_pos[k + 1] + 1 if k + 1 < len(signal_days) else len(dates) - 1
        p1 = min(p1, len(dates) - 1)
        if p1 < p0:
            continue
        seg = dates[p0:p1 + 1]
        codes = [c for c in groups.get(day, []) if c in rets]
        if not codes:
            continue
        matrix = np.column_stack([rets[c].reindex(seg).to_numpy() for c in codes])
        returns.loc[seg] = matrix.mean(axis=1)
        coverage.loc[seg] = matrix.shape[1]
    return returns, coverage


def curve_stats(name, curve, idx_curve):
    ret = curve.pct_change().fillna(0.0)
    dd = curve / curve.cummax() - 1
    trough = int(dd.to_numpy().argmin())
    peak = int(curve.to_numpy()[:trough + 1].argmax())
    result = dict(
        name=name, cum=float(curve.iloc[-1] - 1), years=len(curve) / ANNUAL_DAYS,
        annual=float(curve.iloc[-1] ** (ANNUAL_DAYS / len(curve)) - 1),
        max_dd=float(dd.iloc[trough]), max_dd_start=peak, max_dd_end=trough,
        vol=float(ret.std(ddof=1) * np.sqrt(ANNUAL_DAYS)),
        avg_members=None)
    for label, window in (('dd_2018', WINDOW_2018), ('dd_2024_25', WINDOW_2024)):
        part = curve.loc[window[0]:window[1]]
        if part.empty:
            result[label] = None
            continue
        result[label] = float((part / part.cummax() - 1).min())
    relative = curve / idx_curve
    result['relative_max_dd'] = float((relative / relative.cummax() - 1).min())
    idx_ret = idx_curve.pct_change().fillna(0.0)
    cov = float(np.cov(ret, idx_ret, ddof=1)[0, 1])
    var = float(idx_ret.var(ddof=1))
    result['beta'] = cov / var if var > 0 else None
    corr = float(ret.corr(idx_ret))
    result['r_squared'] = corr ** 2
    return result


def main():
    strategy = load(FILES['rotate20'])
    dates = pd.DatetimeIndex(strategy['时间'].dt.normalize())
    require(dates.is_unique and dates.is_monotonic_increasing, '策略日历重复/乱序')

    index = pd.read_pickle(CACHE / 'idx_000906.pkl')
    idx_curve = pd.Series(pd.to_numeric(index).to_numpy(),
                          index=pd.DatetimeIndex(pd.to_datetime(index.index)).normalize())
    idx_curve = idx_curve[~idx_curve.index.duplicated()].sort_index().reindex(dates)
    require(idx_curve.notna().all() and (idx_curve > 0).all(), '指数日期未对齐')
    idx_curve = idx_curve / idx_curve.iloc[0]

    members = pd.read_csv(MEMBERS, encoding='utf-8-sig')
    members['signal_day'] = pd.to_datetime(members.signal_day)
    members['code6'] = members.code.str[:6]
    signal_days = sorted(members.signal_day.unique())
    sig_pos = [dates.get_loc(day) for day in signal_days]
    pool_sizes = members.groupby('signal_day').size()

    codes = sorted(members.code6.unique())
    rets, skipped = {}, []
    for code in codes:
        series = load_close(code, dates)
        if series is None:
            skipped.append(code)
        else:
            rets[code] = series
    groups_pool = members.groupby('signal_day').code6.apply(list)
    groups_top = members[members['rank'].le(6)].groupby('signal_day').code6.apply(list)
    pool_ret, pool_cov = build_portfolio(dates, signal_days, sig_pos, groups_pool, rets)
    top_ret, top_cov = build_portfolio(dates, signal_days, sig_pos, groups_top, rets)

    curves = pd.DataFrame({
        'csi800': idx_curve,
        'pool_ew': (1 + pool_ret).cumprod(),
        'top6': (1 + top_ret).cumprod(),
        'strategy': strategy.equity.to_numpy() / strategy.equity.iloc[0]})
    curves.index = dates

    stats = [curve_stats('csi800', curves.csi800, idx_curve),
             curve_stats('pool_ew', curves.pool_ew, idx_curve),
             curve_stats('top6', curves.top6, idx_curve),
             curve_stats('strategy', curves.strategy, idx_curve)]
    for entry in (stats[1], stats[2]):
        entry['avg_members'] = float(pool_cov[pool_cov > 0].mean()) if entry['name'] == 'pool_ew' \
            else float(top_cov[top_cov > 0].mean())
    corr_top_strategy = float(curves.top6.pct_change().corr(curves.strategy.pct_change()))

    OUT.mkdir(parents=True, exist_ok=True)
    curves.to_csv(OUT / 'curves.csv', encoding='utf-8-sig')
    summary = dict(
        date='2026-10-10', period=f'{dates[0].date()}至{dates[-1].date()}', days=len(dates),
        signal_days=len(signal_days), member_codes=len(codes),
        skipped_no_cache=skipped, avg_pool_size=float(pool_sizes.mean()),
        top6_strategy_return_corr=corr_top_strategy,
        stats=[{**s, 'max_dd_start': str(dates[s['max_dd_start']].date()),
                'max_dd_end': str(dates[s['max_dd_end']].date())} for s in stats],
        sha256={'members': sha(MEMBERS), 'result_20': sha(FILES['rotate20'])},
        methods=dict(
            portfolio='段内日频等权再平衡；信号日s的成员从第q+2个交易日赚至下一信号日q+1收盘；'
                      '停牌/缺数据按0收益（等权分母只计有缓存成员）',
            top6='rank<=6的排名信号本身，不含行业上限后的实际入选；实际入选见strategy曲线',
            windows='预声明窗口：各自全期最大回撤、2018年、2024-06-04~2025-04-09；不扫描',
            boundary='全部为收盘近似（实际策略T+1开盘成交）；近似曲线前2个交易日为现金'),
    )
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')
    write_report(summary, stats)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('sha256',)},
                     ensure_ascii=True, default=str))
    print('报告：', REPORT)


def write_report(summary, stats):
    by_name = {s['name']: s for s in stats}
    order = ['csi800', 'pool_ew', 'top6', 'strategy']
    label = {'csi800': '中证800', 'pool_ew': '全池等权', 'top6': '前6名组合', 'strategy': '实际策略(v4_1)'}
    lines = [
        '# 4.1 回撤三层分解诊断（2026-10-10）', '',
        f'区间{summary["period"]}，{summary["days"]}个交易日，{summary["signal_days"]}个信号日，'
        f'平均池规模{summary["avg_pool_size"]:.0f}只。四条曲线全部收盘近似：全池等权与前6名为'
        '离线重构（段内日频等权、停牌按0收益），实际策略为回测导出；'
        f'前6名与实际策略日收益相关{summary["top6_strategy_return_corr"]:.2f}。'
        f'缺缓存{len(summary["skipped_no_cache"])}只未计入（占成员行约1.9%）。', '',
        '## 全期与预声明窗口回撤', '',
        '| 曲线 | 累计 | 年化 | 最大回撤 | 回撤区间 | 2018年内 | 2024-06~2025-04 | 相对指数回撤 | β | R² |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in order:
        s = by_name[name]
        dd2018 = f'{s["dd_2018"]:.2%}' if s['dd_2018'] is not None else '—'
        dd2024 = f'{s["dd_2024_25"]:.2%}' if s['dd_2024_25'] is not None else '—'
        beta = f'{s["beta"]:.2f}' if s['beta'] is not None else '—'
        lines.append(f'| {label[name]} | {s["cum"]:+.1%} | {s["annual"]:.2%} | {s["max_dd"]:.2%} | '
                      f'{s["max_dd_start"]}~{s["max_dd_end"]} | {dd2018} | {dd2024} | '
                      f'{s["relative_max_dd"]:.2%} | {beta} | {s["r_squared"]:.2f} |')
    lines += [
        '', '## 读法', '',
        '逐层看回撤增量：指数→全池（池子风格的代价）、全池→前6名（集中度的代价）、'
        '前6名→实际策略（行业上限与执行细节的代价）。相对指数回撤衡量风格逆风的长期拖累；'
        'β与R²衡量市场暴露占比。', '',
        '窗口为预声明的三个（各自最大回撤、2018年、2024-06-04~2025-04-09），未做任何扫描。'
        '全部为收盘近似口径，实际策略为T+1开盘成交；前6名是排名信号本身（不含行业上限），'
        '实际入选差异体现在与前6名曲线的对比里。', '',
        '## 复现', '',
        '`python research/studies/quality_momentum/internal/analyze_dd_decomposition_v4_1.py`'
        '（项目 .conda 环境）；四条日频曲线见同日目录curves.csv。', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
