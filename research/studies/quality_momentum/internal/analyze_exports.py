"""读取聚宽原始导出，用与平台相同的源码复算并核对汇总。

python -m research.studies.quality_momentum.internal.analyze_exports \
    --prefix local/downloads/quality_momentum_v5_turnover_probe \
    --output-dir local/quality_momentum_analysis
"""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from .account_statistics import account_metrics, yearly_metrics, comparison_rows
from .probe_statistics import summarize_periods, response_summary


def _require_columns(frame, columns, name):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f'{name} 缺少字段：{sorted(missing)}')


def _match(actual, exported, keys, name):
    """只比较本地能复算的列；平台额外诊断列保留在原始文件中。"""
    _require_columns(exported, actual.columns, name)
    left, right = actual.copy(), exported[actual.columns].copy()
    for frame in (left, right):
        if frame.duplicated(keys).any():
            raise ValueError(f'{name} 存在重复汇总键：{keys}')
        for key in keys:
            frame[key] = frame[key].astype(str)
    left = left.sort_values(keys).reset_index(drop=True)
    right = right.sort_values(keys).reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(left, right, check_dtype=False, check_exact=False,
                                      rtol=1e-9, atol=1e-9)
    except AssertionError as exc:
        raise ValueError(f'{name} 与本地复算不一致：{exc}') from exc


def _account_results(nav, settings):
    columns = ['account', 'date', 'equity', 'cash_pct', 'holdings', 'turnover',
               'traded_value', 'data_paused']
    _require_columns(nav, columns, 'nav')
    if nav.empty or nav.duplicated(['account', 'date']).any():
        raise ValueError('nav 为空或存在重复账户日期')
    nav = nav.copy()
    dates = pd.to_datetime(nav.date, errors='raise')
    nav['date'] = dates.dt.strftime('%Y-%m-%d')
    if nav.duplicated(['account', 'date']).any():
        raise ValueError('nav 日期规范化后存在重复账户日期')
    numeric = ['equity', 'cash_pct', 'holdings', 'turnover', 'traded_value']
    nav[numeric] = nav[numeric].apply(pd.to_numeric, errors='raise')
    if not np.isfinite(nav[numeric].values).all() or not nav.equity.gt(0).all():
        raise ValueError('nav 包含缺失、非有限数值或非正账户权益')
    paused = nav.data_paused.astype(str).str.lower().map(
        {'true': True, 'false': False, '0': False, '1': True})
    if paused.isna().any():
        raise ValueError('nav.data_paused 必须为布尔值')
    nav['data_paused'] = paused
    initial = float(settings['account_cash'])
    annual_days = float(settings['annual_days'])
    if not np.isfinite([initial, annual_days]).all() or min(initial, annual_days) <= 0:
        raise ValueError('account_cash 和 annual_days 必须为正数')
    if nav.account.nunique() != int(settings['accounts']):
        raise ValueError('nav 账户数与 settings 不一致')
    summaries, annual, calendar = [], [], None
    for account, block in nav.groupby('account'):
        block = block.sort_values('date')
        days = block.date.tolist()
        if calendar is not None and days != calendar:
            raise ValueError('不同账户的净值日期不一致，不能作同区间对照')
        calendar = days
        seed = block.seed.iloc[0] if 'seed' in block else np.nan
        rows = block.to_dict('records')
        summary = account_metrics(rows, initial, annual_days)
        summary.update(account=account, seed=seed)
        summaries.append(summary)
        state = SimpleNamespace(nav_rows=rows, initial_equity=initial, label=account, seed=seed)
        annual.extend(yearly_metrics(state, annual_days))
    comparisons = comparison_rows(summaries, 'full')
    for year in sorted({row['year'] for row in annual}):
        comparisons.extend(comparison_rows([row for row in annual if row['year'] == year], str(year)))
    return {'summary': pd.DataFrame(summaries), 'annual': pd.DataFrame(annual),
            'comparison': pd.DataFrame(comparisons)}


