# 行业ETF：转入材料与复核入口

[行业研究主线](README.md) · [既有比较结果](EXPERIMENTS.md)

2026-10-27将行业相关材料从etf_rotation合并至本目录。37只混排及其闸条件研究已结束；下表用于核对既有结果，不是继续实验的待办，也不是现行lM规则。lM研究基线与生产入口仍以[README](README.md)为准。

## 转入材料

| 材料 | 角色 |
| --- | --- |
| [unified_industry22.py](unified_industry22.py) | 用户固定22只行业名单的单仓排名对照；不同于lM动态池、多仓、自身信号退出 |
| [mixed37_reference.py](mixed37_reference.py) | 已结束的37只混排源代码，仅核对旧持仓和信号 |
| [study_pool_controls_jq.py](study_pool_controls_jq.py) · [analyze_pool_controls.py](analyze_pool_controls.py) | 已完成的15/37只六账户实验与导出核算 |
| [study_entry_volatility.py](study_entry_volatility.py) · [study_switch_correlation.py](study_switch_correlation.py) | 原15只与混合池的历史诊断 |
| [temperature_build.py](temperature_build.py) · [sponsorship_build.py](sponsorship_build.py) · [valuation_build.py](valuation_build.py) | 行业成交额、盈利广度与估值数据面板；温度面板也供etf_timing读取 |
| [study_temperature_gauge.py](study_temperature_gauge.py) · [study_sponsorship.py](study_sponsorship.py) · [study_classic_factors.py](study_classic_factors.py) · [study_valuation.py](study_valuation.py) | 已有行业入场事件的条件诊断，使用旧混合账户选出的事件 |
| [temperature_gate_replay.py](temperature_gate_replay.py) · [temperature_breadth_replay.py](temperature_breadth_replay.py) · [temperature_industry22_replay.py](temperature_industry22_replay.py) | 已结束的温度、双闸与纯行业账户重放 |

原始pool_control_*.csv、position_mixed37.csv及相应results/子目录随脚本移动；_cache/中的pool37价格快照、温度、利润表与股本面板保持原内容。原15只position.csv和fit_*导出继续位于../etf_rotation/，诊断脚本显式只读引用，避免复制出另一份基线。

## 既有运行口径

六账户平台设置为2016-01-01至2026-10-01、初始60万元分成六份各10万元、每天频率、万二且最低5元、零滑点、10:40决策。平台主图为六组合计，不能作为任一子账户绩效。六组净值与决策以原始导出为准。

本地重放使用真实逐日评分和决策价，不重算排名；价格位于乘法复权空间，万二最低5元、100份整手，温度或广度缺失按旧实验定义处理。与平台有分红再投资、成交量约束等差异；原15只锚点保留平台37.22%与本地36.80%两个口径，不混用。

## 路径与依赖

脚本命令前缀现为`python research/studies/industry_momentum/`；从仓库根运行。数据面板首次取数会访问供应商，整理本身不取数、不重跑账户。

时序研究[etf_timing](../etf_timing/README.md)读取本目录_cache/pool37_event_adjusted_20260930/market.csv与_cache/temperature_panel.pkl；跨资产生产不依赖这些行业面板。既有结果文件中的来源路径和哈希记录原实验状态，迁移未改写历史数据。
