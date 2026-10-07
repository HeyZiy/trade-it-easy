"""组装直接粘贴到聚宽策略回测的独立单文件。"""
import ast
from pathlib import Path

from research.tools.single_factor.platform_codegen import definitions, protect_builtins


HERE = Path(__file__).resolve().parent


def render():
    header = '''# -*- coding: utf-8 -*-
# 低预期＋盈利维持：独立聚宽回测诊断，直接粘贴整文件，无需上传旧数据。
# 聚宽「策略回测」：频率“天”，2016-01-04起；结束日选数据已覆盖的日期。
# 资金任意，不下单；研究结果看日志、record与导出CSV，空账户收益不是结果。
# 主60日、辅助20/120日；每20交易日冻结一次，只使用T-1已披露信息。
# 源：internal/中的财务统计、平台适配实现及共用工具。
import builtins as _python_builtins
import numpy as np
import pandas as pd

'''
    pilot = (HERE / 'low_expectations.py').read_text(encoding='utf-8')
    constants = [ast.get_source_segment(pilot, node) for node in ast.parse(pilot).body
                 if isinstance(node, ast.Assign)
                 and all(target.id != 'INPUT_PREFIX' for target in node.targets if isinstance(target, ast.Name))]
    source = (header + '\n'.join(constants) + '\n\n'
              + definitions(HERE.parents[2] / 'tools/single_factor/single_factor_test.py', ('clean', 'summarize_ic'))
              + '\n\n' + definitions(HERE / 'core.py', ('report_date', 'centered_rank'))
              + '\n\n' + definitions(HERE / 'low_expectations.py', (
                  'prepare_profit_features', 'freeze_profit_groups', 'stable_regression',
                  'evaluate_profit_period', 'summarize_profit_periods'))
              + '\n\n' + (HERE / 'low_expectations_backtest_adapter.py').read_text(encoding='utf-8'))
    return protect_builtins(source)


if __name__ == '__main__':
    target = HERE.parent / 'study_jq.py'
    target.write_text(render(), encoding='utf-8')
    print(target)
