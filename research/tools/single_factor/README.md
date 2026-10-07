# 单因子测试引擎

`original_note.md` 是用户指定笔记的原文副本，`original_code.py.txt` 是其中完整示例代码。
原文中的“可直接运行”、统计阈值和结果示例不作为本研究结论。
`single_factor_test.py` 是修订后的独立研究脚本，不依赖任何具体策略文件。

**历史研究（2026-10-06）**：

- ROE 系（`roe_factor_study.py` / `roe_factor_study_jq.py` / `roe_pool.py` /
  `pool_ic_probe_v1.py`）在 `research/studies/roe_quality/`，
  工作流见该目录 `README_factor_study.md`；
- 行业动量量价热度（`crowd_factor_study.py`）在
  `research/studies/industry_momentum/`。

**独立扩展**：定价位置 × 经营改善研究在
[`research/studies/value_revenue/`](../../studies/value_revenue/README.md)，复用本工具的
分组/Rank IC/中性化/HAC统计，另做二维交互与多窗口诊断，并组装聚宽单文件。
[低预期与盈利维持](../../studies/expectations/README.md)为另一独立研究。

## 在聚宽研究环境运行

上传 `single_factor_test.py`，在 Notebook 单元格中运行：

```python
from single_factor_test import Config, run_study, report

cfg = Config(
    start='2016-01-04', end='2026-09-01',
    factor='gap',             # 原笔记实际计算的开盘跳空收益，正确命名
    neutralize=True,
)
tables, snapshots = run_study(cfg)
report(tables, cfg)
```

也可以 `%run single_factor_test.py` 使用文件中的默认配置，自动打印、画图并保存 CSV。
手动调用不必导出文件：`tables['raw']`、`tables['neutral']` 可直接查看；
`snapshots` 保存每轮股票、因子、所属组和未来收益，方便追查异常。
本地运行需要已认证的 JQData 账户和对应数据权限，脚本不读取账户信息、不自动认证。

## 参数和扩展

- `factor='gap'`：截至 asof 当天开盘价 / 前一交易日收盘价 − 1。
- `factor='momentum', lookback=1` 或 `5`：截至 asof 的累计涨幅，需要窗口+1根完整日线。
- `factor='alpha191_001'`：调用聚宽研究环境 `jqlib.alpha191.alpha_001`。
  这是官方 Alpha191 公式，不再以跳空收益冒充；本地没有 jqlib 时明确报错。
- `frequency='monthly'`：完整月的最后一个交易日生成信号。
- `frequency='trading_days', rotate_every=20`：从 start 后第一个交易日起每20交易日生成信号。
- `groups=5`：G1因子最低、G5因子最高，不按结果事后反转标签。
- `neutralize=True`：原始与申万一级行业、log总市值回归残差分别测试。
- `mad_scale=None`：默认保留原始排序；设置为3可启用MAD截断，零MAD时跳过截断。
- `min_ic_stocks=20`：少于20个完整因子/收益对时该轮IC缺测，不是收益有效性的门槛。
- `hac_lags=3`：IC均值的Newey-West滞后参数；应预先设定，不按显著性挑参数。

自定义股票池回调：

```python
def my_pool(api, asof, signal):
    # 使用截至asof/信号时已知的信息，返回股票代码列表。
    # 不用未来收益、不按未来是否退市删股票。
    return api.get_index_stocks('000905.XSHG', date=asof)

cfg = Config(factor='momentum', lookback=5, frequency='trading_days')
tables, snapshots = run_study(cfg, pool_provider=my_pool)
```

注意：传入自定义回调时不会再执行默认指数池的ST/停牌/上市年龄筛选。
要研究具体策略，回调应复现该策略在每个信号时点的完整候选池；换成宽基指数
不能解释其结果。策略的数据截止时点、信号日状态与筛选规则应逐项对齐。

## 相对原文的修正

1. 修复语法；用 `get_all_securities(..., date=asof)` 获取历史上市信息，
   `get_price(..., fields=['paused'])` 获取停牌状态，`get_industry(..., date=asof)` 获取历史行业。
   不使用不存在的 `get_extras('list_date'/'suspended'/'sw1')`。
2. 市值查询显式包含股票代码，用代码对齐；使用log总市值，缺失控制变量不填0。
3. 因子和控制变量截至信号日T−1；理论入场T+1开盘，退出下一轮信号T+1开盘。
   不是聚宽策略9:31撮合价格。保留窗口完整且退出不超过end的区间，不把月中结束当月末。
4. 所有行情明确 `fq='post'`，统一复权口径；因子涨幅不填补停牌日，不跨缺失日凑窗口。
5. 在查看未来收益前固定分组；同分不按股票代码人为拆分，无法形成全部组时缺测。
6. 因子、收益和控制变量不以0补缺。某组任何成员退出报价缺失，该组整轮收益缺测；
   IC使用完整配对样本，同时报告覆盖率，它仍可能受非随机缺失影响。
7. ICIR保留正负号。普通t、Newey-West均值t、年度IC分别报告；
   IC存在缺测期时不压缩时间间隔计算HAC，不套“0.02/0.2即合格”的硬标准。
8. 分组收益和基准使用相同入场、退出日期，收益曲线标在退出日。
   累计曲线一旦缺测便停止，避免补0制造连续净值。
9. 输出每组有效期数、覆盖率、最高减最低组收益差和等权目标名单换手；
   换手不含持有期权重漂移，不能当作实际成交换手。

## 如何解读与限制

本脚本测的是特征与后续报价收益的关系，不是可成交、扣费后的策略收益。
未模拟涨跌停、成交量约束、停牌延迟退出、滑点、税费和公司退市结算。
入场/退出停牌或无报价会缺测，不删除这些股票后悄悄重算组合。
不为了把曲线接起来而将缺失收益设为0或−100%；需要逐笔原因审计和适当的损失/结算口径。
行业数据历史覆盖、财务数据修订等仍受数据供应商限制。

完整候选池的IC可能不强而头部有效；也可能IC较好但扣费无法交易。
看平均分组差异、按年稳定性、样本覆盖，再回到策略撮合回测，不能仅看最终曲线严格单调。
在真正后续验证中预先固定因子方向、窗口和调仓规则，不以试到t>2为停止条件。

## 核对依据

- 聚宽官方SDK：<https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py>
- 聚宽官方API/Alpha191：<https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf>
- Newey-West方法说明：<https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html>

本地测试：`python -m pytest tests/test_single_factor_research.py -q`。
