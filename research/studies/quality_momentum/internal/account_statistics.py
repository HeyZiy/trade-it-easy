"""日频排序对照的账户统计；本地导出分析与聚宽生成脚本共用。"""
import builtins as _b
import pandas as pd


def account_metrics(rows, initial, annual_days=245.0):
    """收益及回撤从原始本金起算，包含首日费用；年度起点由上年末承接。"""
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    equity = pd.to_numeric(df['equity'])
    total_return = equity.iloc[-1] / initial - 1
    peak = equity.cummax().clip(lower=initial)
    return dict(first_date=df['date'].iloc[0], last_date=df['date'].iloc[-1],
        trading_days=_b.len(df), initial_equity=initial, final_equity=equity.iloc[-1],
        pnl=equity.iloc[-1] - initial, return_pct=total_return * 100,
        annual_return_pct=((1 + total_return) ** (annual_days / _b.len(df)) - 1) * 100,
        max_drawdown_pct=-(equity / peak - 1).min() * 100,
        average_cash_pct=df['cash_pct'].mean(), average_holdings=df['holdings'].mean(),
        turnover_sum=df['turnover'].sum(), traded_value=df['traded_value'].sum(),
        data_paused_days=_b.int(df['data_paused'].sum()))

def yearly_metrics(state, annual_days=245.0):
    groups = {}
    for row in state.nav_rows:
        groups.setdefault(_b.int(row['date'][:4]), []).append(row)
    initial, rows = state.initial_equity, []
    for year in _b.sorted(groups):
        metrics = account_metrics(groups[year], initial, annual_days)
        metrics.update(year=year, account=state.label, seed=state.seed)
        rows.append(metrics)
        initial = metrics['final_equity']
    return rows

def comparison_rows(summaries, period):
    momentum = _b.next((r for r in summaries if r['account'] == 'momentum'), None)
    random = [r for r in summaries if r['account'] != 'momentum']
    if momentum is None or not random:
        return []
    rows = []
    for metric, lower_better in (('return_pct', False), ('max_drawdown_pct', True),
                                 ('average_cash_pct', None), ('turnover_sum', None)):
        values = pd.Series([r[metric] for r in random], dtype=float)
        value = momentum[metric]
        better = ((values > value + 1e-10 if lower_better else values < value - 1e-10)
                  if lower_better is not None else None)
        rows.append(dict(period=period, metric=metric, momentum=value,
            random_count=_b.len(random), random_min=values.min(), random_median=values.median(),
            random_max=values.max(), momentum_minus_median=value - values.median(),
            momentum_better_count=_b.int(better.sum()) if better is not None else None,
            tied_count=_b.int((values - value).abs().le(1e-10).sum())))
    return rows
