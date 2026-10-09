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

`deploy/crontab.server`：质量池执行 09:31 → ETF 再平衡 14:40（周一）→ 卫星轮动 14:50 → 质量池信号 15:10。
环境快照任务已摘除（唯一消费方退役，未来策略需要市场门控时按注释加回一行 14:35）。

## 文档

- [strategy/](strategy/) — 策略，不记录实验等
- [docs/trade\_ledger.md](docs/trade_ledger.md) — 模拟测试口径

## 测试

```bash
pytest tests/
```

