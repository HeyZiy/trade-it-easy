# -*- coding: utf-8 -*-
# 聚宽「投资研究」独立运行；不是回测策略。仅查询历史行情，不下单。
# 同目录需有expectations_v1_returns.csv、expectations_v1_metadata.json。
# 日期默认原导出完整区间；若平台历史权限不足，可填写RUN_START/RUN_END。
# 自动生成：audit_quotes.py + build_quote_audit_platform.py
import builtins as _python_builtins
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd
from jqdata import *

RUN_START = None
RUN_END = None
INPUT_PREFIX = 'expectations_v1_'
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
    _python_builtins.print('Quota before:', quota_before, flush=True)
    for date, day in requests.groupby('date', sort=True):
        codes = _python_builtins.sorted(day.code.unique())
        pieces = []
        for mode, fq in MODES.items():
            path = cache / ('%s_%s.csv' % (date, mode))
            if path.exists():
                frame = pd.read_csv(path, dtype={'code': _python_builtins.str}, float_precision='round_trip')
            else:
                batches = []
                for offset in _python_builtins.range(0, _python_builtins.len(codes), 2500):
                    result = api.get_price(codes[offset:offset + 2500], start_date=date, end_date=date,
                                           frequency='daily', fields=FIELDS, fq=fq, panel=False,
                                           skip_paused=False, fill_paused=False, round=False)
                    if result is not None and not result.empty:
                        batches.append(result)
                frame = (pd.concat(batches, ignore_index=True) if batches else
                         pd.DataFrame(columns=['code', 'time'] + FIELDS))
                if not frame.empty:
                    assert _python_builtins.set(pd.to_datetime(frame.time).dt.strftime('%Y-%m-%d')) == {date}
                    assert not frame.code.duplicated().any()
                    assert _python_builtins.set(frame.code).issubset(codes)
                # 全部请求股票保留，包括供应商没有返回行的股票。
                frame = frame.set_index('code').reindex(codes).rename_axis('code').reset_index()
                frame.to_csv(path, index=False, encoding='utf-8-sig')
            assert _python_builtins.set(frame.code) == _python_builtins.set(codes), 'Cache does not cover the same endpoint request'
            frame = frame[['code'] + FIELDS].rename(columns={c: mode + '_' + c for c in FIELDS})
            pieces.append(frame.set_index('code'))
        joined = pd.concat(pieces, axis=1).reset_index()
        unknown = joined.loc[joined.raw_paused.isna(), 'code'].tolist()
        if unknown:
            path = cache / ('%s_status.csv' % date)
            if path.exists():
                status = pd.read_csv(path, dtype={'code': _python_builtins.str}, float_precision='round_trip')
            else:
                status = api.get_price(unknown, start_date=date, end_date=date, frequency='daily',
                                       fields=['open', 'paused', 'volume'], fq=None, panel=False,
                                       skip_paused=False, fill_paused=True, round=False)
                if status is None or status.empty:
                    status = pd.DataFrame(columns=['code', 'time', 'open', 'paused', 'volume'])
                status = status.set_index('code').reindex(unknown).rename_axis('code').reset_index()
                status.to_csv(path, index=False, encoding='utf-8-sig')
            assert not status.code.duplicated().any()
            assert _python_builtins.set(status.code) == _python_builtins.set(unknown)
            status = status[['code', 'open', 'paused', 'volume']].rename(
                columns={c: 'status_' + c for c in ('open', 'paused', 'volume')})
            joined = joined.merge(status, on='code', how='left', validate='one_to_one')
        else:
            for name in ('open', 'paused', 'volume'):
                joined['status_' + name] = np.nan
        joined['date'] = date
        rows.append(joined)
        _python_builtins.print('Rechecked %s: %d endpoints, raw/pre/post' % (date, _python_builtins.len(codes)), flush=True)
    return pd.concat(rows, ignore_index=True), quota_before, api.get_query_count()

