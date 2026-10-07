"""低预期与盈利维持的财务校验、固定分组和收益统计。"""
import numpy as np
import pandas as pd
from research.studies.expectations.internal.core import centered_rank, report_date
from research.tools.single_factor.single_factor_test import clean, summarize_ic

VERSION = 'low_expectations_v2_2026-10-06'

PROFIT_FIELD = 'np_parent_company_owners'

PE_LIMIT = 10.0

GROUP_NAMES = ('A', 'B', 'C', 'D', 'LOW')

HISTORY_COLUMNS = ('code', 'statDate', 'pubDate', 'np_parent_company_owners')

METRICS = ('A_minus_LOW_observed', 'A_minus_B_observed', 'interaction_observed',
           'A_observed_minus_benchmark', 'LOW_observed_minus_benchmark',
           'A_minus_LOW_strict', 'A_minus_B_strict', 'regression_stable_within_low',
           'regression_stable', 'regression_interaction')

def prepare_profit_features(rows, snapshot, max_report_age=240):
    """只接受最新连续八个可见单季度；返回每只原股票的资格及排除原因。"""
    if snapshot.code.duplicated().any() or snapshot['asof'].nunique() != 1:
        raise ValueError('Snapshot must contain unique codes at one asof')
    if set(HISTORY_COLUMNS) - set(rows.columns):
        raise ValueError('Missing profit history columns')
    requested = set(snapshot.code)
    if set(rows.code) - requested:
        raise ValueError('Profit history contains unrequested codes')
    cutoff = pd.Timestamp(snapshot['asof'].iloc[0]).normalize()
    data = rows[list(HISTORY_COLUMNS)].copy()
    data['statDate'] = data.statDate.apply(report_date)
    data['pubDate'] = pd.to_datetime(data.pubDate, errors='coerce').dt.normalize()
    data[PROFIT_FIELD] = clean(data[PROFIT_FIELD])
    blocks = {code: block.sort_values('statDate') for code, block in data.groupby('code')}
    records = []
    for original in snapshot.itertuples(index=False):
        block = blocks.get(original.code, pd.DataFrame(columns=HISTORY_COLUMNS))
        row = dict(code=original.code, signal=original.signal, asof=original.asof,
                   profit_status='ok', quarter_n=len(block), profit_report=pd.NaT,
                   profit_last_published=pd.NaT, current_ttm=np.nan, prior_ttm=np.nan,
                   profit_yoy=np.nan, latest_quarter_change=np.nan, implied_ttm=np.nan,
                   ttm_relative_mismatch=np.nan)
        reason = None
        if block.empty:
            reason = 'no_history'
        elif block.statDate.isna().any() or block.pubDate.isna().any():
            reason = 'invalid_dates'
        elif ((block.statDate > cutoff) | (block.pubDate > cutoff)
              | (block.pubDate < block.statDate)).any():
            reason = 'not_visible'
        elif block.statDate.duplicated().any():
            reason = 'ambiguous_quarter'
        else:
            row['profit_report'] = block.statDate.iloc[-1]
            row['profit_last_published'] = block.pubDate.max()
            if len(block) != 8:
                reason = 'insufficient_quarters' if len(block) < 8 else 'unexpected_quarter_count'
            elif (np.diff([date.to_period('Q').ordinal for date in block.statDate]) != 1).any():
                reason = 'nonconsecutive_quarters'
            elif (cutoff - block.statDate.iloc[-1]).days > max_report_age:
                reason = 'stale_report'
            elif block.statDate.iloc[-1] != pd.Timestamp(original.report).normalize():
                reason = 'report_differs_from_original'
            elif block[PROFIT_FIELD].isna().any():
                reason = 'missing_profit'
            else:
                profits = block[PROFIT_FIELD].values.astype(float)
                current, prior = float(profits[4:].sum()), float(profits[:4].sum())
                row.update(current_ttm=current, prior_ttm=prior,
                           latest_quarter_change=float(profits[-1] - profits[3]))
                if current <= 0:
                    reason = 'nonpositive_current_ttm'
                elif prior <= 0:
                    reason = 'nonpositive_prior_ttm'
                else:
                    row['profit_yoy'] = current / prior - 1
                    if original.pe_ratio <= 0 or original.market_cap <= 0:
                        reason = 'invalid_original_valuation'
                    else:
                        implied = original.market_cap * 1e8 / original.pe_ratio
                        row['implied_ttm'] = implied
                        row['ttm_relative_mismatch'] = current / implied - 1
        row['profit_status'] = reason or 'ok'
        records.append(row)
    return pd.DataFrame(records)

