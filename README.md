# trade-it-easy

个人 A 股交易策略系统：策略研究、信号生产、尾盘执行与名义台账记账一体化。

## 策略地图

| 策略          | 入口                                                   |
| ----------- | ---------------------------------------------------- |
| ETF 长期配置    | `etf_observe.py`（周一尾盘自动调仓批次）                         |
| 行业动量轮动      | `industry_momentum.py`                               |
| ETF 跨资产动量轮动  | `etf_rotation.py`                                    |
| 质量池轮动       | `quality_pool.py`（09:30一次完成信号、模拟成交与报告） |
| 价格循环（Cycle） | —                                                    |

趋势回踩（pullback）已退役，入口脚本不再存在；环境快照随之休眠（唯一消费方
消失），模块与 `data/environment.json` 结构保留，见 strategy/overview\.md。

各策略的定位、假设与不可混用边界见 [strategy/overview.md](strategy/overview.md)。

## 运行环境

- Python 3.14
- 依赖：`pip install -r requirements.txt`
- 敏感配置：`.env`（含数据源 token / 邮件 / 推送密钥）

## 部署（云服务器）

`deploy/crontab.server`：质量池信号 09:00（昨收数据）→ 执行 09:31 → ETF 跨资产动量轮动 10:40 → ETF 再平衡 14:40（周一）→ 卫星轮动 14:50。质量池使用独立台账与状态，规则见[质量池规格](strategy/roe_quality_pool.md)。
环境快照任务已摘除（唯一消费方退役，未来策略需要市场门控时按注释加回一行 14:35）。

## 文档

- [strategy/](strategy/) — 策略，不记录实验等

## 模拟成绩

日报顶部展示最近完整收盘的累计收益、同期基准、超额收益、运行天数与仓位现金。
ETF 长期配置和行业动量共用组合成绩；质量池与跨资产动量轮动各自独立统计。缺价时保留最近完整日期，
未开始记账的账户不显示虚构收益。当前未计手续费、分红和份额变更。

`deploy/crontab.server` 在工作日北京时间次日08:30采集上一交易日收盘成绩，给ETF日线源留出整夜，
并回补最近5个交易日的不完整记录；更新服务器代码后需重新安装该定时任务表。仍缺价时保留最近完整成绩。
收盘任务不交易、不发送通知，成绩与台账保存在服务器，本地不会自动同步。

```bash
python portfolio_report.py                  # 采集最近已收盘交易日，并回补前5个交易日缺口
python portfolio_report.py --dry-run        # 只展示，不保存
python portfolio_report.py --date 2026-10-09 # 重试或补采指定已收盘交易日（只采该日）
```

基准与存储口径见 [src/trade_ledger/performance.py](src/trade_ledger/performance.py) 与 [ledger.py](src/trade_ledger/ledger.py)。

## 测试

使用项目 Python 环境运行：

```bash
python -m pytest tests/ -ra
```

ETF 研究分析与测试统一使用 Python，无需安装 Node.js。研究入口和输出口径见
[ETF 研究复现说明](research/studies/etf_rotation/IMPLEMENTATION.md)。

