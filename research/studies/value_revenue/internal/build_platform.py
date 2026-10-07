"""将共用工具、研究核心与平台适配层组装为聚宽可粘贴的单文件。"""
from research.tools.single_factor.platform_codegen import definitions, protect_builtins
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
TOOL_NAMES = ('clean', 'neutralize', 'assign_groups', 'evaluate_period',
              'membership_turnover', 'summarize_ic')






def render():
    header = '''# -*- coding: utf-8 -*-
# 定价位置 × 经营改善：独立信号诊断，聚宽单文件，不下单。
# 自动组装源：tools/single_factor/single_factor_test.py + value_revenue/internal/core.py
#              + value_revenue/internal/platform_adapter.py；修改源后运行build_platform.py。
# 聚宽设置：2016-01-04～2026-09-30，频率“天”，基准中证800，资金任意。
# 主检验60交易日；20/120日是预先固定的辅助窗口，每20日冻结一次信号。
# 请看[预期差诊断汇总]及CSV；空账户的收益/夏普不是研究结果。
import numpy as np
import pandas as pd
import builtins as _python_builtins

'''
    source = (header + definitions(ROOT / 'research/tools/single_factor/single_factor_test.py', TOOL_NAMES)
              + '\n\n' + definitions(HERE / 'core.py') + '\n\n'
              + (HERE / 'platform_adapter.py').read_text(encoding='utf-8'))
    return protect_builtins(source)


if __name__ == '__main__':
    destination = HERE.parent / 'study_jq.py'
    destination.write_text(render(), encoding='utf-8')
    print(destination)
