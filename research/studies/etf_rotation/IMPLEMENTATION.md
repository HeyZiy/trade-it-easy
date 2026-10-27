# ETF跨资产轮动：实现与复现

[策略主张](README.md) · [实验记录](EXPERIMENTS.md)

成员名单、评分、退出与仓位规则以[生产策略页](../../../strategy/etf_rotation.md)为准。本目录维护15只跨资产基线与17只能源扩充版；行业材料统一归入[行业研究](../industry_momentum/README.md)。

## 当前聚宽入口

每个入口都是可直接粘贴到聚宽新建策略的完整单文件。统一设置：**2016-01-01至2026-10-01、初始10万元、频率每天、买卖佣金各万二且最低5元、零滑点**，计划10:40决策。历史不足25根已完成日线时跳过，不回填上市前指数或商品行情。

| 入口 | 成员版本 | 用途 |
| --- | --- | --- |
| [unified_coverage.py](unified_coverage.py) | 固定15只 | 跨资产平台主入口，已恢复原15只名单及顺序 |
| [unified_energy17.py](unified_energy17.py) | 固定17只 | 原15只末尾依次追加501018南方原油、159981建信能源化工期货 |
| [study_etf_fit_jq.py](study_etf_fit_jq.py) | 固定15只 | 保持主入口交易逻辑，增加fit_signals.csv、fit_market.csv、fit_metadata.json观测导出 |

17只版只检验两项能源暴露的整体扩充。501018为QDII-FOF-LOF，159981为商品期货ETF；原油产品按实际场内基金价格参与，含自身跟踪、汇率与折溢价路径，不用油价代替。15/17版比较必须使用相同日期、资金、费用与频率，从头回测。平台结果见[实验记录](EXPERIMENTS.md)，单只贡献不能由两池收益差确定。

旧入口[unified_hold_qualified.py](unified_hold_qualified.py)为11只池、[same_pool_baseline.py](same_pool_baseline.py)为8只池，仅保留历史版本；不能与15/17只作同池比较。

## 副图与统计

单边换手为当天实际买卖成交金额之和除以2倍前次记录的收盘净值；首日以初始资金为分母，热更新以重置时净值为分母，不含佣金，无成交为0。首次全仓买入约50%，完整卖出再全仓买入约100%，部分成交按实际数量计。

实际仓位为收盘实际持仓市值除以收盘账户净值，负现金时可能略高于100%。取数失败显示缺失值并记日志，不伪装成0。年度收益与持仓统计保留；热更新后的数据不能拼接成一份完整实验。

## 本地分析入口

从仓库根运行，保留原15只观测和持仓研究；缺价、停牌、零量日不跳过、不填充。

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `python research/studies/etf_rotation/analyze_positions.py` | position.csv，只读 | 持仓归因JSON打印到标准输出；初始资金10万、万二最低5元 |
| `python research/studies/etf_rotation/study_etf_fit.py` | position.csv | results/fit_*.csv与fit_method.json |
| `python research/studies/etf_rotation/study_etf_fit_forward.py` | 本目录三个fit_*导出 | results/fit_forward_*.csv |
| `python research/studies/etf_rotation/study_etf_fit_control.py` | 同上 | results/fit_control_*.csv与口径JSON |

行业与跨资产的既有六账户导出、比较结果及行业数据面板已迁入industry_momentum；其复核说明见[行业材料入口](../industry_momentum/IMPLEMENTATION.md)，不作为跨资产策略的运行步骤。

## 生产入口

每日模拟账户入口是仓库根[etf_rotation.py](../../../etf_rotation.py)，判定核是[src/etf/rotation.py](../../../src/etf/rotation.py)。生产继续使用固定15只池，研究17只扩充不自动进入生产。行业动量有独立策略、预算与台账。

本地模拟与平台的实时价回退、复权和费用记账差异见[生产策略页](../../../strategy/etf_rotation.md)。复现记录日期、资金、频率、费用与代码版本；分钟结果与已有日频结果分别登记。

## 已完成的分数上限对照

`python research/studies/etf_rotation/study_score_cap.py`只读取本目录已有fit_*和position.csv，结果写入results/score_cap/。基线须逐日复现平台持仓后才允许解读；当前结果不支持放宽旧持仓的分数上限，生产规则保留。

[study_score_cap_jq.py](study_score_cap_jq.py)为可选聚宽双账户复核：2016-01-01至2026-10-01、初始**20万元**（两组各10万元）、每天、万二最低5元、零滑点、10:40。导出score_cap_nav.csv、score_cap_signals.csv、score_cap_decisions.csv；主图两组合计不能作单组绩效。尚未运行，无新平台结果。

本地执行已补充分红历史价调整、新成交佣金和实时价缺失暂停；分红应收/到账日期及旧流水兼容口径见生产策略页。