def analyze_exports(prefix, output_dir, *, sample_every=None):
    """不修改原始导出。核对通过后写复算 CSV、输入文件哈希和简报。

    prefix 为公共文件名前缀，不含 _settings.csv 等后缀。支持排序对照、
    换手、财报反应、现金支撑及增长口径探针；缺少文件或汇总不一致时抛出异常。
    """
    prefix, output_dir = Path(prefix), Path(output_dir)
    inputs = {}

    def read(kind):
        path = prefix.parent / f'{prefix.name}_{kind}.csv'
        content = path.read_bytes()
        inputs[kind] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(content).hexdigest()}
        # Read the exact bytes that were hashed, rather than reopen a changing file.
        from io import BytesIO
        return pd.read_csv(BytesIO(content), encoding='utf-8-sig', float_precision='round_trip')

    frame = read('settings')
    if {'setting', 'value'} <= set(frame.columns):
        if frame.setting.duplicated().any():
            raise ValueError('settings 存在重复设置项')
        settings = dict(zip(frame.setting, frame.value))
        periods = read('periods')
        _require_columns(periods, ['signal_day', 'scope', 'horizon', 'group', 'window_status'], 'periods')
        mode = settings.get('probe') if settings.get('probe') in ('earnings_response', 'cash_support', 'growth_basis') else 'turnover'
        keys = ['signal_day', 'scope', 'horizon', 'group']
        if mode == 'earnings_response':
            _require_columns(periods, ['metric'], 'periods')
            keys.append('metric')
        if periods.empty or periods.duplicated(keys).any():
            raise ValueError('periods 为空或存在重复期次分组')
        recorded = settings.get('sample_every')
        if recorded is None and sample_every is None:
            raise ValueError('旧导出缺少 sample_every；请显式指定 --sample-every')
        if recorded is not None and sample_every is not None and int(recorded) != sample_every:
            raise ValueError('sample_every 与平台 settings 不一致')
        sample_every = int(recorded) if recorded is not None else sample_every
        if sample_every <= 0:
            raise ValueError('sample_every 必须为正整数')
        summarize = response_summary if mode == 'earnings_response' else summarize_periods
        results = {'summary': summarize(periods, sample_every)}
        keys = ['window', 'scope', 'horizon', 'group'] + (['metric'] if mode == 'earnings_response' else [])
        _match(results['summary'], read('summary'), keys, 'summary')
        if mode == 'cash_support':
            from .cash_support import cash_support_features, cash_period_statistics, PERIOD_COLUMNS as CASH_PERIOD_COLUMNS
            samples, quarters = read('samples'), read('quarters')
            recomputed = []
            for day, block in periods.groupby('signal_day'):
                own = samples[samples.signal_day.eq(day)].copy()
                raw = quarters[quarters.signal_day.eq(day)]
                members = own[own.code.notna() & own.code.ne('')]
                if not members.empty:
                    cash = raw[raw.table.eq('cash_flow')].rename(columns={'value': 'net_operate_cash_flow'})
                    profit = raw[raw.table.eq('income')].rename(columns={'value': 'operating_profit'})
                    features = cash_support_features(cash, profit, members.code.tolist(), own['asof'].iloc[0])
                    _match(features[['code', 'cash_support', 'cash_ttm', 'profit_ttm', 'cash_status']].reset_index(drop=True),
                           members, ['code'], 'cash_support_features')
                snapshot = dict(signal_day=pd.Timestamp(day), asof=pd.Timestamp(block['asof'].iloc[0]),
                                error=block.snapshot_error.iloc[0])
                for h, sub in block.groupby('horizon'):
                    snapshot['status_%d' % h] = sub.window_status.iloc[0]
                recomputed.extend(cash_period_statistics(snapshot, own))
            _match(pd.DataFrame(recomputed).reindex(columns=CASH_PERIOD_COLUMNS), periods,
                   ['signal_day', 'scope', 'horizon', 'group'], 'cash_support_periods')
        if mode == 'growth_basis':
            from .growth_basis import growth_features, freeze_growth, growth_period_statistics, GROWTH_ARMS, PERIOD_COLUMNS as GROWTH_COLUMNS
            samples, quarters = read('samples'), read('quarters')
            if samples.duplicated(['signal_day', 'code']).any():
                raise ValueError('增长口径存在重复成员')
            recomputed = []
            for day, block in periods.groupby('signal_day'):
                own = samples[samples.signal_day.eq(day)].copy()
                members = own[own.code.notna() & own.code.ne('')].copy()
                if not members.empty:
                    for col in ['common'] + ['pass_' + arm for arm in GROWTH_ARMS]:
                        parsed = members[col].astype(str).str.lower().map({'true': True, 'false': False})
                        if parsed.isna().any():
                            raise ValueError('增长口径资格必须为布尔值')
                        members[col] = parsed
                    raw = quarters[quarters.signal_day.eq(day)]
                    features = growth_features(raw, members.code.tolist(), members['asof'].iloc[0])
                    for arm in GROWTH_ARMS[1:]:
                        members[arm + '_report'] = members[arm + '_report'].fillna('')
                    _match(features.reset_index(drop=True), members, ['code'], 'growth_features')
                    frozen = freeze_growth(members)
                    flags = ['code', 'common'] + ['pass_' + arm for arm in GROWTH_ARMS]
                    _match(frozen[flags], members, ['code'], 'growth_membership')
                    own = members
                snapshot = dict(signal_day=pd.Timestamp(day), asof=pd.Timestamp(block['asof'].iloc[0]),
                                error=block.snapshot_error.iloc[0])
                for h, sub in block.groupby('horizon'):
                    snapshot['status_%d' % h] = sub.window_status.iloc[0]
                recomputed.extend(growth_period_statistics(snapshot, own))
            _match(pd.DataFrame(recomputed).reindex(columns=GROWTH_COLUMNS), periods,
                   ['signal_day', 'scope', 'horizon', 'group'], 'growth_periods')
    else:
        if len(frame) != 1:
            raise ValueError('账户 settings 必须只有一行')
        settings = frame.iloc[0].to_dict()
        mode = 'rank_control'
        results = _account_results(read('nav'), settings)
        _match(results['summary'], read('summary'), ['account'], 'summary')
        _match(results['annual'], read('annual'), ['account', 'year'], 'annual')
        exported = read('comparison')
        if results['comparison'].empty:
            if not exported.empty:
                raise ValueError('单账户实验不应包含随机对照结果')
        else:
            _match(results['comparison'], exported, ['period', 'metric'], 'comparison')
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, result in results.items():
        result.to_csv(output_dir / f'recomputed_{name}.csv', index=False)
    sources = [Path(__file__), Path(__file__).with_name('account_statistics.py'),
               Path(__file__).with_name('probe_statistics.py'),
               Path(__file__).resolve().parents[3] / 'tools/factor_analysis/core.py']
    if mode == 'cash_support':
        sources.append(Path(__file__).with_name('cash_support.py'))
    if mode == 'growth_basis':
        sources.append(Path(__file__).with_name('growth_basis.py'))
    manifest = {'mode': mode, 'platform_summary_matches': True, 'inputs': inputs,
                'source_v5_sha256': settings.get('source_v5_sha256'),
                'platform_build_sources_sha256': settings.get('build_sources_sha256'),
                'probe_sample_every': sample_every,
                'local_sources_sha256': {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in sources}}
    (output_dir / 'verification.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    warning = ('账户结果来自平台净值，费用已含在净值中；随机组只作描述性对照。'
               if mode == 'rank_control' else
               '本结果是候选报价或路径诊断，不是含费用和交易约束的账户收益。')
    report = (f'# 聚宽导出复算：{mode}\n\n'
              '本地复算与平台导出汇总一致。输入文件及源码哈希见 verification.json。\n\n'
              f'{warning}\n\n完整结果见 recomputed_*.csv。\n\n'
              f'```text\n{results["summary"].to_string(index=False)}\n```\n')
    (output_dir / 'report.md').write_text(report, encoding='utf-8')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True, help='导出公共前缀，不含 _settings.csv')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--sample-every', type=int, help='仅用于缺少该设置的旧探针导出，需明确原实验采样间隔')
    args = parser.parse_args()
    analyze_exports(args.prefix, args.output_dir, sample_every=args.sample_every)
    print(f'复算与平台汇总一致：{Path(args.output_dir).resolve()}')


if __name__ == '__main__':
    main()
