"""从研究适配代码与公共计算源码生成聚宽单文件。

运行：python -m research.studies.quality_momentum.internal.build_platform [--check]
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from research.tools.factor_analysis.platform_codegen import definitions, protect_builtins


HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[2]
CODEGEN = ROOT / 'research/tools/factor_analysis/platform_codegen.py'
FACTOR_CORE = ROOT / 'research/tools/factor_analysis/core.py'
POOL = STUDY / 'pool_rotation_v5.py'
POOL_NAMES = ('PoolDataError', '_evaluate_pool', '_annualized_vol', 'rank_pool',
              'build_pool', '_prev_trade_day', '_require_columns', '_report_date',
              '_fetch_roe_ttm', '_history_closes', '_held')
FACTOR_NAMES = ('clean', 'assign_groups', 'summarize_ic')
RANK_POOL_NAMES = ('_evaluate_pool', 'rank_pool', 'select_targets', '_annualized_vol',
                   '_history_closes', '_fetch_roe_ttm', '_sw_industries', '_quote_block',
                   '_affordable_shares')
EXPERIMENTS = {
    'rank_control': ('rank_control_adapter.py', 'account_statistics.py',
                     ('account_metrics', 'yearly_metrics', 'comparison_rows'),
                     'experiments/study_v5_rank_control_jq.py'),
    'probe': ('probe_adapter.py', 'probe_statistics.py',
              ('summarize_periods', 'response_summary'), 'experiments/study_v5_turnover_probe_jq.py'),
}


def source_hashes(experiment):
    adapter, statistics, _, _ = EXPERIMENTS[experiment]
    sources = [Path(__file__).resolve(), CODEGEN, POOL, HERE / adapter, HERE / statistics]
    if experiment == 'probe':
        sources.append(FACTOR_CORE)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources}


def render(experiment):
    adapter, statistics, names, _ = EXPERIMENTS[experiment]
    hashes = json.dumps(source_hashes(experiment), sort_keys=True, separators=(',', ':'))
    header = (
        '# -*- coding: utf-8 -*-\n'
        '# 自动生成：修改 quality_momentum/internal 中的源码，再运行\n'
        '# python -m research.studies.quality_momentum.internal.build_platform\n'
        '# 整文件粘贴到聚宽；本文件无需导入本地 research 包。\n'
        f'BUILD_SOURCES_SHA256 = {hashes!r}\n\n'
    )
    source = (HERE / adapter).read_text(encoding='utf-8')
    source = re.sub(r"SOURCE_V5_SHA256 = '[0-9a-f]+'",
                    "SOURCE_V5_SHA256 = '%s'" % hashlib.sha256(POOL.read_bytes()).hexdigest(), source)
    source += '\n\n' + definitions(HERE / statistics, names)
    if experiment == 'probe':
        source += '\n\n' + definitions(POOL, POOL_NAMES)
        source += '\n\n' + definitions(FACTOR_CORE, FACTOR_NAMES)
    else:
        source += '\n\n' + definitions(POOL, RANK_POOL_NAMES)
    return protect_builtins(header + source).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', choices=['all', *EXPERIMENTS], default='all')
    parser.add_argument('--check', action='store_true', help='只核对生成文件是否与源码一致')
    args = parser.parse_args()
    selected = EXPERIMENTS if args.experiment == 'all' else [args.experiment]
    stale = []
    for experiment in selected:
        target = STUDY / EXPERIMENTS[experiment][3]
        expected = render(experiment)
        if args.check:
            if not target.exists() or target.read_text(encoding='utf-8') != expected:
                stale.append(target.name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding='utf-8', newline='\n')
            print(target)
    if stale:
        parser.exit(1, '聚宽文件需要重新生成：' + ', '.join(stale) + '\n')
    if args.check:
        print('聚宽生成文件与源码一致。')


if __name__ == '__main__':
    main()
