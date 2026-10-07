"""审计已导出的平台证据，不查询新行情，不改变冻结信号或原研究口径。

从仓库根运行：.conda/python.exe -m research.studies.value_revenue.internal.analyze_exports
缺报价敏感性仅报告可观察样本均值，明确不作可成交组合收益。
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.studies.value_revenue.internal.core import (
    CELL_NAMES, FACTOR_NAMES, assign_cells, interaction_regression,
)
from research.tools.single_factor.single_factor_test import evaluate_period, summarize_ic


HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
DATA_DIR = STUDY / 'data'
PREFIX = 'expectations_v1_'


def same_number(actual, expected, label):
    if pd.isna(actual) and pd.isna(expected):
        return
    if not np.isfinite(actual) or not np.isfinite(expected) or not np.isclose(actual, expected, atol=1e-9, rtol=1e-7):
        raise AssertionError('%s: %s != %s' % (label, actual, expected))


def stats_row(series, mode, horizon, metric, scope='all', lags=None):
    lag = max(3, int(np.ceil(horizon / 20)) - 1) if lags is None else lags
    stats = summarize_ic(series, lag)
    stats.update(mode=mode, horizon=int(horizon), metric=metric, scope=scope, hac_lags=lag)
    return stats


def load_data():
    metadata = json.loads((DATA_DIR / (PREFIX + 'metadata.json')).read_text(encoding='utf-8-sig'))
    frames = {}
    hashes = {}
    for name in ('audit', 'periods', 'snapshots', 'returns', 'summary'):
        path = DATA_DIR / (PREFIX + name + '.csv')
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        # 恢复to_csv导出的浮点值；普通解析的末位差异会改变同分秩和IC。
        frame = pd.read_csv(path, dtype={'code': str, 'industry': str}, float_precision='round_trip')
        for column in ('signal', 'asof', 'entry', 'exit', 'report', 'published',
                       'previous_report', 'previous_published'):
            if column in frame:
                frame[column] = pd.to_datetime(frame[column], errors='raise')
        frames[name] = frame
    hashes[PREFIX + 'metadata.json'] = hashlib.sha256((DATA_DIR / (PREFIX + 'metadata.json')).read_bytes()).hexdigest()
    return metadata, frames, hashes


def reconcile(metadata, frames):
    snapshots, returns, periods, audit = (frames[n] for n in ('snapshots', 'returns', 'periods', 'audit'))
    assert not snapshots.duplicated(['signal', 'code']).any()
    assert not returns.duplicated(['signal', 'horizon', 'code']).any()
    assert not periods.duplicated(['signal', 'horizon', 'mode']).any()
    assert not audit.duplicated(['signal']).any()
    assert audit['signal'].nunique() == metadata['signal_count']
    assert set(snapshots['signal']) == set(audit['signal'])
    assert (snapshots['published'] <= snapshots['asof']).all()
    assert (snapshots['previous_published'] <= snapshots['asof']).all()
    assert (snapshots['asof'] < snapshots['signal']).all()
    assert (snapshots['report'] < snapshots['published']).all()
    assert (snapshots['previous_report'] < snapshots['previous_published']).all()
    assert (snapshots['asof'] - snapshots['report']).dt.days.max() <= metadata['max_report_age']
    quarter_gap = (snapshots['report'].dt.to_period('Q').astype('int64')
                   - snapshots['previous_report'].dt.to_period('Q').astype('int64'))
    assert (quarter_gap == 1).all()
    assert np.allclose(snapshots['value'], 1 / snapshots['pe_ratio'], atol=1e-10)
    assert np.allclose(snapshots['improvement'], snapshots['revenue_yoy'] - snapshots['previous_revenue_yoy'])
    assert (periods['asof'] < periods['signal']).all()
    assert (periods['signal'] < periods['entry']).all()
    assert (periods['entry'] < periods['exit']).all()
    assert periods['exit'].max() <= pd.Timestamp(metadata['last_day'])
    assert snapshots.groupby('signal').size().sort_index().equals(
        audit.set_index('signal')['common_sample_n'].sort_index().rename(None))
    # 以实际20日成熟序列核验60/120日端点，完全不查询新的交易日历。
    sequence = periods[(periods['mode'] == 'raw') & (periods['horizon'] == 20)].sort_values('signal')
    signals = list(sequence['signal'])
    entries = list(sequence['entry']) + [sequence['exit'].iloc[-1]]
    positions = {signal: pos for pos, signal in enumerate(signals)}
    for row in periods.itertuples():
        assert row.horizon % metadata['rotate_every'] == 0
        pos = positions[row.signal]
        assert row.entry == entries[pos]
        assert row.exit == entries[pos + row.horizon // metadata['rotate_every']]
    snapshot_groups = {signal: block.set_index('code') for signal, block in snapshots.groupby('signal', sort=True)}
    period_keys = {(r.signal, r.horizon, r.mode): r for r in periods.itertuples()}
    merged_blocks = []
    factor_checks = 0
    regression_checks = 0
    for (signal, horizon), block in returns.groupby(['signal', 'horizon'], sort=True):
        snapshot = snapshot_groups[signal]
        assert set(block['code']) == set(snapshot.index) | {metadata['benchmark']}
        assert block['entry'].nunique() == block['exit'].nunique() == 1
        future = block.set_index('code')['future_return']
        for mode in ('raw', 'neutral'):
            row = period_keys[(signal, horizon, mode)]
            view = pd.DataFrame({name: snapshot[mode + '_' + name] for name in FACTOR_NAMES})
            cells = assign_cells(view['value'], view['improvement'])
            assert np.array_equal(cells.fillna('NA').values, snapshot[mode + '_cell'].fillna('NA').values)
            assert row.entry == block['entry'].iloc[0] and row.exit == block['exit'].iloc[0]
            same_number(row.benchmark, future.loc[metadata['benchmark']], 'benchmark')
            assert row.pool_n == len(snapshot)
            for name in FACTOR_NAMES:
                recomputed, _ = evaluate_period(view[name], future, metadata['n_groups'], metadata['min_ic_n'])
                for field in ('ic', 'coverage', 'paired_n', 'high_minus_low'):
                    same_number(getattr(row, name + '_' + field), recomputed[field], name + '_' + field)
                    factor_checks += 1
            if horizon == metadata['primary_horizon']:
                recomputed = interaction_regression(view, snapshot, future, metadata['min_ic_n'])
                for field in ('regression_value', 'regression_improvement', 'regression_interaction', 'regression_n'):
                    same_number(getattr(row, field), recomputed[field], field)
                    regression_checks += 1
            for cell in CELL_NAMES:
                codes = cells.index[cells == cell]
                observed = future.reindex(codes)
                assert len(codes) == getattr(row, cell + '_n')
                assert observed.notna().sum() == getattr(row, cell + '_valid_n')
                expected = observed.mean() if len(codes) >= metadata['min_cell_n'] and observed.notna().all() else np.nan
                same_number(getattr(row, cell), expected, cell)
        merged_blocks.append(block[block['code'] != metadata['benchmark']].merge(
            snapshot.reset_index(), on='code', how='left', validate='one_to_one', suffixes=('', '_snapshot')))
    merged = pd.concat(merged_blocks, ignore_index=True)
    assert (merged['signal'] == merged['signal_snapshot']).all()
    missing = merged['future_return'].isna()
    assert merged['entry_valid'].isin([True, False]).all()
    merged['missing_class'] = np.where(~missing, 'observed', np.where(
        ~merged['entry_valid'], 'entry_quote_invalid', 'later_endpoint_or_adjustment_missing'))
    summary_checks = 0
    for row in frames['summary'].itertuples():
        if row.scope != 'all' or row.metric.endswith('_paired_mean'):
            continue
        block = periods[(periods['mode'] == row.mode) & (periods['horizon'] == row.horizon)].sort_values('signal')
        expected = summarize_ic(block[row.metric], int(row.hac_lags))
        for field in ('mean', 'n', 't_hac'):
            same_number(getattr(row, field), expected[field], 'summary_' + field)
            summary_checks += 1
    return merged, dict(snapshot_rows=len(snapshots), return_rows=len(returns), period_rows=len(periods),
                        factor_field_checks=factor_checks, primary_regression_field_checks=regression_checks,
                        full_summary_field_checks=summary_checks)


def analyze(frames, merged):
    periods = frames['periods']
    records = []
    for (mode, horizon), block in periods.groupby(['mode', 'horizon']):
        block = block.sort_values('signal')
        for field in ('regression_value', 'regression_improvement', 'regression_interaction'):
            records.append(stats_row(block[field], mode, horizon, field))
        records.append(stats_row(block['additive_ic'] - block['value_ic'], mode, horizon, 'additive_minus_value_ic'))
        records.append(stats_row(block['additive_ic'] - block['improvement_ic'], mode, horizon, 'additive_minus_improvement_ic'))
        for lag in (0, 3, 6, 12):
            for field in ('regression_improvement', 'regression_interaction', 'improvement_ic', 'additive_minus_value_ic'):
                values = block[field] if field in block else block['additive_ic'] - block['value_ic']
                records.append(stats_row(values, mode, horizon, field, 'bandwidth_sensitivity', lag))
        stride = horizon // 20
        for phase in range(stride):
            sampled = block.iloc[phase::stride]
            for field in ('value_ic', 'improvement_ic', 'regression_improvement', 'regression_interaction'):
                records.append(stats_row(sampled[field], mode, horizon, field, 'nonoverlap_phase_%d' % phase, 3))
    coverage_rows, observed_rows = [], []
    for (signal, horizon), block in merged.groupby(['signal', 'horizon'], sort=True):
        for mode in ('raw', 'neutral'):
            observed = {}
            coverages = []
            for cell in CELL_NAMES:
                members = block[block[mode + '_cell'] == cell]
                valid = members['future_return'].notna()
                row = dict(signal=signal, horizon=horizon, mode=mode, cell=cell,
                           members=len(members), valid_n=int(valid.sum()),
                           coverage=valid.mean(), entry_invalid=int((~members['entry_valid']).sum()),
                           later_missing=int(((members['missing_class'] == 'later_endpoint_or_adjustment_missing')).sum()))
                coverage_rows.append(row)
                coverages.append(row['coverage'])
                observed[cell] = members['future_return'].mean()
            # 明确事后敏感性：固定分组的可观察子样本均值；不替代原主指标。
            observed_rows.append(dict(signal=signal, horizon=horizon, mode=mode,
                                      min_cell_coverage=min(coverages), **observed,
                                      observed_interaction=(observed['B'] - observed['A']) - (observed['D'] - observed['C'])))
    correlations = []
    for signal, block in frames['snapshots'].groupby('signal'):
        for mode in ('raw', 'neutral'):
            correlations.append(dict(signal=signal, mode=mode,
                                     value_improvement_rank_corr=block[mode + '_value'].rank().corr(block[mode + '_improvement'].rank()),
                                     improvement_revenue_level_rank_corr=block[mode + '_improvement'].rank().corr(block['revenue_yoy'].rank())))
    return pd.DataFrame(records), pd.DataFrame(coverage_rows), pd.DataFrame(observed_rows), pd.DataFrame(correlations)


def main():
    metadata, frames, hashes = load_data()
    print('Loaded exports; reconciling all ICs/groups and 60-day regressions...', flush=True)
    merged, checks = reconcile(metadata, frames)
    statistics, coverage, observed, correlations = analyze(frames, merged)
    folder = STUDY / 'results' / 'exports_audit'
    folder.mkdir(parents=True, exist_ok=True)
    for name, frame in (('statistics', statistics), ('cell_coverage', coverage),
                        ('observed_cells_sensitivity', observed), ('factor_correlations', correlations)):
        frame.to_csv(folder / (name + '.csv'), index=False, encoding='utf-8-sig')
    # 只汇总缺失原因可识别的范围，returns.csv没有停牌/退市/调整价的细分类字段。
    missing = merged[merged['future_return'].isna()]
    missing.groupby(['horizon', 'missing_class']).size().rename('n').to_csv(folder / 'missing_classes.csv')
    missing.groupby(['horizon', 'code']).size().rename('missing_windows').sort_values(ascending=False).to_csv(folder / 'missing_stock_counts.csv')
    missing.groupby(['horizon', 'industry']).size().rename('n').to_csv(folder / 'missing_industry_counts.csv')
    coverage_summary = coverage.groupby(['mode', 'horizon', 'cell']).agg(
        mean_coverage=('coverage', 'mean'), min_coverage=('coverage', 'min'),
        total_members=('members', 'sum'), missing_entry=('entry_invalid', 'sum'), missing_later=('later_missing', 'sum'))
    coverage_summary.to_csv(folder / 'coverage_summary.csv')
    manifest = dict(metadata=metadata, input_sha256=hashes, checks=checks,
                    missing_classes=missing['missing_class'].value_counts().to_dict(),
                    sensitivity_is_posthoc=True, external_quotes_queried=False)
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Reconciliation passed:', checks)
    print('Missing:', manifest['missing_classes'])
    print('Rank correlations:', correlations.groupby('mode')[['value_improvement_rank_corr', 'improvement_revenue_level_rank_corr']].mean().round(4).to_dict())
    print('Main-window paired tests and regression:')
    print(statistics[(statistics.horizon == 60) & (statistics.scope == 'all')][['mode','metric','n','mean','t_hac']].to_string(index=False))
    print('Nonoverlap improvement:')
    print(statistics[(statistics.horizon >= 60) & statistics.scope.str.startswith('nonoverlap') &
                     (statistics.metric == 'regression_improvement')][['mode','horizon','scope','n','mean','t_hac']].to_string(index=False))
    print('Mean coverage by cell:')
    print(coverage_summary.to_string())
    print('Observed-pairs cell sensitivity (no coverage threshold; not portfolio returns):')
    for (mode,horizon), block in observed.groupby(['mode','horizon']):
        stats=summarize_ic(block.sort_values('signal')['observed_interaction'],max(3,horizon//20-1))
        print(mode,horizon,'mean',round(stats['mean'],6),'t',round(stats['t_hac'],3),
              'worst cell coverage',round(block.min_cell_coverage.min(),4))
    print('Saved:', folder)


if __name__ == '__main__':
    main()
