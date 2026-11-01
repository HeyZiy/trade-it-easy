# 公共因子分析函数

研究脚本负责股票池、财务可见性、信号时间、入场退出日期和收益构造；本包接收明确的数据，提供计算与展示函数。导入不会联网、运行默认研究或加载 AmazingData。

## 调用

```python
from research.tools.factor_analysis import (
    rank_ic, analyze_factor, analyze_factors, render_report,
)

# 单截面：两个 Series 的索引均为股票代码。
ic = rank_ic(factor_values, endpoint_returns, min_ic_stocks=20)

# 多期：行是信号日期，列是股票；收益的行标签指向该轮信号。
result = analyze_factor(factor_panel, future_return_panel, name='value', groups=5)
print(result.ic_summary)
print(result.periods)       # IC、组收益、覆盖率、名单换手
print(result.memberships)   # 在查看未来收益前固定的分组
render_report(result, 'reports/value.html')

# 多因子及给定权重的合成结果走同一个计算流程。
results = analyze_factors(
    {'value': value_panel, 'growth': growth_panel}, future_return_panel,
    weights={'value': .5, 'growth': .5},
)
render_report(results, 'reports/factors.html')
```

调用方先确定因子方向、尺度和权重；`combine_factors` 默认等权，显式权重原样使用并保留符号。它不自动标准化、补缺、根据历史结果挑权重或剔除缺失因子。

## 职责

| 文件 | 能力 |
|---|---|
| `core.py` | 数据清理、MAD、行业/log市值中性化、Rank IC、分组、覆盖率、名单换手、ICIR/HAC 汇总 |
| `analysis.py` | 对显式因子与未来收益矩阵分析，返回 `FactorResult`；多因子合成复用相同流程 |
| `study.py` | 可调用的聚宽研究流程 `Config/run_study/report`，保留历史选样和收益端点规则 |
| `reporting.py` | `render_report` 消费计算结果，默认不打开浏览器 |
| `sdk.py` | 按需调用官方预处理、共线性、正交化、加权、打分、回归、分层和拥挤度功能 |
| `report_renderer.py` | HTML 渲染器；需要扩展报告时可直接使用 `FactorAnalysisReport` |
| `platform_codegen.py` | 抽取公共函数并组装聚宽单文件，平台脚本无需上传本地包 |

没有独立演示 CLI、自动下载当前股票名单的入口或临时 compare 脚本。

## 计算约定

- Rank IC 调用 `scipy.stats.spearmanr`，按股票代码配对，NaN/Inf 不补值，常数或配对数不足时缺测。
- 分组在查看未来收益前固定；同分不按代码拆组，缺收益的成员继续保留。任一成员缺退出报价，该组该轮收益缺测。
- G1 为低因子组；ICIR 保留符号。普通均值 t 和 Newey-West/HAC 均报告，存在缺测期时不压缩时间间隔来计算 HAC。
- 报告直接使用输入的区间收益；累计曲线遇到缺测停止，不补零接线，也不假设输入一定是日频后年化。
- `membership_turnover` 是等权目标名单的半 L1 差异，不是包含权重漂移和交易约束的实际成交换手。
- 因子诊断不是含费用、滑点、涨跌停和停牌延迟退出的账户回测。

## 可选 SDK 功能

```python
from research.tools.factor_analysis import sdk

correlations = sdk.collinearity(factors)
orthogonal = sdk.orthogonalize(factors, method='symmetric')
scores = sdk.score_stocks(factors, {'value': .5, 'growth': .5})
```

SDK 模块只提供函数调用，不维护第二套自实现兜底算法。IC/收益驱动权重的训练数据及其可见日期由调用方提供。SDK 回归、分层和拥挤度使用官方自己的价格与持有期约定，作为显式扩展调用，不能直接替代指定入场/退出收益的诊断。

`sdk.weight_factors` 保留官方默认的合成结果 Min-Max 归一化；需要保留加权后的尺度时显式传入 `weight_normalization=False`。普通给定权重求和使用 `combine_factors` 即可。

本机 AmazingData 1.1.9 的 `IcAnalysis` 在生成前移收益后再次读取后续行，合成方向样例中观察到收益区间错位。因此公共 IC 使用相同的 SciPy 计算函数，不接入该类。必要的等价性和边界用例在测试中维护。

## 研究与来源

ROE 专用流程在 [roe_quality](../../studies/roe_quality/README_factor_study.md)，价值/营收和低预期研究各自维护数据准备与研究规则。现有 `Config/run_study/report` 可以在项目环境中按函数调用，收益端点仍为下一交易日开盘到下一轮下一交易日开盘；配置与回调的时间约定见 `study.py`。

[原始笔记](../../references/factor_analysis/original_note.md) 和 [原始示例](../../references/factor_analysis/original_code.py.txt) 保留在资料目录，不参与运行。
[官方算法参考](references/factor_analysis_algorithms.md) 是来源说明，具体执行口径以本包接口为准。
