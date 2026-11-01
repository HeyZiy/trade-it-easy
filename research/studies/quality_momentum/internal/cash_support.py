"""现金支撑的季度准备与统计；输入为已披露的单季数据。"""
import numpy as np
import pandas as pd
from research.tools.factor_analysis import clean

PERIOD_COLUMNS = ['signal_day', 'asof', 'scope', 'horizon', 'group', 'window_status',
                  'n', 'observed', 'coverage', 'return_mean', 'return_available',
                  'median_available', 'win_available', 'loss15_available', 'gain15_available',
                  'cash_support_mean', 'snapshot_error']


def cash_support_features(cash, profit, codes, asof):
    """两表各自最新四季必须连续、同报告期；不退回旧的完整窗口。"""
    asof = pd.Timestamp(asof).normalize()
    output = []
    for code in codes:
        row = dict(code=code, cash_support=np.nan, cash_ttm=np.nan,
                   profit_ttm=np.nan, cash_status='missing_quarters', cash_report='')
        series = []
        for frame, field in ((cash, 'net_operate_cash_flow'), (profit, 'operating_profit')):
            own = frame[frame.code.eq(code)].copy()
            own['report'] = pd.to_datetime(own.statDate, errors='coerce').dt.normalize()
            own['published'] = pd.to_datetime(own.pubDate, errors='coerce').dt.normalize()
            values = pd.to_numeric(own[field], errors='coerce')
            if (own.report.isna().any() or own.published.isna().any()
                    or own.report.gt(asof).any() or own.published.gt(asof).any()
                    or own.published.lt(own.report).any()):
                row['cash_status'] = 'invalid_visibility'
                break
            if own.report.duplicated().any():
                row['cash_status'] = 'duplicate_report'
                break
            own[field] = values
            own = own.sort_values('report').tail(4)
            quarters = own.report.dt.to_period('Q')
            if (len(own) != 4 or not own.report.dt.is_quarter_end.all()
                    or list(quarters) != list(pd.period_range(quarters.iloc[-1] - 3, periods=4, freq='Q'))):
                break
            if (asof - own.report.iloc[-1]).days > 240:
                row['cash_status'] = 'stale_report'
                break
            if not np.isfinite(own[field].values).all():
                row['cash_status'] = 'invalid_value'
                break
            series.append(own.set_index('report')[field])
        if len(series) == 2:
            if not series[0].index.equals(series[1].index):
                row['cash_status'] = 'report_mismatch'
            else:
                cf, op = float(series[0].sum()), float(series[1].sum())
                row.update(cash_ttm=cf, profit_ttm=op,
                           cash_report=str(series[0].index[-1].date()))
                if not np.isfinite([cf, op]).all():
                    row['cash_status'] = 'invalid_value'
                elif op <= 0:
                    row['cash_status'] = 'nonpositive_profit'
                else:
                    value = cf / op
                    row.update(cash_support=value if np.isfinite(value) else np.nan,
                               cash_status='ok' if np.isfinite(value) else 'invalid_ratio')
        output.append(row)
    return pd.DataFrame(output).set_index('code', drop=False)


def cash_period_statistics(snapshot, frame, scopes=('pool_raw', 'strong_raw', 'pool_neutral'), horizons=(20, 40, 60)):
    rows = []
    for scope in scopes:
        for h in horizons:
            groups = {}
            for group, label in enumerate(('low', 'mid', 'high'), 1):
                own = (frame[frame['group_' + scope].eq(group)]
                       if 'group_' + scope in frame else pd.DataFrame())
                returns = clean(own['r%d' % h]) if len(own) else pd.Series(dtype=float)
                known = returns.dropna()
                row = dict(signal_day=str(snapshot['signal_day'].date()),
                           asof=str(snapshot['asof'].date()), scope=scope, horizon=h, group=label,
                           window_status=snapshot['status_%d' % h], n=len(own), observed=len(known),
                           coverage=len(known) / len(own) if len(own) else np.nan,
                           return_mean=float(returns.mean()) if len(own) and returns.notna().all() else np.nan,
                           return_available=float(known.mean()) if len(known) else np.nan,
                           median_available=float(known.median()) if len(known) else np.nan,
                           win_available=float(known.gt(0).mean()) if len(known) else np.nan,
                           loss15_available=float(known.le(-.15).mean()) if len(known) else np.nan,
                           gain15_available=float(known.ge(.15).mean()) if len(known) else np.nan,
                           cash_support_mean=float(own.cash_support.mean()) if len(own) else np.nan,
                           snapshot_error=snapshot['error'])
                groups[label] = row
                rows.append(row)
            low, high = groups['low'], groups['high']
            difference = dict(high)
            difference.update(group='high_minus_low', n=low['n'] + high['n'],
                              observed=low['observed'] + high['observed'],
                              coverage=min(low['coverage'], high['coverage'])
                              if np.isfinite([low['coverage'], high['coverage']]).all() else np.nan)
            for key in ('return_mean', 'return_available', 'median_available', 'win_available',
                        'loss15_available', 'gain15_available', 'cash_support_mean'):
                difference[key] = high[key] - low[key]
            rows.append(difference)
    return rows
