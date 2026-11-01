# 基本面分析

输入报表、股本和行情，返回按报告期或交易日索引的指标表。指标按盈利、成长、效率、盈余质量、安全、治理、估值、股东和规模九类组织，分类及名称见 `CATEGORY_MAP`。

```python
from research.tools.fundamental_analysis import analyze_fundamentals, fetch_inputs

# 已准备好的三表；SDK 字段，利润表和现金流量表为年内累计值。
result = analyze_fundamentals(code, bs, inc, cf, categories=['profitability', 'growth'])
growth = result['growth']  # 完整时序 DataFrame，不截断、不四舍五入

# 需要联网时显式获取输入；同一组 categories 也传给计算函数。
inputs = fetch_inputs('600001.SH', 20200101, 20261030, categories='size')
size = analyze_fundamentals(**inputs)['size']
```

- `core.py`：TTM、单季值和九类指标计算，不获取数据。
- `analysis.py`：选择类别并返回 `{类别: DataFrame}`；异常直接抛出。
- `data.py`：使用共享 AmazingData 会话准备输入，仅调用时联网。
- `catalog.py`：指标名称和频率；单类函数 `calc_growth` 等也可直接调用。
- [指标说明](references/fundamental_analysis_guide.md)；[上游声明](../references/upstream_notice.md)。

季频结果索引是报告期，日频结果索引是交易日。TTM 按「本期累计＋去年年报－去年同期累计」计算；不可把单季数据直接传入。规模和估值使用未复权收盘价，股本单位是万股，市值单位是元。缺少辅助数据沿用各指标原有 NaN 规则；日频类别要求非空 `kline`。

公式来自官方示例，保持原有计算口径；这不代表已经完成历史可见性审计。全量财报会包含修订值，部分原公式在缺少公告日、派息日时回退到报告期，报表筛选也保留了原有回退行为。历史研究应先在 studies 按研究时点准备可见数据，再检查所选指标的时间映射；不要据此替换生产策略的 PIT 逻辑。
