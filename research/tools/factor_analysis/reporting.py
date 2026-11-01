"""渲染已计算的统一诊断结果；不重新选样、计算收益或调用 SDK。"""
from html import escape
from pathlib import Path

import pandas as pd

from .analysis import FactorResult
from .report_renderer import FactorAnalysisReport


def render_report(results, output_path, *, title='因子诊断报告', open_browser=False):
    """单因子或多因子结果写入 HTML；默认不打开浏览器。"""
    if isinstance(results, FactorResult):
        results = {results.name: results}
    if not results:
        raise ValueError('至少提供一个诊断结果')
    report = FactorAnalysisReport('factor_analysis', escape(title))
    report.add_definition_section(
        '<p>输入为调用方给定的因子与入场到退出收益；G1 为低因子组。'
        '分组不根据未来收益缺失重新选择成员。收益为报价诊断，未模拟费用与成交约束。</p>')
    summaries = pd.DataFrame({name: result.ic_summary for name, result in results.items()})
    report.sections.append('<section id="ic-analysis"><h2>IC 统计</h2><div class="card">'
                           + summaries.to_html(escape=True, na_rep='缺测') + '</div></section>')
    charts, details = [], []
    for name, result in results.items():
        periods = result.periods
        cols = [f'G{i}' for i in range(1, result.groups + 1)]
        curves = (1 + periods[cols]).cumprod(skipna=False) - 1
        charts.append(report._echarts_line(curves, escape(name) + ' 分组累计报价收益',
                                           '收益', cols))
        details.append('<h3>' + escape(name) + '</h3>'
                       + periods.to_html(escape=True, na_rep='缺测'))
    report.sections.append('<section id="stratification-analysis"><h2>分组收益</h2>'
                           + ''.join(charts) + '</section>')
    report.sections.append('<section id="period-details"><h2>逐期覆盖与统计</h2>'
                           + ''.join(details) + '</section>')
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return report.generate(str(path), open_browser=open_browser)
