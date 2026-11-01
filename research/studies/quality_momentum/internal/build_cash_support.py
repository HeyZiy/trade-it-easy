"""生成现金支撑聚宽单文件：python -m research.studies.quality_momentum.internal.build_cash_support。"""
import argparse
import hashlib
import json
from pathlib import Path

from research.tools.factor_analysis.platform_codegen import definitions, protect_builtins
from .build_platform import POOL_NAMES

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[2]
TARGET = STUDY / 'experiments/study_v5_cash_support_jq.py'


def render():
    pool = STUDY / 'pool_rotation_v5.py'
    probe = HERE / 'probe_adapter.py'
    core = ROOT / 'research/tools/factor_analysis/core.py'
    codegen = ROOT / 'research/tools/factor_analysis/platform_codegen.py'
    sources = [Path(__file__), HERE / 'build_platform.py', HERE / 'cash_support_adapter.py',
               HERE / 'cash_support.py', HERE / 'probe_statistics.py', probe, pool, core, codegen]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    header = ('# -*- coding: utf-8 -*-\n'
              '# 自动生成：python -m research.studies.quality_momentum.internal.build_cash_support\n'
              '# 整文件粘贴聚宽，天频率；2016-01-04至2026-09-01；不下单。\n'
              'BUILD_SOURCES_SHA256 = %r\n' % json.dumps(hashes, sort_keys=True))
    adapter = (HERE / 'cash_support_adapter.py').read_text(encoding='utf-8').replace(
        "SOURCE_V5_SHA256 = ''", 'SOURCE_V5_SHA256 = %r' % hashes[pool.relative_to(ROOT).as_posix()])
    shared = definitions(probe, ('fixed_groups', 'forward_quotes', 'quote_at', 'resolve_snapshot',
                                 'after_trading_end', '_append_csv', '_write_settings'))
    shared = shared.replace('换手探针', '现金支撑')
    # Shared resolver's optional earnings-response branch is inactive for PROBE=cash_support.
    return protect_builtins(header + adapter + '\n\n' + shared + '\n\n'
                            + definitions(HERE / 'cash_support.py') + '\n\n'
                            + definitions(HERE / 'probe_statistics.py', ('summarize_periods',)) + '\n\n'
                            + definitions(pool, POOL_NAMES) + '\n\n'
                            + definitions(core, ('clean', 'assign_groups', 'summarize_ic'))).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding='utf-8') != source:
            parser.exit(1, '现金支撑聚宽文件需要重新生成\n')
        print('现金支撑聚宽文件与源码一致。')
    else:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(source, encoding='utf-8', newline='\n')
        print(TARGET)


if __name__ == '__main__':
    main()
