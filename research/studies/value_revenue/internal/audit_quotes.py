"""独立重查行情端点，不修改冻结信号或原导出。

运行示例：.conda/python.exe -m research.studies.value_revenue.internal.audit_quotes
    --start 2025-06-28 --end 2026-07-05
日期必须在账户权限内。凭证仅从环境/.env读取，不输出；原始行情按日缓存。
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
DATA_DIR = STUDY / 'data'
ROOT = STUDY.parents[2]
PREFIX = 'expectations_v1_'
FIELDS = ['open', 'factor', 'paused']
MODES = {'raw': None, 'pre': 'pre', 'post': 'post'}


def positive(values):
    return values.notna() & np.isfinite(values) & (values > 0)


def compare_windows(returns, endpoints, security_info=None):
    """同一重查时点的前/后复权收益互证；另重放原动态前复权四舍五入口径。"""
    out = returns.copy()
    for side in ('entry', 'exit'):
        quotes = endpoints.rename(columns={'date': side})
        quotes = quotes.rename(columns={c: side + '_' + c for c in quotes if c not in ('code', side)})
        out = out.merge(quotes, on=['code', side], how='left', validate='many_to_one')
    # fill_paused=False可能使paused本身也为空；单独重查状态，填充值不用于收益。
    entry_paused = out.entry_raw_paused
    exit_paused = out.exit_raw_paused
    if 'entry_status_paused' in out:
        entry_paused = entry_paused.fillna(out.entry_status_paused)
        exit_paused = exit_paused.fillna(out.exit_status_paused)
    entry_ok = positive(out.entry_raw_open) & (entry_paused == 0)
    exit_ok = positive(out.exit_raw_open) & (exit_paused == 0)
    raw_ok = entry_ok & exit_ok
    post_ok = raw_ok & positive(out.entry_post_open) & positive(out.exit_post_open)
    pre_ok = raw_ok & positive(out.entry_pre_open) & positive(out.exit_pre_open)
    out['reference_return'] = (out.exit_post_open / out.entry_post_open - 1).where(post_ok)
    out['pre_reference_return'] = (out.exit_pre_open / out.entry_pre_open - 1).where(pre_ok)
    out['raw_return'] = (out.exit_raw_open / out.entry_raw_open - 1).where(raw_ok)
    factor_ok = positive(out.entry_post_factor) & positive(out.exit_post_factor)
    out['factor_ratio'] = (out.exit_post_factor / out.entry_post_factor).where(factor_ok)
    adjusted_entry = out.entry_raw_open / out.factor_ratio
    out['dynamic_entry_unrounded'] = adjusted_entry
    out['dynamic_entry_rounded'] = adjusted_entry.round(2)
    out['replayed_return'] = (out.exit_raw_open / out.dynamic_entry_rounded - 1).where(
        raw_ok & factor_ok & positive(out.dynamic_entry_rounded))
    out['adjustment_present'] = factor_ok & ~np.isclose(out.factor_ratio, 1, atol=1e-10, rtol=1e-10)
    out['pre_post_difference'] = out.pre_reference_return - out.reference_return
    out['export_minus_reference'] = out.future_return - out.reference_return
    out['export_minus_replayed'] = out.future_return - out.replayed_return
    # 两套报价处于同一供应商、不同环境；复查差异不直接等同原回测代码错误。
    out['endpoint_class'] = np.select([
        entry_paused == 1,
        ~entry_ok,
        exit_paused == 1,
        ~exit_ok,
        ~post_ok | ~pre_ok | ~factor_ok,
    ], ['entry_paused', 'entry_unavailable', 'exit_paused', 'exit_unavailable',
        'adjusted_quote_unavailable'], default='both_endpoints_available')
    if security_info:
        out['security_end_date'] = out.code.map({c: i['end_date'] for c, i in security_info.items()})
        ended = out.security_end_date.notna() & (out.exit > out.security_end_date)
        out.loc[ended & (out.endpoint_class == 'exit_unavailable'), 'endpoint_class'] = 'exit_after_security_end'
    out['export_missing'] = out.future_return.isna()
    out['entry_status_disagrees'] = out.entry_valid != entry_ok
    return out


def fetch_endpoints(api, requests, cache):
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    quota_before = api.get_query_count()
    print('Quota before:', quota_before, flush=True)
    for date, day in requests.groupby('date', sort=True):
        codes = sorted(day.code.unique())
        pieces = []
        for mode, fq in MODES.items():
            path = cache / ('%s_%s.csv' % (date, mode))
            if path.exists():
                frame = pd.read_csv(path, dtype={'code': str}, float_precision='round_trip')
            else:
                batches = []
                for offset in range(0, len(codes), 2500):
                    result = api.get_price(codes[offset:offset + 2500], start_date=date, end_date=date,
                                           frequency='daily', fields=FIELDS, fq=fq, panel=False,
                                           skip_paused=False, fill_paused=False, round=False)
                    if result is not None and not result.empty:
                        batches.append(result)
                frame = (pd.concat(batches, ignore_index=True) if batches else
                         pd.DataFrame(columns=['code', 'time'] + FIELDS))
                if not frame.empty:
                    assert set(pd.to_datetime(frame.time).dt.strftime('%Y-%m-%d')) == {date}
                    assert not frame.code.duplicated().any()
                    assert set(frame.code).issubset(codes)
                # 全部请求股票保留，包括供应商没有返回行的股票。
                frame = frame.set_index('code').reindex(codes).rename_axis('code').reset_index()
                frame.to_csv(path, index=False, encoding='utf-8-sig')
            assert set(frame.code) == set(codes), 'Cache does not cover the same endpoint request'
            frame = frame[['code'] + FIELDS].rename(columns={c: mode + '_' + c for c in FIELDS})
            pieces.append(frame.set_index('code'))
        joined = pd.concat(pieces, axis=1).reset_index()
        unknown = joined.loc[joined.raw_paused.isna(), 'code'].tolist()
        if unknown:
            path = cache / ('%s_status.csv' % date)
            if path.exists():
                status = pd.read_csv(path, dtype={'code': str}, float_precision='round_trip')
            else:
                status = api.get_price(unknown, start_date=date, end_date=date, frequency='daily',
                                       fields=['open', 'paused', 'volume'], fq=None, panel=False,
                                       skip_paused=False, fill_paused=True, round=False)
                if status is None or status.empty:
                    status = pd.DataFrame(columns=['code', 'time', 'open', 'paused', 'volume'])
                status = status.set_index('code').reindex(unknown).rename_axis('code').reset_index()
                status.to_csv(path, index=False, encoding='utf-8-sig')
            assert not status.code.duplicated().any()
            assert set(status.code) == set(unknown)
            status = status[['code', 'open', 'paused', 'volume']].rename(
                columns={c: 'status_' + c for c in ('open', 'paused', 'volume')})
            joined = joined.merge(status, on='code', how='left', validate='one_to_one')
        else:
            for name in ('open', 'paused', 'volume'):
                joined['status_' + name] = np.nan
        joined['date'] = date
        rows.append(joined)
        print('Rechecked %s: %d endpoints, raw/pre/post' % (date, len(codes)), flush=True)
    return pd.concat(rows, ignore_index=True), quota_before, api.get_query_count()


def fetch_security_info(api, comparison, path):
    codes = sorted(comparison.loc[comparison.endpoint_class == 'exit_unavailable', 'code'].unique())
    info = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for code in codes:
        if code not in info:
            item = api.get_security_info(code)
            if item is not None:
                info[code] = {name: str(getattr(item, name, None))
                              for name in ('display_name', 'start_date', 'end_date', 'type')}
    path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def make_summary(comparison):
    records = []
    stock = comparison[comparison.code != '000906.XSHG']
    for horizon, block in stock.groupby('horizon', sort=True):
        observed = block.future_return.notna() & block.reference_return.notna()
        replayed = block.future_return.notna() & block.replayed_return.notna()
        dual = block.reference_return.notna() & block.pre_reference_return.notna()
        delta = block.loc[observed, 'export_minus_reference'].abs()
        records.append(dict(
            horizon=int(horizon), records=len(block), export_missing=int(block.export_missing.sum()),
            entry_paused=int((block.endpoint_class == 'entry_paused').sum()),
            exit_paused=int((block.endpoint_class == 'exit_paused').sum()),
            exit_after_security_end=int((block.endpoint_class == 'exit_after_security_end').sum()),
            endpoint_unavailable=int(block.endpoint_class.isin(['entry_unavailable', 'exit_unavailable',
                                                               'adjusted_quote_unavailable']).sum()),
            missing_now_available=int((block.export_missing & block.reference_return.notna()).sum()),
            entry_status_disagrees=int(block.entry_status_disagrees.sum()),
            adjustment_windows=int((observed & block.adjustment_present).sum()),
            comparable_n=int(observed.sum()),
            pre_post_checked_n=int(dual.sum()),
            pre_post_mismatch_1e8=int((block.loc[dual, 'pre_post_difference'].abs() > 1e-8).sum()),
            replay_checked_n=int(replayed.sum()),
            replay_mismatch_1e8=int((block.loc[replayed, 'export_minus_replayed'].abs() > 1e-8).sum()),
            reference_difference_max=delta.max(),
            reference_difference_mean=delta.mean(),
            reference_difference_gt_1bp=int((delta > .0001).sum()),
            reference_difference_gt_10bp=int((delta > .001).sum()),
            reference_difference_gt_100bp=int((delta > .01).sum()),
        ))
    return pd.DataFrame(records)


def rounding_ic_sensitivity(comparison, snapshots, n_groups=5, min_ic_n=100):
    """固定全部原始因子和名单，仅比较报价精度造成的IC差异。"""
    from research.tools.single_factor.single_factor_test import evaluate_period
    snapshots_by_signal = {signal: block.set_index('code')
                           for signal, block in snapshots.groupby('signal')}
    rows = []
    for (signal, horizon), block in comparison.groupby(['signal', 'horizon']):
        block = block.set_index('code')
        snapshot = snapshots_by_signal[signal]
        for mode in ('raw', 'neutral'):
            for factor in ('value', 'improvement', 'additive', 'product'):
                values = snapshot[mode + '_' + factor]
                old, _ = evaluate_period(values, block.future_return, n_groups, min_ic_n)
                new, _ = evaluate_period(values, block.reference_return, n_groups, min_ic_n)
                rows.append(dict(signal=signal, horizon=horizon, mode=mode, factor=factor,
                                 export_ic=old['ic'], unrounded_ic=new['ic'],
                                 difference=new['ic'] - old['ic']))
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--offline', action='store_true', help='只用已有行情缓存，不连接账户')
    args = parser.parse_args()
    start, end = (pd.Timestamp(d).strftime('%Y-%m-%d') for d in (args.start, args.end))
    assert start <= end
    path = DATA_DIR / (PREFIX + 'returns.csv')
    input_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    data = pd.read_csv(path, dtype={'code': str}, float_precision='round_trip')
    assert not data.duplicated(['code', 'signal', 'horizon']).any()
    selected = data[(data.entry >= start) & (data.exit <= end)].copy()
    assert not selected.empty, 'No complete windows within requested permission range'
    requests = pd.concat([selected[['code', 'entry']].rename(columns={'entry': 'date'}),
                          selected[['code', 'exit']].rename(columns={'exit': 'date'})],
                         ignore_index=True).drop_duplicates()
    folder = STUDY / 'results' / 'quote_audit' / ('%s_%s' % (start, end))
    folder.mkdir(parents=True, exist_ok=True)
    if args.offline:
        def cache_missing(*args, **kwargs):
            raise RuntimeError('Required cache missing; offline mode will not query data')
        api = SimpleNamespace(get_price=cache_missing, get_security_info=cache_missing,
                              get_query_count=lambda: None)
    else:
        from dotenv import load_dotenv
        import jqdatasdk as api
        load_dotenv(ROOT / '.env')
        user, password = os.environ.get('JQ_USERNAME'), os.environ.get('JQ_PASSWORD')
        if not user or not password:
            raise RuntimeError('JQ credentials not configured; do not paste credentials into logs')
        api.auth(user, password)
    quotes, quota_before, quota_after = fetch_endpoints(api, requests, folder / 'endpoints')
    comparison = compare_windows(selected, quotes)
    security_path = folder / 'security_info.json'
    security_info = fetch_security_info(api, comparison, security_path)
    comparison = compare_windows(selected, quotes, security_info)
    comparison.to_csv(folder / 'comparison.csv', index=False, encoding='utf-8-sig')
    snapshots_path = DATA_DIR / (PREFIX + 'snapshots.csv')
    snapshots = pd.read_csv(snapshots_path, dtype={'code': str}, float_precision='round_trip')
    metadata = json.loads((DATA_DIR / (PREFIX + 'metadata.json')).read_text(encoding='utf-8-sig'))
    sensitivity = rounding_ic_sensitivity(comparison, snapshots, metadata['n_groups'], metadata['min_ic_n'])
    sensitivity.to_csv(folder / 'rounding_ic_sensitivity.csv', index=False, encoding='utf-8-sig')
    summary = make_summary(comparison)
    summary.to_csv(folder / 'summary.csv', index=False, encoding='utf-8-sig')
    anomalies = comparison[comparison.export_missing | comparison.entry_status_disagrees |
                           (comparison.export_minus_replayed.abs() > 1e-8) |
                           (comparison.pre_post_difference.abs() > 1e-8)]
    anomalies.to_csv(folder / 'anomalies.csv', index=False, encoding='utf-8-sig')
    manifest = dict(
        checked_at_utc=datetime.now(timezone.utc).isoformat(),
        requested_start=start, requested_end=end, actual_entry_first=selected.entry.min(),
        actual_exit_last=selected.exit.max(), records=len(selected), unique_endpoints=len(requests),
        dates=requests.date.nunique(), source_returns_sha256=input_hash,
        source_snapshots_sha256=hashlib.sha256(snapshots_path.read_bytes()).hexdigest(),
        endpoint_csv_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted((folder / 'endpoints').glob('*.csv'))},
        security_info_sha256=hashlib.sha256(security_path.read_bytes()).hexdigest(),
        provider='JQData SDK; same vendor as backtest, not an independent vendor',
        quote_modes=['unadjusted', 'pre', 'post'], fill_paused=False, round=False,
        status_only_fill_paused=True,
        offline_replay=args.offline,
        quota_before=quota_before, quota_after=quota_after,
        scope='complete windows within account permission; not whole historical sample',
        summary=summary.where(pd.notna(summary), None).to_dict(orient='records'))
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(summary.to_string(index=False), flush=True)
    print('Saved:', folder, flush=True)


if __name__ == '__main__':
    main()
