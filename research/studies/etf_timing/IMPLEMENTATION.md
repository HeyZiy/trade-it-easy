# 单 ETF 时序研究：实现与复现

[研究问题与判据](README.md)。两个聚宽入口对应同一套时序合成规则
（score 门进出 + 跳水当日清仓 + 温度闸 + 波目仓位），只差池定义：

| 用途 | 入口 | 池 |
| --- | --- | --- |
| 固定行业池版 | [jq_timing_industry22.py](jq_timing_industry22.py) | 固定 22 只行业 ETF（同 etf_rotation 名单），温度闸全覆盖 |
| 动态池版 | [jq_timing_dynamic_pool.py](jq_timing_dynamic_pool.py) | lM 规则动态池（20 日重建六关，相关性 0.90 研究基线），温度表仅覆盖 22 只，表外成员默认无闸（`STRICT_TEMP=True` 切 fail-closed） |

两版由 [make_jq_scripts.py](make_jq_scripts.py) 从同一模板生成（改模板后重跑生成器，
不手改产物）。聚宽设置：**2016-01-01~2026-10-01，初始 100000，频率每天**，基准
000300，无滑点，万二单边无最低佣金（对齐本地研究口径）。14:55 决策、现价成交。

## 温度闸的嵌入方式

平台无法复算温度（需申万时点成分×全成分股成交额面板），以"temp_pct ≥0.9 的
交易日区间表"离线嵌入：源 `etf_rotation/_cache/temperature_panel.pkl` → 压缩为
`_cache/temp_hot_ranges.py`（734 段区间）→ 生成器注入两个脚本的 `TEMP_HOT`。
区间端点含端；表冻结至 2026-09-30，跑更长窗口或实盘前须重建
（重建入口 `etf_rotation/temperature_build.py`）。温度口径与 etf_rotation 线全部
温度实验一致（20 日均值比的自身 expanding 分位，min_obs=120）。

## 与相邻策略的关系

- 不含 lM v3_3_2 的 -8% 限亏/保本损：属行业动量线待重跑证据，若加入走单变量；
- 不含跨资产 15 池：本线只答"行业 ETF 的时序买卖点"；跨资产引擎见 etf_rotation；
- 该合成四件各自单变量过检，作为完整账户**尚无任何读数**——两个脚本的首次
  平台运行即整体首跑，对照锚（本地重放口径）：纯行业22无闸 5.33%/-35.85%、
  22 等权 6.78%、原15跨资产轮动 36.8%/-19.0%。

结果跑出后登记回 [README](README.md)，不写进本文件。
