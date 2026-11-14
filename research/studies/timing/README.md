# 择时能否改善长期持有体验？

固定规则的择时能否改善持有体验，以及为此付出多少收益。

## 从这里看

| 想看什么                                      | 入口                                                                                    |
|-----------------------------------------------|-----------------------------------------------------------------------------------------|
| 当前结论                                      | [研究结论](docs/FINDINGS.md)                                                            |
| 数据、规则、指标定义                          | [统一口径](docs/METHOD.md)                                                              |
| 一级：12只ETF、代表规则横向比较               | [结果](results/level1/summary.md) · [脚本](run_level1.py)                               |
| 二级：恒生科技、固定参数组比较                | [结果](results/level2/513180/summary.md) · [脚本](run_level2.py)                        |
| 附加：唐奇安与ATR通道的族扫描（排除参数侥幸） | [结论](docs/FINDINGS.md) · [唐奇安](common/donchian.py) · [ATR通道](common/atr_channel.py) |
| 附加：网格中枢重定频率是否改变结论            | [结论](docs/FINDINGS.md) · [脚本](experiments/grid_reanchor.py)                         |
| 创业板：46%回撤如何形成，与恒生科技同时间比较 | [诊断](results/chi_next_drawdown/summary.md) · [脚本](experiments/chi_next_drawdown.py) |
| 各规则的净值、回撤与仓位曲线                  | [脚本](plot_curves.py)，输出到各层 `plots/`（不入库）                                   |

## 当前结论

恒生科技513180在2022-03-18至2026-09-30的样本中，唐奇安20/10和多组均线ATR通道同时改善了长拿的收益、回撤与最长水下时间；这是4.5年短样本上的单点结果。

跨12只ETF的族扫描只支持回撤与水下时间的改善：3年滚动窗口中唐奇安族的回撤改善比例为76%–100%、ATR通道族为85%–96%，而两族的超额年化中位数几乎全为负。择时买到的是回撤缓解，不是收益。

一级用于发现不同标的的表现差异，二级用于并列比较预先固定的代表参数，附加层用于排除参数侥幸；逐标的给结论。完整数字与适用范围见[研究结论](docs/FINDINGS.md)。

## 运行

从仓库根目录运行：

```text
python research/studies/timing/fetch_data.py
python research/studies/timing/run_level1.py
python research/studies/timing/run_level2.py
python -m research.studies.timing.common.donchian
python -m research.studies.timing.common.atr_channel
python -m research.studies.timing.experiments.grid_reanchor
python -m research.studies.timing.plot_curves
```

已有数据时，直接运行一级或二级。一级读取[assets.py](assets.py)中的全部12只ETF；二级目前只运行恒生科技，固定规则在run_level2.py的PRESETS中。绘图只读已生成的 daily 曲线，可用 `--layer`、`--code` 缩小范围。

## 目录

```text
timing/
├─ README.md           研究问题和阅读入口
├─ assets.py           唯一标的配置
├─ fetch_data.py       数据准备
├─ run_level1.py       一级：多标的固定规则
├─ run_level2.py       二级：单标的固定参数
├─ plot_curves.py      daily 曲线画成交互 HTML
├─ common/             共享账户、信号和体验指标；donchian 与 atr_channel 自带族扫描入口
├─ experiments/        网格频率对照、创业板回撤诊断
├─ docs/
│  ├─ FINDINGS.md      当前结论
│  └─ METHOD.md        统一口径
└─ results/            各层原始输出与 plots/；csv、json 与 html 均不入库
   ├─ level1/          一级当前结果
   ├─ level2/513180/   二级当前结果
   ├─ chi_next_drawdown/ 创业板回撤诊断
   └─ supplementary/   族扫描与网格频率对照
```

行情与 PE 数据在 `research/data/etf_hfq_em/` 与 `research/data/index_pe_lg/`，
CSV 不入库，口径与重生成见各目录 README 与 manifest。
