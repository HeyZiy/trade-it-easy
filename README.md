# trade-it-easy

个人 A 股交易策略系统：策略研究、信号生产、尾盘执行与名义台账记账一体化。

## 策略地图

| 策略          | 入口                                               |
| ----------- | ------------------------------------------------ |
| ETF 长期配置    | `etf_observe.py`（周一尾盘自动调仓批次）                     |
| 行业动量轮动      | `industry_momentum.py`                           |
| 趋势回踩        | `pullback_analysis.py`（买）/ `pullback_sell.py`（卖） |
| 价格循环（Cycle） | —                                                |

各策略的定位、假设与不可混用边界见 [strategy/overview.md](strategy/overview.md)。

## 运行环境

- Python 3.14
- 依赖：`pip install -r requirements.txt`
- 敏感配置：`.env`（含数据源 token / 邮件 / 推送密钥）

## 部署（云服务器）

`deploy/crontab.server`：环境快照 14:35 → ETF 再平衡 14:40（周一）→ 趋势卖出 14:45 → 卫星轮动 14:50 → 买入分析 15:10。
环境判定只产标签与判定事实（`data/environment.json` 快照），policy 归各策略。

## 文档

- [strategy/](strategy/) — 各策略规格、证据与铁律
- [docs/trade\_ledger.md](docs/trade_ledger.md) — 名义成交台账口径（持仓/资金唯一事实来源）

## 测试

```bash
pytest tests/
```

