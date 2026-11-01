"""聚宽回调生成真实格式 CSV，再从磁盘复算；不代表平台历史收益证据。"""
import ast
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_quality_pool_v2 import world  # noqa: E402,F401
from test_platform_quality_momentum_rank_control import control, run_control  # noqa: E402
from test_platform_quality_momentum_turnover_probe import probe, response_probe, run  # noqa: E402
from research.studies.quality_momentum.internal import build_platform  # noqa: E402
from research.studies.quality_momentum.internal.analyze_exports import analyze_exports  # noqa: E402


def test_generated_scripts_are_current_and_need_no_repository_imports():
    for name, config in build_platform.EXPERIMENTS.items():
        source = build_platform.render(name)
        assert source == (build_platform.STUDY / config[3]).read_text(encoding='utf-8')
        imports = [node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
        assert not any(module and module.startswith(('research', 'data_provider')) for module in imports)


@pytest.fixture(params=['rank_control', 'momentum_only', 'turnover', 'earnings_response'])
def platform_export(world, tmp_path, request):
    mode = request.param
    if mode in ('rank_control', 'momentum_only'):
        stub, module, _, calendar = control(world, mode='compare' if mode == 'rank_control' else mode)
        run_control(stub, calendar[270:273])
    else:
        stub, module, _, calendar = (response_probe if mode == 'earnings_response' else probe)(world)
        run(stub, module, calendar[270:336])
    module.on_strategy_end(stub.ctx)
    for name, content in stub.exports.items():
        (tmp_path / name).write_text(content, encoding='utf-8')
    return mode, tmp_path / module.OUTPUT_PREFIX


def test_platform_exports_recompute_locally_and_reject_changed_observations(platform_export, tmp_path):
    mode, prefix = platform_export
    before = {path: path.read_bytes() for path in prefix.parent.glob(prefix.name + '_*.csv')}
    result = analyze_exports(prefix, tmp_path / 'analysis')
    assert not result['summary'].empty
    metadata = json.loads((tmp_path / 'analysis/verification.json').read_text(encoding='utf-8'))
    assert metadata['platform_summary_matches']
    assert metadata['mode'] == ('rank_control' if mode == 'momentum_only' else mode)
    assert metadata['platform_build_sources_sha256']
    assert all(path.read_bytes() == content for path, content in before.items())
    if mode in ('rank_control', 'momentum_only'):
        assert result['summary'].return_pct.lt(0).all()  # 首日费用保留在账户结果内。
        path = prefix.with_name(prefix.name + '_nav.csv')
        frame = pd.read_csv(path)
        frame.loc[0, 'equity'] *= .9
    else:
        path = prefix.with_name(prefix.name + '_periods.csv')
        frame = pd.read_csv(path)
        field = 'value' if mode == 'earnings_response' else 'return_mean'
        row = frame.index[frame[field].notna()][0]
        frame.loc[row, field] += .1
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match='不一致'):
        analyze_exports(prefix, tmp_path / 'changed')
    assert not (tmp_path / 'changed/verification.json').exists()