def fetch_security_info(api, comparison, path):
    codes = _python_builtins.sorted(comparison.loc[comparison.endpoint_class == 'exit_unavailable', 'code'].unique())
    info = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for code in codes:
        if code not in info:
            item = api.get_security_info(code)
            if item is not None:
                info[code] = {name: _python_builtins.str(_python_builtins.getattr(item, name, None))
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
        records.append(_python_builtins.dict(
            horizon=_python_builtins.int(horizon), records=_python_builtins.len(block), export_missing=_python_builtins.int(block.export_missing.sum()),
            entry_paused=_python_builtins.int((block.endpoint_class == 'entry_paused').sum()),
            exit_paused=_python_builtins.int((block.endpoint_class == 'exit_paused').sum()),
            exit_after_security_end=_python_builtins.int((block.endpoint_class == 'exit_after_security_end').sum()),
            endpoint_unavailable=_python_builtins.int(block.endpoint_class.isin(['entry_unavailable', 'exit_unavailable',
                                                               'adjusted_quote_unavailable']).sum()),
            missing_now_available=_python_builtins.int((block.export_missing & block.reference_return.notna()).sum()),
            entry_status_disagrees=_python_builtins.int(block.entry_status_disagrees.sum()),
            adjustment_windows=_python_builtins.int((observed & block.adjustment_present).sum()),
            comparable_n=_python_builtins.int(observed.sum()),
            pre_post_checked_n=_python_builtins.int(dual.sum()),
            pre_post_mismatch_1e8=_python_builtins.int((block.loc[dual, 'pre_post_difference'].abs() > 1e-8).sum()),
            replay_checked_n=_python_builtins.int(replayed.sum()),
            replay_mismatch_1e8=_python_builtins.int((block.loc[replayed, 'export_minus_replayed'].abs() > 1e-8).sum()),
            reference_difference_max=delta.max(),
            reference_difference_mean=delta.mean(),
            reference_difference_gt_1bp=_python_builtins.int((delta > .0001).sum()),
            reference_difference_gt_10bp=_python_builtins.int((delta > .001).sum()),
            reference_difference_gt_100bp=_python_builtins.int((delta > .01).sum()),
        ))
    return pd.DataFrame(records)

def run_quote_audit():
    metadata = json.loads(Path(INPUT_PREFIX + 'metadata.json').read_text(encoding='utf-8-sig'))
    start = pd.Timestamp(RUN_START or metadata['first_day']).strftime('%Y-%m-%d')
    end = pd.Timestamp(RUN_END or metadata['last_day']).strftime('%Y-%m-%d')
    path = Path(INPUT_PREFIX + 'returns.csv')
    data = pd.read_csv(path, dtype={'code': _python_builtins.str}, float_precision='round_trip')
    assert not data.duplicated(['code', 'signal', 'horizon']).any()
    selected = data[(data.entry >= start) & (data.exit <= end)].copy()
    assert not selected.empty, 'No complete windows within requested dates'
    requests = pd.concat([selected[['code', 'entry']].rename(columns={'entry': 'date'}),
                          selected[['code', 'exit']].rename(columns={'exit': 'date'})],
                         ignore_index=True).drop_duplicates()
    folder = Path('expectations_quote_audit_%s_%s' % (start, end))
    folder.mkdir(parents=True, exist_ok=True)
    api = SimpleNamespace(get_price=get_price, get_security_info=get_security_info,
                          get_query_count=lambda: {'status': 'not_queried_in_research'})
    quotes, _, _ = fetch_endpoints(api, requests, folder / 'endpoints')
    comparison = compare_windows(selected, quotes)
    security_path = folder / 'security_info.json'
    security_info = fetch_security_info(api, comparison, security_path)
    comparison = compare_windows(selected, quotes, security_info)
    comparison.to_csv(folder / 'comparison.csv', index=False, encoding='utf-8-sig')
    summary = make_summary(comparison)
    summary.to_csv(folder / 'summary.csv', index=False, encoding='utf-8-sig')
    anomalies = comparison[comparison.export_missing | comparison.entry_status_disagrees |
                           (comparison.export_minus_replayed.abs() > 1e-8) |
                           (comparison.pre_post_difference.abs() > 1e-8)]
    anomalies.to_csv(folder / 'anomalies.csv', index=False, encoding='utf-8-sig')
    manifest = _python_builtins.dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
                    start=start, end=end, records=_python_builtins.len(selected), unique_endpoints=_python_builtins.len(requests),
                    source_returns_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    round=False, fill_paused=False, status_only_fill_paused=True,
                    provider='JoinQuant research; same vendor as backtest',
                    original_metadata=metadata,
                    endpoint_csv_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in _python_builtins.sorted((folder / 'endpoints').glob('*.csv'))},
                    security_info_sha256=hashlib.sha256(security_path.read_bytes()).hexdigest())
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    with zipfile.ZipFile(_python_builtins.str(folder) + '.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for item in _python_builtins.sorted(folder.rglob('*')):
            if item.is_file():
                archive.write(_python_builtins.str(item), _python_builtins.str(item.relative_to(folder)))
    _python_builtins.print(summary.to_string(index=False))
    _python_builtins.print('Audit completed; download:', _python_builtins.str(folder) + '.zip')
    return summary


run_quote_audit()
