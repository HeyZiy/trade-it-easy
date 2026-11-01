"""增长口径对照：单季输入、四季同比及固定资格，非交易回测。"""
import numpy as np
import pandas as pd
from research.tools.factor_analysis import clean

GROWTH_ARMS = ('baseline', 'ttm_total', 'ttm_parent', 'ttm_adjusted')
GROWTH_METRICS = ('net_profit', 'np_parent_company_owners', 'adjusted_profit')
PERIOD_COLUMNS = ['signal_day', 'asof', 'scope', 'horizon', 'group', 'window_status',
                  'n', 'observed', 'coverage', 'return_mean', 'return_available',
                  'median_available', 'win_available', 'loss15_available', 'gain15_available', 'snapshot_error']


def growth_features(raw, codes, asof):
    asof = pd.Timestamp(asof).normalize()
    raw = raw.copy()
    raw['report'] = pd.to_datetime(raw.statDate, errors='coerce').dt.normalize()
    raw['published'] = pd.to_datetime(raw.pubDate, errors='coerce').dt.normalize()
    raw['value'] = pd.to_numeric(raw.value, errors='coerce')
    rows = []
    for code in codes:
        row = dict(code=code)
        for metric, arm in zip(GROWTH_METRICS, GROWTH_ARMS[1:]):
            own = raw[raw.code.eq(code) & raw.metric.eq(metric)].sort_values('report')
            row.update({arm: np.nan, arm + '_current': np.nan, arm + '_prior': np.nan,
                        arm + '_report': '', arm + '_status': 'missing_quarters'})
            if (own.report.isna().any() or own.published.isna().any()
                    or own.report.gt(asof).any() or own.published.gt(asof).any()
                    or own.published.lt(own.report).any()):
                row[arm + '_status'] = 'invalid_visibility'
                continue
            if own.report.duplicated().any():
                row[arm + '_status'] = 'duplicate_report'
                continue
            own = own.tail(8)
            if len(own) != 8:
                continue
            periods = own.report.dt.to_period('Q')
            if (not own.report.dt.is_quarter_end.all()
                    or list(periods) != list(pd.period_range(periods.iloc[-1] - 7, periods=8, freq='Q'))):
                continue
            row[arm + '_report'] = str(own.report.iloc[-1].date())
            if (asof - own.report.iloc[-1]).days > 240:
                row[arm + '_status'] = 'stale_report'
                continue
            if not np.isfinite(own.value.values).all():
                row[arm + '_status'] = 'invalid_value'
                continue
            prior, current = float(own.value.iloc[:4].sum()), float(own.value.iloc[4:].sum())
            row.update({arm + '_current': current, arm + '_prior': prior})
            if not np.isfinite([prior, current]).all():
                row[arm + '_status'] = 'invalid_value'
            elif prior <= 0:
                row[arm + '_status'] = 'nonpositive_prior'
            else:
                value = (current / prior - 1) * 100
                row[arm + '_status'] = 'ok' if np.isfinite(value) else 'invalid_value'
                row[arm] = value if np.isfinite(value) else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index('code', drop=False)


def freeze_growth(frame):
    frame = frame.copy()
    frame['baseline'] = frame.np_yoy_pct
    frame['common'] = frame[list(GROWTH_ARMS)].notna().all(axis=1)
    baseline_report = pd.to_datetime(frame.stat_date, errors='coerce')
    for arm in GROWTH_ARMS[1:]:
        frame['pass_' + arm] = frame[arm].gt(10)
        frame['common'] &= pd.to_datetime(frame[arm + '_report'], errors='coerce').eq(baseline_report)
    frame['pass_baseline'] = frame.baseline.gt(10)
    return frame


def growth_period_statistics(snapshot, frame):
    rows = []
    for scope in ('available_inputs', 'common_inputs'):
        for h in (20, 40, 60):
            groups = {}
            for arm in GROWTH_ARMS:
                if 'pass_' + arm not in frame:
                    own = pd.DataFrame()
                else:
                    mask = frame['pass_' + arm].eq(True)
                    if scope == 'common_inputs':
                        mask &= frame.common.eq(True)
                    own = frame[mask]
                r = clean(own['r%d' % h]) if len(own) else pd.Series(dtype=float)
                known = r.dropna()
                row = dict(signal_day=str(snapshot['signal_day'].date()), asof=str(snapshot['asof'].date()),
                           scope=scope, horizon=h, group=arm, window_status=snapshot['status_%d' % h],
                           n=len(own), observed=len(known), coverage=len(known)/len(own) if len(own) else np.nan,
                           return_mean=float(r.mean()) if len(r) and r.notna().all() else np.nan,
                           return_available=float(known.mean()) if len(known) else np.nan,
                           median_available=float(known.median()) if len(known) else np.nan,
                           win_available=float(known.gt(0).mean()) if len(known) else np.nan,
                           loss15_available=float(known.le(-.15).mean()) if len(known) else np.nan,
                           gain15_available=float(known.ge(.15).mean()) if len(known) else np.nan,
                           snapshot_error=snapshot['error'])
                groups[arm] = row
                rows.append(row)
            for left, right in (('ttm_total', 'baseline'), ('ttm_parent', 'ttm_total'),
                                ('ttm_adjusted', 'ttm_parent'), ('ttm_adjusted', 'baseline')):
                a, b = groups[left], groups[right]
                diff = dict(a)
                diff.update(group=left + '_minus_' + right, n=a['n'] + b['n'],
                            observed=a['observed'] + b['observed'],
                            coverage=min(a['coverage'], b['coverage'])
                            if np.isfinite([a['coverage'], b['coverage']]).all() else np.nan)
                for key in ('return_mean', 'return_available', 'median_available', 'win_available',
                            'loss15_available', 'gain15_available'):
                    diff[key] = a[key] - b[key]
                rows.append(diff)
    return rows
