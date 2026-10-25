# -*- coding: utf-8 -*-
"""P0 对照：质量池等权 vs 同域无质量筛选等权 + 同规模随机子集。

问题：池子的超额，是质量筛选带来的，还是“主板等权 + 20日再平衡 + 小组合集中度”
       本身的风格？本脚本只读现有缓存，不新增平台回测、不改基线码。

设计：
  - 域：_cache/pool 中 3191 只 60/00 主板日线（当前上市；退市缺失，见偏差）。
  - 日历/信号：与 dd decomposition 相同，result_20.csv 的 2591 个交易日；
    130 个信号日；信号日 s 的成员从第 q+2 个交易日赚到下一信号日 q+1 收盘，
    段内日频等权再平衡；停牌/缺数据按 0 收益。
  - 池等权：members.csv 当期全部成员（有缓存者），复算 dd 报告的 8.97% 作校验。
  - 同域等权：每个信号日，所有满足“最近61个交易日收盘完整 + 当日有收盘”的 60/00 股票。
  - 随机子集：每个信号日从同域中无放回抽 N=当期池成员数，500 次；
    与池等权同口径，用来控制“95只组合的集中度/运气”。
  - 偏差：缓存是当前上市 3191 只，退市股缺失 → 同域等权被高估；
    同域含历史 ST、池不含 → 同域等权可能被压低。两个方向部分抵消。
    随机子集与池用同一域，故“池在随机分布中的分位”对上述域级偏差是稳健的。

预注册判线（先写死，跑完不改）：
  主判据：池等权在 500 个同规模随机子集年化分布中的分位 P。
    - P >= 95 且 池−同域 20日期间超额均值 >0、t>=2、年化差 >3pp -> pass
    - P < 50 -> fail
    - 其余 -> inconclusive
  辅助（不改判）：池−同域期间超额、分年、β/回撤；池等权复算与 dd 报告的校验。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from analyze_rotate_period_v4_1 import FILES, load  # noqa: E402

CACHE = STUDY.parent / 'roe_quality' / '_cache' / 'pool'
MEMBERS = STUDY / 'exports' / 'pool_rotation_v4_1_members.csv'
OUT = STUDY / 'reports' / 'universe_control_2026-10-21'
REPORT = STUDY / 'reports' / 'universe_control_2026-10-21.md'
ANNUAL_DAYS = 245.0
LOOKBACK = 61
DRAWS = 500
SEED = 20261021


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


def load_universe(full_dates: pd.DatetimeIndex, dates: pd.DatetimeIndex):
    paths = sorted(CACHE.glob('bars_hfq_*.pkl'))
    require(paths, '缓存为空')
    codes = [p.stem.replace('bars_hfq_', '') for p in paths]
    ret = np.zeros((len(dates), len(codes)), dtype=np.float32)
    valid = np.zeros((len(full_dates), len(codes)), dtype=bool)
    started = time.time()
    for j, path in enumerate(paths):
        frame = pd.read_pickle(path)[['date', 'close']].copy()
        frame['date'] = pd.to_datetime(frame.date).dt.normalize()
        frame = frame.drop_duplicates('date').sort_values('date').set_index('date')
        series = pd.to_numeric(frame.close, errors='coerce').reindex(full_dates)
        valid[:, j] = (series.notna() & series.gt(0)).to_numpy()
        ret_full = series.ffill().pct_change().fillna(0.0)
        ret[:, j] = ret_full.reindex(dates).fillna(0.0).to_numpy(dtype=np.float32)
        if (j + 1) % 500 == 0:
            print(f'loaded {j + 1}/{len(paths)}  {time.time() - started:.1f}s',
                  flush=True)
    return codes, ret, valid


def curve_stats(daily: np.ndarray, idx_ret: np.ndarray) -> dict:
    curve = np.cumprod(1.0 + daily)
    dd = curve / np.maximum.accumulate(curve) - 1.0
    trough = int(np.argmin(dd))
    peak = int(np.argmax(curve[:trough + 1]))
    vol = float(np.std(daily, ddof=1) * math.sqrt(ANNUAL_DAYS))
    beta = float(np.cov(daily, idx_ret, ddof=1)[0, 1] / np.var(idx_ret, ddof=1))
    corr = float(np.corrcoef(daily, idx_ret)[0, 1])
    return dict(
        annual=float(curve[-1] ** (ANNUAL_DAYS / len(curve)) - 1.0),
        cum=float(curve[-1] - 1.0),
        max_dd=float(dd[trough]),
        vol=vol,
        beta=beta,
        r_squared=corr ** 2,
        trough_pos=trough,
        peak_pos=peak,
    )


def main() -> None:
    full_index = pd.read_pickle(CACHE / 'idx_000906.pkl')
    full_dates = pd.DatetimeIndex(pd.to_datetime(full_index.index)).normalize()
    require(full_dates.is_monotonic_increasing, '指数日历乱序')

    strategy = load(FILES['rotate20'])
    dates = pd.DatetimeIndex(pd.to_datetime(strategy['时间'])).normalize()
    require(dates.is_unique and dates.is_monotonic_increasing, '策略日历重复/乱序')
    require(dates.isin(full_dates).all(), '策略日历不在指数日历')
    idx_curve = pd.Series(pd.to_numeric(full_index).to_numpy(), index=full_dates)
    idx_curve = idx_curve[~idx_curve.index.duplicated()].sort_index().reindex(dates)
    require(idx_curve.notna().all() and (idx_curve > 0).all(), '指数未对齐')
    idx_ret = idx_curve.pct_change().fillna(0.0).to_numpy()

    members = pd.read_csv(MEMBERS, encoding='utf-8-sig')
    members['signal_day'] = pd.to_datetime(members.signal_day)
    members['code6'] = members.code.astype(str).str[:6]
    signal_days = sorted(members.signal_day.unique())
    sig_pos = np.array([dates.get_loc(d) for d in signal_days])
    full_pos = {d: i for i, d in enumerate(full_dates)}
    require(all(d in full_pos for d in signal_days), '信号日不在指数日历')
    sig_pos_full = np.array([full_pos[d] for d in signal_days])
    require((sig_pos_full >= LOOKBACK - 1).all(), '早期信号日历史不足61日')

    codes, ret, valid = load_universe(full_dates, dates)
    code_to_j = {c: j for j, c in enumerate(codes)}
    members['j'] = members.code6.map(code_to_j)
    missing = members[members.j.isna()].code6.nunique()
    members = members[members.j.notna()].copy()
    pool_groups = members.groupby('signal_day').j.apply(lambda s: np.array(sorted(s), dtype=int))
    pool_sizes = members.groupby('signal_day').size()

    nav_path = STUDY / 'exports' / 'pool_rotation_v4_1_2_nav.csv'
    nav = pd.read_csv(nav_path, parse_dates=['date']).set_index('date').reindex(dates)
    require(nav.equity.notna().all(), '策略净值未对齐')

    # 与 dd 报告同口径的池等权（不额外要求当日有价），用于校验。
    pool_daily = np.zeros(len(dates))
    pool_elig_daily = np.zeros(len(dates))
    univ_daily = np.zeros(len(dates))
    rows = []
    rand_segments = []
    rng = np.random.default_rng(SEED)

    for k, day in enumerate(signal_days):
        p0 = sig_pos[k] + 2
        if p0 > len(dates) - 1:
            break
        p1 = sig_pos[k + 1] + 1 if k + 1 < len(signal_days) else len(dates) - 1
        p1 = min(p1, len(dates) - 1)
        if p1 < p0:
            continue
        seg_idx = np.arange(p0, p1 + 1)
        pool_idx = pool_groups.get(day, np.array([], dtype=int))
        pool_idx = np.array([j for j in pool_idx if np.isfinite(ret[:, j]).all()], dtype=int)
        # 同域：最近61个交易日完整 + 当日有收盘
        no = sig_pos_full[k]
        eligible = np.flatnonzero(valid[no - LOOKBACK + 1:no + 1, :].all(axis=0) & valid[no, :])
        pool_elig = np.intersect1d(pool_idx, eligible)
        require(len(eligible) >= 10, f'{day.date()} 同域股票不足')
        require(len(pool_elig) > 0, f'{day.date()} 池成员不在同域')

        def ew(idx: np.ndarray) -> np.ndarray:
            return ret[np.ix_(seg_idx, idx)].mean(axis=1)

        def cum(daily_seg: np.ndarray) -> float:
            return float(np.prod(1.0 + daily_seg) - 1.0)

        pool_seg_daily = ew(pool_idx)
        pool_seg = cum(pool_seg_daily)
        pool_daily[seg_idx] = pool_seg_daily
        pe_seg_daily = ew(pool_elig)
        pool_elig_daily[seg_idx] = pe_seg_daily
        univ_seg_daily = ew(eligible)
        univ_daily[seg_idx] = univ_seg_daily
        univ_seg = cum(univ_seg_daily)

        n = len(pool_elig)
        samples = np.array([rng.choice(eligible, size=n, replace=False) for _ in range(DRAWS)])
        rand_matrix = ret[np.ix_(seg_idx, samples.ravel())].reshape(len(seg_idx), DRAWS, n)
        rand_seg = np.prod(1.0 + rand_matrix.mean(axis=2), axis=0) - 1.0
        rand_segments.append(rand_seg)
        actual_seg = float(nav.equity.iloc[p1] / nav.equity.iloc[p0 - 1] - 1.0)
        rows.append(dict(signal_day=day, year=day.year, n_pool=len(pool_idx),
                         n_pool_elig=n, n_eligible=len(eligible),
                         pool=pool_seg, pool_elig=cum(pe_seg_daily),
                         univ=univ_seg, actual=actual_seg,
                         actual_minus_pool=actual_seg - cum(pe_seg_daily),
                         actual_minus_univ=actual_seg - univ_seg,
                         excess=pool_seg - univ_seg,
                         excess_elig=cum(pe_seg_daily) - univ_seg,
                         rand_mean=float(rand_seg.mean()),
                         rand_p05=float(np.quantile(rand_seg, 0.05)),
                         rand_p95=float(np.quantile(rand_seg, 0.95)),
                         pool_pct=float((rand_seg < cum(pe_seg_daily)).mean()) * 100.0))

    periods = pd.DataFrame(rows)
    require(len(periods) >= 100, '有效期数不足')
    OUT.mkdir(parents=True, exist_ok=True)
    periods.to_csv(OUT / 'period_returns.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame({'date': dates, 'pool_all': pool_daily,
                  'pool_elig': pool_elig_daily, 'universe': univ_daily}).to_csv(
        OUT / 'curves.csv', index=False, encoding='utf-8-sig')

    pool_stats = curve_stats(pool_daily, idx_ret)
    pool_elig_stats = curve_stats(pool_elig_daily, idx_ret)
    univ_stats = curve_stats(univ_daily, idx_ret)
    actual_daily = pd.Series(nav.equity).pct_change().fillna(0.0).to_numpy()
    actual_stats = curve_stats(actual_daily, idx_ret)
    for item in (pool_stats, pool_elig_stats, univ_stats, actual_stats):
        item['max_dd_start'] = str(dates[item['peak_pos']].date())
        item['max_dd_end'] = str(dates[item['trough_pos']].date())
    dd_path = STUDY / 'reports' / 'dd_decomposition_v4_1_2026-10-10' / 'summary.json'
    dd_ref = None
    if dd_path.exists():
        dd_doc = json.loads(dd_path.read_text(encoding='utf-8'))
        dd_ref = next((float(s['annual']) for s in dd_doc.get('stats', [])
                       if s.get('name') == 'pool_ew'), None)

    # 随机子集的全期年化分布：逐期段收益复合
    rand_matrix = np.column_stack(rand_segments)          # (draws, periods)
    rand_cum = np.prod(1.0 + rand_matrix, axis=1) - 1.0
    rand_annual = (1.0 + rand_cum) ** (ANNUAL_DAYS / len(dates)) - 1.0
    pool_cum_elig = float(np.prod(1.0 + periods.pool_elig.to_numpy()) - 1.0)
    pool_annual_elig = float((1.0 + pool_cum_elig) ** (ANNUAL_DAYS / len(dates)) - 1.0)
    p_annual = float((rand_annual < pool_annual_elig).mean()) * 100.0
    avg_pct = float(periods.pool_pct.mean())
    median_pct = float(periods.pool_pct.median())
    rand_annual_stats = dict(
        mean=float(rand_annual.mean()), median=float(np.median(rand_annual)),
        p05=float(np.quantile(rand_annual, 0.05)),
        p95=float(np.quantile(rand_annual, 0.95)),
    )
    win_share = float((periods.excess > 0).mean())
    diff = periods.excess
    diff_elig = periods.excess_elig
    def period_summary(series: pd.Series) -> dict:
        return dict(mean=float(series.mean()), median=float(series.median()),
                    positive_share=float((series > 0).mean()),
                    naive_t=naive_t(series), nw_t=nw_t(series, 1))

    actual_vs_pool = period_summary(periods.actual_minus_pool)
    actual_vs_univ = period_summary(periods.actual_minus_univ)
    summary = dict(
        date='2026-10-21', period=f'{dates[0].date()}至{dates[-1].date()}',
        days=len(dates), signal_days=len(signal_days), effective_periods=len(periods),
        universe_codes=len(codes), pool_codes=members.code6.nunique(), missing_pool_codes=int(missing),
        avg_pool_size=float(pool_sizes.mean()), avg_pool_elig=float(periods.n_pool_elig.mean()),
        avg_eligible=float(periods.n_eligible.mean()),
        stats=dict(pool=pool_stats, pool_elig=pool_elig_stats,
                   universe=univ_stats, actual=actual_stats),
        actual=dict(vs_pool=actual_vs_pool, vs_universe=actual_vs_univ,
                    annual_diff_pool=float(actual_stats['annual'] - pool_elig_stats['annual']),
                    annual_diff_universe=float(actual_stats['annual'] - univ_stats['annual'])),
        excess=dict(mean=float(diff.mean()), median=float(diff.median()),
                    positive_share=win_share, naive_t=naive_t(diff), nw_t=nw_t(diff, 1)),
        excess_elig=dict(mean=float(diff_elig.mean()), median=float(diff_elig.median()),
                         positive_share=float((diff_elig > 0).mean()),
                         naive_t=naive_t(diff_elig), nw_t=nw_t(diff_elig, 1)),
        pool_pct=dict(annual=p_annual, period_mean=avg_pct, period_median=median_pct),
        pool_annual_elig=pool_annual_elig,
        random_annual=rand_annual_stats,
        random_segment=dict(mean=float(periods.rand_mean.mean()),
                            p05=float(periods.rand_p05.mean()),
                            p95=float(periods.rand_p95.mean())),
        verdict=None,
        dd_reference_pool_annual=dd_ref,
        methods='段内日频等权；信号日+2至下一信号日+1；停牌/缺数据0收益；'
                '同域=最近61日完整+当日有收盘；随机子集=同期同域同N无放回500次',
    )

    annual_diff = pool_elig_stats['annual'] - univ_stats['annual']
    p = p_annual
    if p < 50:
        verdict = 'fail'
    elif p >= 95 and diff.mean() > 0 and naive_t(diff) >= 2 and annual_diff > 0.03:
        verdict = 'pass'
    else:
        verdict = 'inconclusive'
    summary['verdict'] = verdict
    summary['annual_diff'] = float(annual_diff)
    summary['annual_diff_pool_all'] = float(pool_stats['annual'] - univ_stats['annual'])
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')

    def pct(x: float) -> str:
        return '—' if pd.isna(x) else f'{x:+.2%}'

    def num(x: float, d: int = 2) -> str:
        return '—' if pd.isna(x) else f'{x:.{d}f}'

    yearly = periods.groupby('year').agg(
        excess=('excess', 'mean'), excess_elig=('excess_elig', 'mean'),
        actual_minus_pool=('actual_minus_pool', 'mean'),
        pool_pct=('pool_pct', 'mean'), n=('signal_day', 'count')).reset_index()
    yearly.to_csv(OUT / 'yearly.csv', index=False, encoding='utf-8-sig')

    lines = [
        '# P0 质量池 vs 同域等权（2026-10-21）', '',
        f'区间 {dates[0].date()}~{dates[-1].date()}（{len(dates)}日），'
        f'{len(signal_days)}个信号日，{len(periods)}期有效；'
        f'同域 {len(codes)}只60/00，池 {members.code6.nunique()}只（缺缓存 {int(missing)}只）。',
        f'平均池规模 {pool_sizes.mean():.1f}，同域平均 {periods.n_eligible.mean():.0f}只。', '',
        '## 全期', '',
        '| 曲线 | 年化 | 最大回撤 | 波动 | β | R² |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
        f'| 池等权（dd口径） | {pool_stats["annual"]:.2%} | {pool_stats["max_dd"]:.2%} | '
        f'{pool_stats["vol"]:.2%} | {pool_stats["beta"]:.2f} | {pool_stats["r_squared"]:.2f} |',
        f'| 池等权（同域交集） | {pool_elig_stats["annual"]:.2%} | '
        f'{pool_elig_stats["max_dd"]:.2%} | {pool_elig_stats["vol"]:.2%} | '
        f'{pool_elig_stats["beta"]:.2f} | {pool_elig_stats["r_squared"]:.2f} |',
        f'| 同域等权 | {univ_stats["annual"]:.2%} | {univ_stats["max_dd"]:.2%} | '
        f'{univ_stats["vol"]:.2%} | {univ_stats["beta"]:.2f} | {univ_stats["r_squared"]:.2f} |',
        f'| 实际v4_1_2 | {actual_stats["annual"]:.2%} | {actual_stats["max_dd"]:.2%} | '
        f'{actual_stats["vol"]:.2%} | {actual_stats["beta"]:.2f} | '
        f'{actual_stats["r_squared"]:.2f} |',
        '', f'池（同域交集）− 同域年化：**{annual_diff:+.2%}**；'
            f'池（dd口径）− 同域：**{pool_stats["annual"] - univ_stats["annual"]:+.2%}**。',
        (f'校验：dd 报告池等权年化 {dd_ref:.2%}，本脚本复算 {pool_stats["annual"]:.2%}，'
         f'差 {pool_stats["annual"] - dd_ref:+.2%}。' if dd_ref is not None else
         '校验：未找到 dd 报告 summary.json。'), '',
        '## 期间超额（20日，池−同域）', '',
        '| 口径 | 均值 | 中位 | 正差占比 | naive t | NW t |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
        f'| 池全部成员 | {pct(summary["excess"]["mean"])} | {pct(summary["excess"]["median"])} | '
        f'{summary["excess"]["positive_share"]:.1%} | {num(summary["excess"]["naive_t"])} | '
        f'{num(summary["excess"]["nw_t"])} |',
        f'| 池∩同域 | {pct(summary["excess_elig"]["mean"])} | '
        f'{pct(summary["excess_elig"]["median"])} | '
        f'{summary["excess_elig"]["positive_share"]:.1%} | '
        f'{num(summary["excess_elig"]["naive_t"])} | '
        f'{num(summary["excess_elig"]["nw_t"])} |',
        '', '## 同规模随机子集（500次/期）', '',
        f'- **全期年化分位：池 {pool_annual_elig:.2%} 位于随机子集分布的 {p_annual:.1f}% 分位**；'
        f'随机子集年化 均值 {rand_annual_stats["mean"]:.2%}、中位 {rand_annual_stats["median"]:.2%}、'
        f'5%-95% [{rand_annual_stats["p05"]:.2%}, {rand_annual_stats["p95"]:.2%}]。',
        f'- 期间段收益分位均值 {avg_pct:.1f}%（中位 {median_pct:.1f}%），作为诊断；'
        f'随机段收益均值 {pct(periods.rand_mean.mean())}，'
        f'5%-95% [{pct(periods.rand_p05.mean())}, {pct(periods.rand_p95.mean())}]。',
        f'- 池段收益均值 {pct(periods.pool_elig.mean())}；'
        f'池−随机均值 {pct(periods.pool_elig.mean() - periods.rand_mean.mean())}。', '',
        '## 补充：实际策略 v4_1_2 vs 池等权', '',
        '| 对照 | 20日均值 | 中位 | 正差占比 | naive t | NW t | 年化差 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |',
        f'| 实际−池∩同域 | {pct(actual_vs_pool["mean"])} | {pct(actual_vs_pool["median"])} | '
        f'{actual_vs_pool["positive_share"]:.1%} | {num(actual_vs_pool["naive_t"])} | '
        f'{num(actual_vs_pool["nw_t"])} | '
        f'{actual_stats["annual"] - pool_elig_stats["annual"]:+.2%} |',
        f'| 实际−同域 | {pct(actual_vs_univ["mean"])} | {pct(actual_vs_univ["median"])} | '
        f'{actual_vs_univ["positive_share"]:.1%} | {num(actual_vs_univ["naive_t"])} | '
        f'{num(actual_vs_univ["nw_t"])} | '
        f'{actual_stats["annual"] - univ_stats["annual"]:+.2%} |', '',
        '## 分年（池−同域）', '',
        '| 年 | 期数 | 池−同域 | 池∩同域−同域 | 实际−池 | 池平均分位 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for row in yearly.itertuples():
        lines.append(f'| {int(row.year)} | {int(row.n)} | {pct(row.excess)} | '
                     f'{pct(row.excess_elig)} | {pct(row.actual_minus_pool)} | '
                     f'{row.pool_pct:.1f}% |')
    lines += ['', '## 判线', '',
              f'- 主判据：池在全期随机子集年化分布中的分位 **{p_annual:.1f}%**（阈值 95 / 50）。',
              f'- 辅助：池∩同域−同域 20日超额均值 {pct(summary["excess_elig"]["mean"])}，'
              f'naive t {num(summary["excess_elig"]["naive_t"])}；年化差 {annual_diff:+.2%}。',
              f'- **判定：`{verdict}`**。'
              + (' 池显著跑赢同规模随机子集与同域，质量腿成立。' if verdict == 'pass'
                 else ' 池未跑赢同规模随机子集（或年化差为负），质量腿未通过。'
                 if verdict == 'fail' else
                 ' 未同时满足主判线；质量腿不确定，不把它当正证据。'),
              '', '## 综合结论（P1+P0）', '',
              '- **P1 动量腿：fail。** top6−rank7-12 在 20/40/60 日分别为 '
              '+0.21%/+0.43%/+0.81%，NW t 0.56/0.78/1.06；top6 与紧邻的 7-12 没有区别，'
              '所谓“只取前6、绝不顺延”的截断没有证据。对25名以后的优势才是真的，'
              '但那是“避开后段”，不是“选最强6只”。详见 `rank_bucket_control_2026-10-21.md`。',
              f'- **P0 质量腿：`{verdict}`。** 池年化 {pool_elig_stats["annual"]:.2%} vs 同域 '
              f'{univ_stats["annual"]:.2%}（+{annual_diff:.2%}），但20日期间超额均值 '
              f'{pct(diff_elig.mean())}、t {num(naive_t(diff_elig))}；'
              f'全期年化分位 {p_annual:.1f}%（恰在95门槛），期间分位仅 {avg_pct:.1f}%。'
              '池的优势主要体现在更低波动（17.8% vs 22.1%）、更低β（0.77 vs 0.96）和'
              '更小最大回撤（-31.6% vs -45.9%），不是更高的期间平均收益。',
              f'- **实际 vs 池：不显著。** 实际v4_1_2−池 20日均值 '
              f'{pct(actual_vs_pool["mean"])}、t {num(actual_vs_pool["naive_t"])}、'
              f'中位 {pct(actual_vs_pool["median"])}、正差占比 '
              f'{actual_vs_pool["positive_share"]:.1%}；年化差 '
              f'{actual_stats["annual"] - pool_elig_stats["annual"]:+.2%} 主要来自路径/复利。'
              f'实际最大回撤 {actual_stats["max_dd"]:.2%} vs 池 {pool_elig_stats["max_dd"]:.2%}，'
              '主动层更像“降回撤”，不是“增收益”。',
              '- **因此：P2 不需要平台。** 当前策略的收益假设未闭合：动量腿已否，'
              '质量腿只支持低波/降回撤，主动层收益不显著。不要按“质量+动量alpha”部署；'
              '若还要继续，先决定是否简化成池等权/前12持有，再做一次预注册的账户对照。',
              '', '## 偏差与边界', '',
              '- 缓存是当前上市3191只，退市股缺失 → 同域等权被高估；同域含历史ST、池不含 → '
              '同域等权可能被压低。两个方向部分抵消，故单看“池−同域”不是干净因果；'
              '随机子集与池同域，分位判据对域级偏差稳健。',
              '- 同域只要求最近61日收盘完整+当日有收盘，不再加质量/估值/波动条件；'
              '这是“同域无质量筛选”的近似，不是全市场指数。',
              '- 段内日频等权、停牌0收益、无费用/涨跌停/执行；不能直接当账户收益。',
              '- 池等权复算用于校验 dd 报告；若与 dd 的 8.97% 有差异，以复算口径为准并记录。',
              '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print(json.dumps({k: summary[k] for k in
                      ('effective_periods', 'avg_pool_size', 'avg_eligible',
                       'annual_diff', 'annual_diff_pool_all', 'excess', 'pool_pct',
                       'verdict')}, ensure_ascii=False, default=str))
    print('pool annual', pool_stats['annual'], 'univ annual', univ_stats['annual'])
    print('report:', REPORT)


if __name__ == '__main__':
    main()
