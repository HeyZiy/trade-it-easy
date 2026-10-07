# 价值因子 × 营收改善

**结论：EP有历史收益关联；营收加速没有显示稳定的额外贡献，组合及交互暂不进入策略开发。** 这是价值与经营改善研究，未检验原文的业绩天花板。

从这页开始。想运行实验，只看[study_jq.py](study_jq.py)；想复查结果，再看下面链接。`internal/`是维护和审计工具，日常无需阅读。

## 实验测什么

问题：营收同比加速的后续收益优势，是否在低估值公司中更强？

- 价值指标：EP=1/PE(TTM)，只取正PE。
- 改善指标：最新已披露单季度营收同比−上一季度营收同比，单位百分点；两季度须连续。
- 比较单因子、等权中心秩相加、秩乘积以及高低EP×强弱改善四格。
- 原始因子与行业/log市值残差因子同时报告。回归控制行业、log市值、120日动量及当前营收增长水平，保留两因子主效应及交互。

样本为历史主板，上市至少365自然日，T−1非ST且未停牌，正PE/市值；财报距T−1不超过240自然日，要求行业和121根完整正收盘价。每20交易日冻结一次，T−1取数，次日开盘入场，观察20/60/120交易日后开盘收益，以60日为主。到期重查动态前复权入场开盘。

## 全期结果

2016-01-04～2026-09-30，131期信号；60日成熟128期。主回归跨期系数的HAC t：

| 回归项 | 原始因子 | 行业/市值残差因子 |
|---|---:|---:|
| EP | 2.37 | 2.43 |
| 营收加速 | 1.85 | 1.38 |
| EP×营收加速 | 1.22 | 0.63 |

60日“等权相加IC−EP IC”为−0.00526/−0.00359，HAC t为−0.80/−0.56，没有显示优于EP，也不足以断言组合显著更差。此处系数、IC及报价收益不是可交易组合收益。

第一轮的严格四格要求全部成员有报价，60日仅5/128期四格共同完整，不能据此判断交互成败。窗口缺报价率为20日0.502%、60日0.823%、120日0.979%；缺报价保留成员，不补零。

原始CSV重算一致：265,952条快照、776,198条收益记录、766条窗口/口径记录。行情权限内近期58,521条有效股票窗口的前/后复权及原动态取整口径一致；103条缺测对应98条停牌及5条证券结束后的端点。此检查来自同一供应商另一环境，未独立核验完整历史报价或财务修订。

## 怎么运行、看哪些文件

把[study_jq.py](study_jq.py)整文件粘贴到聚宽策略回测，频率选“天”，使用上述日期。无需上传其他代码；不下单，看日志及CSV，空账户收益/夏普不是实验结果。

| 位置 | 用途 |
|---|---|
| [data/expectations_v1_summary.csv](data/expectations_v1_summary.csv) | 原平台全期/年度汇总 |
| [data/expectations_v1_metadata.json](data/expectations_v1_metadata.json) | 原实验参数和完成状态 |
| `data/`其他CSV | 原始快照、逐股收益、逐期统计及样本审计 |
| [results/exports_audit/statistics.csv](results/exports_audit/statistics.csv) | 本地重算与增量比较 |
| [results/exports_audit/manifest.json](results/exports_audit/manifest.json) | 原始输入哈希及核验计数 |
| [results/quote_audit/2025-06-28_2026-07-05/manifest.json](results/quote_audit/2025-06-28_2026-07-05/manifest.json) | 实际行情核查范围及证据 |

数据保留历史`expectations_v1_`前缀，内容和哈希不变；`data/`仅本地留存。两个因子相乘会同时奖励“低价值、弱改善”，不能当作只买便宜且改善股票的分数。历史区间已被观察，年度切片不称为独立样本外。统计未含费用、滑点及成交限制，缺期HAC不以普通t替代。

[另一独立研究：低预期与盈利维持](../expectations/README.md)。

<details>
<summary>维护命令（通常无需阅读）</summary>

```powershell
python -m research.studies.value_revenue.internal.build_platform
python -m research.studies.value_revenue.internal.analyze_exports
python -m research.studies.value_revenue.internal.audit_quotes --start 2025-06-28 --end 2026-07-05 --offline
python -m pytest tests/test_value_revenue_factor_study.py tests/test_value_revenue_quote_audit.py -q
```

平台内置名称可能被numpy覆盖；生成代码统一使用builtins。回测引擎API由策略全局注入，不从jqdata模块属性调用。
</details>
