# trade-it-easy

个人 A 股交易策略系统：策略研究、信号生产、尾盘执行与名义台账记账一体化。

## 策略地图

| 策略          | 入口                                                   |
| ----------- | ---------------------------------------------------- |
| ETF 长期配置    | `etf_observe.py`（周一尾盘自动调仓批次）                         |
| 行业动量轮动      | `industry_momentum.py`                               |
| 质量池轮动       | `quality_pool.py signal` / `quality_pool.py execute` |
| 价格循环（Cycle） | —                                                    |

趋势回踩（pullback）已退役，入口脚本不再存在；环境快照随之休眠（唯一消费方
消失），模块与 `data/environment.json` 结构保留，见 strategy/overview\.md。

各策略的定位、假设与不可混用边界见 [strategy/overview.md](strategy/overview.md)。

## 运行环境

- Python 3.14
- 依赖：`pip install -r requirements.txt`
- 敏感配置：`.env`（含数据源 token / 邮件 / 推送密钥）

## 部署（云服务器）

`deploy/crontab.server`：质量池信号 08:00（昨收数据）→ 执行 09:31 → ETF 再平衡 14:40（周一）→ 卫星轮动 14:50。质量池使用独立台账与状态，规则见[质量池规格](strategy/roe_quality_pool.md)。
环境快照任务已摘除（唯一消费方退役，未来策略需要市场门控时按注释加回一行 14:35）。

## 文档

- [strategy/](strategy/) — 策略，不记录实验等
- [docs/trade\_ledger.md](docs/trade_ledger.md) — 模拟测试口径

## 模拟成绩

日报顶部展示最近完整收盘的累计收益、同期基准、超额收益、运行天数与仓位现金。
ETF核心配置和行业动量共用组合成绩，质量池独立统计。缺价时保留最近完整日期，
未开始记账的账户不显示虚构收益。当前未计手续费、分红和份额变更。

`deploy/crontab.server` 增加15:30收盘采集；更新服务器代码后需重新安装该定时任务表。
收盘任务不交易、不发送通知，成绩与台账保存在服务器，本地不会自动同步。

```bash
python portfolio_report.py                  # 采集最近已收盘交易日
python portfolio_report.py --dry-run        # 只展示，不保存
python portfolio_report.py --date 2026-10-09 # 重试或补采指定已收盘交易日
```

基准规则和存储口径见 [台账规格第七节](docs/trade_ledger.md#七模拟报告与收盘成绩第一版)。

## 测试

```bash
pytest tests/
```

