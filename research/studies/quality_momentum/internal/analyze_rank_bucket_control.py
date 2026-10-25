# -*- coding: utf-8 -*-
"""P1 对照：池内排名分桶，检验动量腿是否只是装饰。

数据：rank_validity_v4_1_2026-10-10/member_forward_returns.csv（130期，逐成员前瞻收益）。
不新增回测、不改基线码；只读现有 CSV。

预注册判线（先写死，跑完不改）：
  主检验：top6 − rank7-12 的 20 日前瞻收益差（以期为单位）。
  - 20日差值 <= 0，或 NW t < 1.0  -> 动量腿未通过（可视为装饰）。
  - 40日与60日差值均 > 0 且 NW t >= 1.5 -> 动量腿在40-60日有效，
    20日检查节拍与兑现期错配，才值得进入 P2（40/60日账户对照）。
  - 其余 -> 不确定，不进入 P2。
  辅助（不改判）：分桶均值是否随 rank 单调下降；分年正差占比；样本覆盖。

方法边界：
  - 前瞻收益为 T+1 收盘 -> T+1+h 收盘；停牌/缺收盘剔除，不填零。
  - 20日期限窗口基本不重叠；40/60日重叠，NW lag=期限//20，不宣称独立样本。
  - 这是排名层观察，不含行业上限、留存、费用与执行；不能直接当账户收益。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
SOURCE = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10' / 'member_forward_returns.csv'
OUT = STUDY / 'reports' / 'rank_bucket_control_2026-10-21'
REPORT = STUDY / 'reports' / 'rank_bucket_control_2026-10-21.md'
HORIZONS = (20, 40, 60)
BUCKETS = ((1, 6), (7, 12), (13, 24), (25, 48), (49, 10 ** 9))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nw_t(series: pd.Series, lag: int) -> float:
    values = series.dropna().to_numpy()
    n = len(values)
    if n < 3:
        return float('nan')
    errors = values - values.mean()
    gamma = float((errors * errors).sum() / n)
    for ell in range(1, lag + 1):
        gamma += 2 * (1 - ell / (lag + 1)) * float((errors[ell:] * errors[:-ell]).sum() / n)
    se = math.sqrt(max(gamma, 0.0) / n)
    return float(values.mean() / se) if se > 0 else float('nan')


def naive_t(series: pd.Series) -> float:
    values = series.dropna()
    if len(values) < 3 or values.std(ddof=1) == 0:
        return float('nan')
    return float(values.mean() / (values.std(ddof=1) / math.sqrt(len(values))))


def bucket_label(low: int, high: int) -> str:
    return f'{low}-{high}' if high < 10 ** 9 else f'{low}+'


def summarize_diff(series: pd.Series, lag: int) -> dict:
    values = series.dropna()
    return dict(
        periods=int(len(values)),
        mean=float(values.mean()) if len(values) else float('nan'),
        median=float(values.median()) if len(values) else float('nan'),
        positive_share=float(values.gt(0).mean()) if len(values) else float('nan'),
        naive_t=naive_t(values),
        nw_t=nw_t(values, lag),
    )


def main() -> None:
    require(SOURCE.exists(), f'找不到 {SOURCE}')
    df = pd.read_csv(SOURCE, encoding='utf-8-sig')
    df['signal_day'] = pd.to_datetime(df.signal_day)
    require(not df.empty and df.signal_day.nunique() >= 100, '样本不足')
    df['year'] = df.signal_day.dt.year

    rows = []
    ic_rows = []
    for horizon in HORIZONS:
        key = f'r{horizon}'
        status = f'r{horizon}_status'
        sample = df[df[status].eq('ok') & df[key].notna() & df['rank'].notna()].copy()
        require(len(sample) > 0, f'{horizon}日无有效观测')
        for day, frame in sample.groupby('signal_day'):
            frame = frame.sort_values('rank')
            rec = dict(signal_day=day, horizon=horizon, year=day.year, n=len(frame))
            for low, high in BUCKETS:
                part = frame[(frame['rank'] >= low) & (frame['rank'] <= high)]
                rec[f'bucket_{bucket_label(low, high)}'] = part[key].mean()
                rec[f'n_{bucket_label(low, high)}'] = len(part)
            top = frame[frame['rank'] <= 6]
            b712 = frame[(frame['rank'] >= 7) & (frame['rank'] <= 12)]
            b1324 = frame[(frame['rank'] >= 13) & (frame['rank'] <= 24)]
            b25 = frame[frame['rank'] >= 25]
            rec['top6_mean'] = top[key].mean() if len(top) else np.nan
            rec['b712_mean'] = b712[key].mean() if len(b712) else np.nan
            rec['b1324_mean'] = b1324[key].mean() if len(b1324) else np.nan
            rec['b25_mean'] = b25[key].mean() if len(b25) else np.nan
            rec['top6_minus_712'] = (rec['top6_mean'] - rec['b712_mean']
                                     if len(top) and len(b712) else np.nan)
            rec['top6_minus_1324'] = (rec['top6_mean'] - rec['b1324_mean']
                                      if len(top) and len(b1324) else np.nan)
            rec['top6_minus_25'] = (rec['top6_mean'] - rec['b25_mean']
                                    if len(top) and len(b25) else np.nan)
            rec['top6_minus_rest'] = (top[key].mean() - frame[frame['rank'] > 6][key].mean()
                                      if len(top) and len(frame[frame['rank'] > 6]) else np.nan)
            rows.append(rec)
            if len(frame) >= 10:
                ic = frame['rank'].corr(frame[key], method='spearman')
                ic_rows.append(dict(signal_day=day, horizon=horizon, year=day.year,
                                    rank_ic=float(ic) if pd.notna(ic) else np.nan))

    periods = pd.DataFrame(rows)
    ic = pd.DataFrame(ic_rows)
    OUT.mkdir(parents=True, exist_ok=True)
    periods.to_csv(OUT / 'period_buckets.csv', index=False, encoding='utf-8-sig')
    ic.to_csv(OUT / 'period_rank_ic.csv', index=False, encoding='utf-8-sig')

    summary = {}
    yearly = []
    for horizon in HORIZONS:
        frame = periods[periods.horizon.eq(horizon)]
        lag = horizon // 20
        buckets = {}
        for low, high in BUCKETS:
            label = bucket_label(low, high)
            series = frame[f'bucket_{label}'].dropna()
            buckets[label] = dict(
                periods=int(len(series)),
                mean=float(series.mean()) if len(series) else float('nan'),
                median=float(series.median()) if len(series) else float('nan'),
            )
        diffs = {}
        for name, col in (('top6_minus_712', 'top6_minus_712'),
                          ('top6_minus_1324', 'top6_minus_1324'),
                          ('top6_minus_25', 'top6_minus_25'),
                          ('top6_minus_rest', 'top6_minus_rest')):
            diffs[name] = summarize_diff(frame[col], lag)
        ic_series = ic[ic.horizon.eq(horizon)].rank_ic.dropna()
        summary[str(horizon)] = dict(
            periods=int(len(frame)), buckets=buckets, diffs=diffs,
            rank_ic=dict(periods=int(len(ic_series)),
                         mean=float(ic_series.mean()) if len(ic_series) else float('nan'),
                         nw_t=nw_t(ic_series, lag)),
        )
        by_year = frame.groupby('year').top6_minus_712.agg(['count', 'mean']).reset_index()
        by_year['horizon'] = horizon
        yearly.append(by_year)
    yearly = pd.concat(yearly, ignore_index=True)
    yearly.to_csv(OUT / 'yearly_top6_minus_712.csv', index=False, encoding='utf-8-sig')

    # 判线
    d20 = summary['20']['diffs']['top6_minus_712']
    d40 = summary['40']['diffs']['top6_minus_712']
    d60 = summary['60']['diffs']['top6_minus_712']
    if d20['mean'] <= 0 or (pd.notna(d20['nw_t']) and d20['nw_t'] < 1.0):
        verdict = 'fail'
    elif (d40['mean'] > 0 and d60['mean'] > 0
          and pd.notna(d40['nw_t']) and pd.notna(d60['nw_t'])
          and d40['nw_t'] >= 1.5 and d60['nw_t'] >= 1.5):
        verdict = 'horizon_edge'
    else:
        verdict = 'inconclusive'
    summary['verdict'] = verdict

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')

    def pct(value: float) -> str:
        return '—' if pd.isna(value) else f'{value:+.2%}'

    def num(value: float, digits: int = 2) -> str:
        return '—' if pd.isna(value) else f'{value:.{digits}f}'

    lines = [
        '# P1 池内排名分桶对照（2026-10-21）', '',
        f'数据：`{SOURCE.relative_to(STUDY.parents[2])}`；'
        f'130期、{df.code6.nunique()}只、{len(df):,}条成员记录。',
        '口径：T+1收盘至T+1+h收盘；停牌/缺收盘剔除；20日窗口基本不重叠，'
        '40/60日重叠以NW(lag=h//20)修正。', '',
        '## 分桶前瞻收益（期均值）', '',
        '| 期限 | 1-6 | 7-12 | 13-24 | 25-48 | 49+ |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for horizon in HORIZONS:
        b = summary[str(horizon)]['buckets']
        lines.append(f'| {horizon}日 | ' + ' | '.join(
            pct(b[bucket_label(low, high)]['mean']) for low, high in BUCKETS) + ' |')
    lines += ['', '## top6 − rank7-12（主检验）', '',
              '| 期限 | 期数 | 均值 | 中位 | 正差占比 | naive t | NW t | rank IC |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for horizon in HORIZONS:
        s = summary[str(horizon)]
        d = s['diffs']['top6_minus_712']
        lines.append(f'| {horizon}日 | {d["periods"]} | {pct(d["mean"])} | {pct(d["median"])} | '
                     f'{d["positive_share"]:.1%} | {num(d["naive_t"])} | {num(d["nw_t"])} | '
                     f'{num(s["rank_ic"]["mean"], 3)} |')
    lines += ['', '## top6 相对其他分桶（诊断）', '',
              '| 期限 | −7-12 | −13-24 | −25+ | −全部其余 |',
              '| --- | ---: | ---: | ---: | ---: |']
    for horizon in HORIZONS:
        d = summary[str(horizon)]['diffs']
        lines.append(f'| {horizon}日 | {pct(d["top6_minus_712"]["mean"])} | '
                     f'{pct(d["top6_minus_1324"]["mean"])} | {pct(d["top6_minus_25"]["mean"])} | '
                     f'{pct(d["top6_minus_rest"]["mean"])} |')
    lines += ['', '## 分年：top6 − rank7-12 均值', '',
              '| 年 | 20日 | 40日 | 60日 |', '| --- | ---: | ---: | ---: |']
    pivot = yearly.pivot(index='year', columns='horizon', values='mean')
    for year, row in pivot.iterrows():
        lines.append(f'| {year} | {pct(row.get(20))} | {pct(row.get(40))} | {pct(row.get(60))} |')
    lines += ['', '## 判线', '',
              f'- 20日 top6−7-12：均值 {pct(d20["mean"])}，NW t {num(d20["nw_t"])}。',
              f'- 40日：均值 {pct(d40["mean"])}，NW t {num(d40["nw_t"])}；'
              f'60日：均值 {pct(d60["mean"])}，NW t {num(d60["nw_t"])}。',
              f'- **判定：`{verdict}`**。'
              + (' 20日不显著或为负，动量腿未通过；不进入P2。' if verdict == 'fail'
                 else ' 40/60日同向为正且NW t≥1.5，存在“20日检查、40-60日兑现”的错配；'
                      '可进入P2，但P2需先写分年一致性判线。' if verdict == 'horizon_edge'
                 else ' 未同时满足主判线，动量腿不确定；不进入P2。'),
              '', '## 边界', '',
              '- 排名层观察，不含行业上限、留存、费用、涨跌停与执行；不能直接当账户收益。',
              '- 20日主检验以期为单位；40/60日重叠窗口只做NW修正，不宣称独立样本。',
              '- 分桶样本随池规模变化；早期池规模小，7-12/13-24桶可能为空，已在期数中剔除。',
              '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print(json.dumps({h: {k: summary[h][k] for k in ('periods', 'diffs')}
                      for h in map(str, HORIZONS)}, ensure_ascii=False, default=str))
    print('verdict:', verdict)
    print('report:', REPORT)


if __name__ == '__main__':
    main()
