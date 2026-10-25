# 个股趋势回踩研究（已收线）

**状态：已退役。** v3 的盈亏比 1.221 触发预登记证伪线；v4 的信号尸检把问题
从“何时买（回踩形态）”改成“同一天买哪只（截面排序）”；v4.1 门控消融与 v5
行业分支没有跑完，也没有完整成绩。本目录只保留版本链和失败证据，不新增实验。

## 版本链

| 文件 | 内容 | 状态 |
|---|---|---|
| [`trend_v1.py`](trend_v1.py) | 严格市场门控、回踩形态与旧卖出规则 | 历史，已被 v3 取代 |
| [`trend_v3.py`](trend_v3.py) | 放宽门控与个股排列，卖侧改 Chandelier 追踪线 | 证伪线触发 |
| [`trend_v4.py`](trend_v4.py) | 删除回踩形态信号，改为截面排序 | 尸检结论版本 |
| [`trend_v4_1_gate_on.py`](trend_v4_1_gate_on.py) / [`trend_v4_1_gate_off.py`](trend_v4_1_gate_off.py) | v4 执行口径修正；市场门控开/关消融 | 暂停，无最终成绩 |
| [`trend_v5_industry.py`](trend_v5_industry.py) | v4.1 去门控 + 行业动量信号层 | 中止，无最终成绩 |

## 保留的文件

- `result_v1.csv` / `result_v3.csv`：被
  [`research/tools/market_regimes/README.md`](../../tools/market_regimes/README.md)
  作为示例输入引用，不能随脚本一起删除。
- `tools/`：v3 信号复刻、行情取数与 MFE 尸检的辅助脚本。
- [`tests/test_platform_trend_versions.py`](../../../tests/test_platform_trend_versions.py)
  仍绑定 v4.1/v5 脚本；如果整线删除，应同时删除该测试并更新 market_regimes
  的示例引用。

生产入口已移除；策略页见 [`strategy/trend_strategy.md`](../../../strategy/trend_strategy.md)，
账户定位见 [`strategy/overview.md`](../../../strategy/overview.md)。