def freeze_profit_groups(snapshot, features):
    """所有分组只使用原信号和已披露利润，与未来报价无关。"""
    keys = ['signal', 'asof', 'code']
    joined = snapshot.merge(features, on=keys, how='left', validate='one_to_one')
    if joined.profit_status.isna().any():
        raise ValueError('Missing feature coverage for original members')
    common = joined[joined.profit_status == 'ok'].copy()
    common['low_expectations'] = common.pe_ratio < PE_LIMIT
    common['profit_maintained'] = common.current_ttm >= common.prior_ttm
    common['cell'] = np.select([
        common.low_expectations & common.profit_maintained,
        common.low_expectations & ~common.profit_maintained,
        ~common.low_expectations & common.profit_maintained,
    ], ['A', 'B', 'C'], default='D')
    return common

def stable_regression(frame, future_return, min_n=100):
    """离散条件＋连续EP/行业/市值/动量；信号时定秩，缺报价后不重算。"""
    frame = frame.set_index('code')
    low = frame.low_expectations.astype(float)
    stable = frame.profit_maintained.astype(float)
    cap = np.log(clean(frame.market_cap))
    cap_sd = cap.std(ddof=0)
    cap = (cap - cap.mean()) / cap_sd if cap_sd > 0 else cap * 0
    x = pd.DataFrame(dict(intercept=1.0, low=low, stable=stable, interaction=low * stable,
                          value=centered_rank(1 / frame.pe_ratio), log_cap=cap,
                          momentum=centered_rank(frame.momentum120)))
    industry = pd.get_dummies(frame.industry, prefix='industry', drop_first=True).astype(float)
    x = pd.concat([x, industry], axis=1, sort=False)
    y = clean(future_return).reindex(x.index)
    valid = x.notna().all(axis=1) & y.notna()
    x, y = x.loc[valid], y.loc[valid]
    out = dict(regression_n=len(y), regression_stable=np.nan, regression_interaction=np.nan,
               regression_stable_within_low=np.nan, regression_status='ok')
    drop = [c for c in x if c not in ('intercept', 'low', 'stable', 'interaction')
            and x[c].nunique() <= 1]
    x = x.drop(columns=drop)
    if len(y) < max(min_n, len(x.columns) + 3):
        out['regression_status'] = 'insufficient_pairs'
    elif np.linalg.matrix_rank(x.values) != len(x.columns):
        out['regression_status'] = 'not_identifiable'
    else:
        beta = pd.Series(np.linalg.lstsq(x.values, y.values, rcond=None)[0], index=x.columns)
        out.update(regression_stable=float(beta.stable),
                   regression_interaction=float(beta.interaction),
                   regression_stable_within_low=float(beta.stable + beta.interaction))
    return out

def evaluate_profit_period(frame, future_return, benchmark=np.nan, min_cell_n=20, min_n=100):
    stats = dict(common_n=len(frame), benchmark=benchmark)
    for name in GROUP_NAMES:
        members = frame[frame.low_expectations] if name == 'LOW' else frame[frame.cell == name]
        values = clean(future_return).reindex(members.code)
        valid_n = int(values.notna().sum())
        stats[name + '_n'] = len(members)
        stats[name + '_valid_n'] = valid_n
        stats[name + '_coverage'] = valid_n / len(members) if len(members) else np.nan
        stats[name + '_observed'] = values.mean() if valid_n >= min_cell_n else np.nan
        stats[name + '_strict'] = (values.mean() if len(members) >= min_cell_n
                                   and values.notna().all() else np.nan)
    for mode in ('observed', 'strict'):
        stats['A_minus_LOW_' + mode] = stats['A_' + mode] - stats['LOW_' + mode]
        stats['A_minus_B_' + mode] = stats['A_' + mode] - stats['B_' + mode]
    stats['interaction_observed'] = stats['A_minus_B_observed'] - (
        stats['C_observed'] - stats['D_observed'])
    stats['A_observed_minus_benchmark'] = stats['A_observed'] - benchmark
    stats['LOW_observed_minus_benchmark'] = stats['LOW_observed'] - benchmark
    stats.update(stable_regression(frame, future_return, min_n))
    return stats

def summarize_profit_periods(periods, rotate_every=20):
    records = []
    for horizon, block in periods.groupby('horizon', sort=True):
        block = block.sort_values('signal')
        lags = max(3, int(np.ceil(horizon / rotate_every)) - 1)
        scopes = [('all', block)] + [(str(year), piece) for year, piece in
                                    block.groupby(pd.to_datetime(block.signal).dt.year)]
        for scope, piece in scopes:
            for metric in METRICS:
                row = summarize_ic(piece[metric], lags)
                row.update(horizon=int(horizon), scope=scope, metric=metric,
                           scheduled_n=len(piece), hac_lags=lags)
                records.append(row)
    return pd.DataFrame(records)
