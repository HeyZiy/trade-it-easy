# -*- coding: utf-8 -*-
"""聚宽研究入口：同一ROE候选池/选股日期，一次计算1日与5日强弱诊断。

Notebook上传本文件、single_factor_test.py、roe_pool.py、roe_rotation_v1_1_2.py。
%run roe_factor_study.py；可先在Config中缩短区间进行数据接口验证。
完整候选池，不取前10只；不下单，不改变原策略文件。
"""
import sys
from pathlib import Path
from dataclasses import asdict
import hashlib
import json

# 平台四文件平铺同目录直接平导入；仓库内引擎在 tools/single_factor，补路径
_REPO_TOOLS = Path(__file__).resolve().parents[2] / 'tools' / 'single_factor'
if _REPO_TOOLS.is_dir():
    sys.path.insert(0, str(_REPO_TOOLS))

from single_factor_test import Config, run_study, report
from roe_pool import ROECandidatePool


START_DATE = '2016-01-04'
END_DATE = '2026-09-01'
SOURCE_PATH = None  # 仓库内自动定位；平台上传后自动用当前目录的v1.1.2
OUTPUT_DIR = 'roe_factor_output'


def main(api=None, source_path=None, start=START_DATE, end=END_DATE, plot=True):
    if api is None:
        try:
            import jqdata as api
        except ImportError:
            import jqdatasdk as api
    source_path = source_path or SOURCE_PATH
    if source_path is None and Path('roe_rotation_v1_1_2.py').exists():
        source_path = 'roe_rotation_v1_1_2.py'
    pool = ROECandidatePool(api, source_path)
    cfg = Config(start=start, end=end, factor='momentum', lookback=1,
                 frequency='trading_days', rotate_every=pool.rotate_every,
                 neutralize=True, output_dir=OUTPUT_DIR)
    tables, snapshots = run_study(cfg, api, pool, lookbacks=(1, 5))
    folder = Path(cfg.output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for mode, table in tables.items():
        table.to_csv(folder / ('periods_%s.csv' % mode), encoding='utf-8-sig')
    metadata = dict(config=asdict(cfg), lookbacks=[1, 5], source=str(pool.source_path),
                    source_sha256=hashlib.sha256(pool.source_path.read_bytes()).hexdigest(),
                    status='diagnostic_only', pool_fail_open_requests=len(pool.audit))
    (folder / 'config.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    snapshots.to_csv(folder / 'snapshots.csv', index=False, encoding='utf-8-sig')
    import pandas as pd
    pd.DataFrame(pool.audit, columns=['signal', 'operation', 'error_type', 'legacy_fail_open']).to_csv(
        folder / 'pool_audit.csv', index=False, encoding='utf-8-sig')
    report(tables, cfg, plot=plot)
    print('ROE source:', pool.source_path)
    print('Pool fail-open requests:', len(pool.audit))
    print('Saved:', folder.resolve())
    return tables, snapshots


if __name__ == '__main__':
    main()
