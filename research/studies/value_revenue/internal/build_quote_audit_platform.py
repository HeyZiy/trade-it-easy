"""生成聚宽投资研究内的独立报价审计脚本，不需要重跑策略。"""
from pathlib import Path

from research.tools.single_factor.platform_codegen import definitions, protect_builtins


HERE = Path(__file__).resolve().parent
FUNCTIONS = ('positive', 'compare_windows', 'fetch_endpoints', 'fetch_security_info', 'make_summary')


def render():
    header = '''# -*- coding: utf-8 -*-
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

'''
    runner = '''
def run_quote_audit():
    metadata = json.loads(Path(INPUT_PREFIX + 'metadata.json').read_text(encoding='utf-8-sig'))
    start = pd.Timestamp(RUN_START or metadata['first_day']).strftime('%Y-%m-%d')
    end = pd.Timestamp(RUN_END or metadata['last_day']).strftime('%Y-%m-%d')
    path = Path(INPUT_PREFIX + 'returns.csv')
    data = pd.read_csv(path, dtype={'code': str}, float_precision='round_trip')
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
    manifest = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
                    start=start, end=end, records=len(selected), unique_endpoints=len(requests),
                    source_returns_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    round=False, fill_paused=False, status_only_fill_paused=True,
                    provider='JoinQuant research; same vendor as backtest',
                    original_metadata=metadata,
                    endpoint_csv_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in sorted((folder / 'endpoints').glob('*.csv'))},
                    security_info_sha256=hashlib.sha256(security_path.read_bytes()).hexdigest())
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    with zipfile.ZipFile(str(folder) + '.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(folder.rglob('*')):
            if item.is_file():
                archive.write(str(item), str(item.relative_to(folder)))
    print(summary.to_string(index=False))
    print('Audit completed; download:', str(folder) + '.zip')
    return summary


run_quote_audit()
'''
    return protect_builtins(header + definitions(HERE / 'audit_quotes.py', FUNCTIONS) + '\n' + runner)


if __name__ == '__main__':
    target = HERE / 'quote_audit_jq.py'
    target.write_text(render(), encoding='utf-8')
    print(target)
