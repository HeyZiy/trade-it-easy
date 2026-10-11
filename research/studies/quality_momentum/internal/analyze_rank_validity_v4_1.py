# -*- coding: utf-8 -*-
"""排名有效期离线探针 v4_1：冻结130期质量池与排名，观察前6名优势的期限衰减。

预声明设计（2026-10-10，不扫窗口）：
- 观察单位：v4_1 导出的每个信号日池快照（members CSV），前6名=rank<=6，其余=池内其他。
- 期限刻度：T+1收盘起算 5/10/20/40/60 个中证800交易日（T=信号日，预声明这5个）。
- 两个信号：12-2动量（策略信号）与入场前20日涨幅（对照，方向存疑）；同分按代码升序。
- 每期两个量：前6名-其他 差值（排名有效期）、前6名-全池均值 差值（头部凸性，
  pool_ic_probe_v1 封存假设(a)的验证）。
- t 检验以"期"为单位；重叠窗口用 Newey-West（lag=期限//20）修正，不宣称独立样本。
- 大亏损发生率：60日内复权收盘跌幅<=-15%的成员占比，前6名 vs 其他。
收盘近似：不含开盘价/涨跌停可成交性；停牌缺口剔除不填零；窗口止于缓存末端。
运行：python research/studies/quality_momentum/internal/analyze_rank_validity_v4_1.py
（.conda 环境；缺缓存的池成员自动从新浪补抓，不开聚宽平台）
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[2]
CACHE = ROOT / 'research/studies/roe_quality/_cache/pool'
OUT = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10.md'
EXPORTS = STUDY / 'exports'
MEMBERS = EXPORTS / 'pool_rotation_v4_1_members.csv'
HORIZONS = (5, 10, 20, 40, 60)
TOP_N = 6
SEVERE = -0.15
START, END = '20150601', '20260930'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_missing(codes6):
    """补抓缺失日线（新浪，与 pool_curve_v1 同源同格式），幂等。"""
    todo = [c for c in codes6 if not (CACHE / f'bars_{c}.pkl').exists()]
    if not todo:
        return [], []
    CACHE.mkdir(parents=True, exist_ok=True)

    def one(code):
        sym = ('sh' if code.startswith('6') else 'sz') + code
        try:
            frames = []
            for adjust in ('', 'hfq'):
                frame = ak.stock_zh_a_daily(symbol=sym, start_date=START,
                                            end_date=END, adjust=adjust)
                if frame is None or len(frame) == 0:
                    return code, False
                frames.append(frame.rename(columns={'日期': 'date', '收盘': 'close'})[['date', 'close']])
            frames[0].to_pickle(CACHE / f'bars_{code}.pkl')
            frames[1].to_pickle(CACHE / f'bars_hfq_{code}.pkl')
            return code, True
        except Exception as exc:  # 新浪偶发失败不写缓存，留空报告
            print(f'[bars] {code} FAIL {exc}', flush=True)
            return code, False

    results = list(ThreadPoolExecutor(4).map(one, todo))
    ok = [c for c, good in results if good]
    failed = [c for c, good in results if not good]
    print(f'[bars] 补抓 {len(todo)} 只：成功 {len(ok)}，失败 {len(failed)}', flush=True)
    return ok, failed


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


def main():
    members = pd.read_csv(MEMBERS, encoding='utf-8-sig')
    require(not members.empty, 'members为空')
    members['signal_day'] = pd.to_datetime(members.signal_day)
    members['code6'] = members.code.str[:6]
    require(not members.duplicated(['signal_day', 'code']).any(), '同信号日成员重复')
    ranked = members[members['rank'].notna()].copy()
    print('ranking_status分布：', ranked.ranking_status.value_counts().to_dict())
    print('pool_reason分布：', ranked.pool_reason.value_counts().to_dict())

    codes6 = sorted(ranked.code6.unique())
    ok, failed = fetch_missing(codes6)
    failed_rows = ranked[ranked.code6.isin(set(failed))]
    failed_exposure = dict(rows=int(len(failed_rows)), share=float(len(failed_rows) / len(ranked)),
                           top6_rows=int(failed_rows['rank'].le(TOP_N).sum()),
                           median_rank=float(failed_rows['rank'].median()) if len(failed_rows) else np.nan)
    print('缺失缓存暴露：', failed_exposure, flush=True)
    close = {}
    for code in codes6:
        path = CACHE / f'bars_hfq_{code}.pkl'
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        require(list(frame.columns) == ['date', 'close'], f'{code}行情结构错误')
        dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
        series = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(), index=dates)
        require(series.index.is_unique and series.index.is_monotonic_increasing, f'{code}行情乱序')
        require(series.gt(0).all(), f'{code}行情含无效值')
        close[code] = series
    index = pd.read_pickle(CACHE / 'idx_000906.pkl')
    calendar = pd.DatetimeIndex(pd.to_datetime(index.index)).normalize().sort_values()
    calendar = calendar[(calendar >= ranked.signal_day.min()) & (calendar <= calendar.max())]
    require(calendar.is_unique, '基准日历重复')

    pos = {day: i for i, day in enumerate(calendar)}
    signal_positions = sorted({pos[d] for d in ranked.signal_day.unique() if d in pos})
    require(len(signal_positions) >= 100, f'信号日过少 {len(signal_positions)}')

    # 每成员每期限的前瞻收益：T+1收盘 -> T+1+h收盘；停牌缺口剔除不填零。
    rows = []
    for record in ranked.itertuples():
        series = close.get(record.code6)
        base = pos.get(record.signal_day)
        if series is None or base is None or base + 1 >= len(calendar):
            continue
        t1 = calendar[base + 1]
        entry = series.get(t1)
        prior20 = np.nan
        if base >= 21:
            prior = series.get(calendar[base - 1])
            past = series.get(calendar[base - 21])
            if pd.notna(prior) and pd.notna(past) and past > 0:
                prior20 = prior / past - 1
        if pd.isna(entry) or entry <= 0:
            continue
        row = dict(signal_day=record.signal_day, code=record.code, code6=record.code6,
                   rank=int(record.rank), momentum=record.momentum, prior20=prior20)
        for horizon in HORIZONS:
            end_pos = base + 1 + horizon
            if end_pos >= len(calendar):
                row[f'r{horizon}_status'] = 'censored'
                continue
            exit_close = series.get(calendar[end_pos])
            row[f'r{horizon}_status'] = 'ok' if pd.notna(exit_close) and exit_close > 0 else 'missing_close'
            row[f'r{horizon}'] = exit_close / entry - 1 if row[f'r{horizon}_status'] == 'ok' else np.nan
        rows.append(row)
    forward = pd.DataFrame(rows)
    require(not forward.empty, '无有效前瞻观测')
    OUT.mkdir(parents=True, exist_ok=True)
    forward.to_csv(OUT / 'member_forward_returns.csv', index=False, encoding='utf-8-sig')

    # 每期分组：动量前6 vs 其他；对照信号 prior20 前6 vs 其他。
    forward['top6_mom'] = forward['rank'] <= TOP_N
    forward['prior20_rank'] = forward.groupby('signal_day').prior20.rank(
        ascending=False, method='first', na_option='bottom')
    forward['top6_prior'] = forward.prior20_rank.le(TOP_N)

    diff_rows = []
    for signal, top_flag in (('momentum', 'top6_mom'), ('prior20', 'top6_prior')):
        for horizon in HORIZONS:
            key = f'r{horizon}'
            sample = forward[forward[f'r{horizon}_status'].eq('ok')]
            for day, frame in sample.groupby('signal_day'):
                top, rest = frame[frame[top_flag]], frame[~frame[top_flag]]
                pool_mean = frame[key].mean()
                if top.empty or rest.empty:
                    continue
                diff_rows.append(dict(signal=signal, horizon=horizon, signal_day=day,
                                      periods_observed=len(frame), top_mean=top[key].mean(),
                                      rest_mean=rest[key].mean(), pool_mean=pool_mean,
                                      top_minus_rest=top[key].mean() - rest[key].mean(),
                                      top_minus_pool=top[key].mean() - pool_mean,
                                      n_top=len(top), n_rest=len(rest)))
    diffs = pd.DataFrame(diff_rows)
    diffs.to_csv(OUT / 'period_diffs.csv', index=False, encoding='utf-8-sig')

    def summarize(frame, key):
        grouped = frame.groupby('horizon')[key]
        result = {}
        for h in HORIZONS:
            if h not in grouped.groups:
                result[str(h)] = dict(periods=0, mean=np.nan, median=np.nan,
                                      positive_share=np.nan, naive_t=np.nan, nw_t=np.nan)
                continue
            series = grouped.get_group(h)
            result[str(h)] = dict(periods=int(len(series)), mean=float(series.mean()),
                                  median=float(series.median()),
                                  positive_share=float(series.gt(0).mean()),
                                  naive_t=naive_t(series), nw_t=nw_t(series, h // 20))
        return result

    summary = dict(
        date='2026-10-10', design='预声明期限5/10/20/40/60与两个信号，不扫窗口',
        signal_days=int(ranked.signal_day.nunique()), members_rows=len(ranked),
        member_codes=len(codes6), fetched_now=len(ok), fetch_failed=failed,
        fetch_failed_exposure=failed_exposure,
        observation_counts={str(h): int(forward[f'r{h}_status'].eq('ok').sum()) for h in HORIZONS},
        momentum_top_minus_rest=summarize(diffs[diffs.signal.eq('momentum')], 'top_minus_rest'),
        momentum_top_minus_pool=summarize(diffs[diffs.signal.eq('momentum')], 'top_minus_pool'),
        prior20_top_minus_rest=summarize(diffs[diffs.signal.eq('prior20')], 'top_minus_rest'),
        prior20_top_minus_pool=summarize(diffs[diffs.signal.eq('prior20')], 'top_minus_pool'),
        severe60_top6_momentum=dict(
            top6=float(forward[forward.top6_mom & forward.r60_status.eq('ok')].r60.le(SEVERE).mean()),
            rest=float(forward[~forward.top6_mom & forward.r60_status.eq('ok')].r60.le(SEVERE).mean()),
            top6_n=int((forward.top6_mom & forward.r60_status.eq('ok')).sum()),
            rest_n=int((~forward.top6_mom & forward.r60_status.eq('ok')).sum())),
        severe60_top6_prior20=dict(
            top6=float(forward[forward.top6_prior & forward.r60_status.eq('ok')].r60.le(SEVERE).mean()),
            rest=float(forward[~forward.top6_prior & forward.r60_status.eq('ok')].r60.le(SEVERE).mean()),
            top6_n=int((forward.top6_prior & forward.r60_status.eq('ok')).sum()),
            rest_n=int((~forward.top6_prior & forward.r60_status.eq('ok')).sum())),
        methods=dict(
            forward='T+1收盘至T+1+h收盘（中证800日历推进），后复权收盘；停牌/缺口剔除不填零；'
                    '窗口止于缓存末端，不足标记censored',
            top6='rank<=6（排名信号本身，不含行业上限后的实际入选）；prior20为入场前20日涨幅对照',
            tests='以期为单位；重叠窗口Newey-West lag=期限//20修正；不宣称窗口间独立',
            boundary='收盘近似，无开盘价/涨跌停/资金约束；期限是观察刻度，不能直接当作调仓周期',
            sha256={'members': sha(MEMBERS)},
            cache_manifest_sha256={f'{c}_raw': sha(CACHE / f'bars_{c}.pkl') for c in codes6
                                   if (CACHE / f'bars_{c}.pkl').exists()},
        ),
    )
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')
    yearly = diffs[(diffs.signal.eq('momentum')) & (diffs.horizon.eq(20))].copy()
    yearly['year'] = yearly.signal_day.dt.year
    yearly_table = yearly.groupby('year').agg(
        periods=('top_minus_rest', 'size'), mean_top_minus_rest=('top_minus_rest', 'mean'),
        median_top_minus_rest=('top_minus_rest', 'median'),
        positive_share=('top_minus_rest', lambda s: s.gt(0).mean())).reset_index()
    yearly_table.to_csv(OUT / 'yearly_h20_momentum.csv', index=False, encoding='utf-8-sig')
    write_report(summary, yearly_table, diffs)
    print(json.dumps({k: v for k, v in summary.items() if k != 'methods'}, ensure_ascii=True, default=str))


def write_report(summary, yearly, diffs):
    def table(measures, label):
        rows = ['| 期限（交易日） | 期数 | 差值均值 | 差值中位数 | 正差占比 | naive t | NW t |',
                '| --- | --- | --- | --- | --- | --- | --- |']
        for h in HORIZONS:
            m = measures[str(h)]
            rows.append(f'| {h} | {m["periods"]} | {m["mean"]:.2%} | {m["median"]:.2%} | '
                        f'{m["positive_share"]:.1%} | {m["naive_t"]:.2f} | {m["nw_t"]:.2f} |')
        return '\n'.join(rows)

    mom = summary['momentum_top_minus_rest']
    mom_pool = summary['momentum_top_minus_pool']
    prior = summary['prior20_top_minus_rest']
    lines = [
        '# 4.1 排名有效期离线探针（2026-10-10）', '',
        f'{summary["signal_days"]}个信号日、{summary["members_rows"]}条池成员记录、'
        f'{summary["member_codes"]}只股票；缺缓存自动补抓{summary["fetched_now"]}只'
        + (f'，失败{len(summary["fetch_failed"])}只：{"、".join(summary["fetch_failed"])}' if summary["fetch_failed"] else '')
        + '。预声明期限5/10/20/40/60与两个信号，未扫其他窗口。', '',
        '## 12-2动量：前6名 − 池内其他', '',
        table(mom, 'momentum'), '',
        '## 12-2动量：前6名 − 全池均值（头部凸性）', '',
        table(mom_pool, 'momentum_pool'), '',
        '## 对照：入场前20日涨幅 前6名 − 池内其他', '',
        table(prior, 'prior20'), '',
        '## 大亏损发生率（60日内收盘跌幅≤-15%）', '',
        f'动量前6名 {summary["severe60_top6_momentum"]["top6"]:.1%}'
        f'（{summary["severe60_top6_momentum"]["top6_n"]}观测） vs 池内其他 '
        f'{summary["severe60_top6_momentum"]["rest"]:.1%}'
        f'（{summary["severe60_top6_momentum"]["rest_n"]}观测）；'
        f'入场前20日涨幅前6名 {summary["severe60_top6_prior20"]["top6"]:.1%} vs 其他 '
        f'{summary["severe60_top6_prior20"]["rest"]:.1%}。', '',
        '## 20日差值分年（动量信号）', '',
        '| 年份 | 期数 | 差值均值 | 差值中位数 | 正差占比 |', '| --- | --- | --- | --- | --- |']
    for row in yearly.itertuples():
        lines.append(f'| {row.year} | {row.periods} | {row.mean_top_minus_rest:.2%} | '
                     f'{row.median_top_minus_rest:.2%} | {row.positive_share:.1%} |')
    lines += ['', '## 方法边界', '',
        f'后复权收盘近似，无开盘价、涨跌停与资金约束；停牌/缺口剔除不填零；'
        f'重叠窗口以Newey-West（lag=期限//20）修正，不宣称独立样本。'
        f'期限只是观察刻度，不能直接当作调仓周期；改变周期需平台策略级对照回测。', '',
        f'缺失缓存{len(summary["fetch_failed"])}只：新浪对这批股票的成交额历史返回空'
        f'（多为已退市或重组股票），为保持复权口径一致未换数据源。'
        f'它们仅占{summary["fetch_failed_exposure"]["share"]:.1%}观测'
        f'（{summary["fetch_failed_exposure"]["rows"]}条，排名中位数'
        f'{summary["fetch_failed_exposure"]["median_rank"]:.0f}，进过前6名仅'
        f'{summary["fetch_failed_exposure"]["top6_rows"]}期），对前6名对比影响可忽略。', '',
        '## 复现', '',
        '`python research/studies/quality_momentum/internal/analyze_rank_validity_v4_1.py`（项目 .conda 环境）', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print('报告：', REPORT)


if __name__ == '__main__':
    main()
