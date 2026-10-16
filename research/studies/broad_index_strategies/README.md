# 宽基ETF：择时能否改善长期持有体验？

以长期配置为基础，先量出长拿的收益、回撤和等待，再比较固定规则能否减少深亏与漫长水下时间，同时尽量保留收益。

## 从这里看

| 想看什么 | 入口 |
|---|---|
| 当前结论 | [研究结论](docs/FINDINGS.md) |
| 数据、规则、指标定义 | [统一口径](docs/METHOD.md) |
| 一级：12只ETF、代表规则横向比较 | [结果](results/level1/summary.md) · [脚本](run_level1.py) |
| 二级：恒生科技、固定参数组比较 | [结果](results/level2/513180/summary.md) · [脚本](run_level2.py) |
| 创业板：46%回撤如何形成，与恒生科技同时间比较 | [诊断](results/chi_next_drawdown/summary.md) · [脚本](experiments/chi_next_drawdown.py) |
| 聚宽：恒生科技三组账户核对 | [运行说明](docs/JOINQUANT.md) · [可粘贴脚本](study_jq.py) |

## 当前结论

恒生科技513180在2022-03-18至2026-09-30的样本中，唐奇安20/10和多组均线ATR通道改善了长拿的收益、回撤与最长水下时间。单均线和其他唐奇安设置表现较弱，不能把结论扩大为所有趋势规则都有效。

一级用于发现不同标的的表现差异，二级用于并列比较预先固定的代表参数；逐标的给结论，不按全样本最高年化挑参数。完整数字与适用范围见[研究结论](docs/FINDINGS.md)。

## 运行

从仓库根目录运行：

```text
python research/studies/broad_index_strategies/fetch_data.py
python research/studies/broad_index_strategies/run_level1.py
python research/studies/broad_index_strategies/run_level2.py
```

已有缓存时，直接运行一级或二级。一级读取[assets.py](assets.py)中的全部12只ETF；二级目前只运行恒生科技，固定规则在run_level2.py的PRESETS中。

## 目录

```text
broad_index_strategies/
├─ README.md           研究问题和阅读入口
├─ assets.py           唯一标的配置
├─ fetch_data.py       数据准备
├─ run_level1.py       一级：多标的固定规则
├─ run_level2.py       二级：单标的固定参数
├─ study_jq.py         聚宽：长拿、唐奇安20/10、ATR主规则，分别回测
├─ common/             共享账户、信号和体验指标
├─ experiments/        网格频率对照、创业板回撤诊断
├─ docs/
│  ├─ FINDINGS.md      当前结论
│  ├─ METHOD.md        统一口径
│  └─ JOINQUANT.md     聚宽运行与导出说明
├─ results/
│  ├─ level1/          一级当前结果
│  ├─ level2/513180/    二级当前结果
│  └─ chi_next_drawdown/ 创业板回撤诊断
└─ _cache/             行情与PE缓存
```
