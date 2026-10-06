# -*- coding: utf-8 -*-
"""名义成交台账（跨策略共享，有交易语义）。

持仓/资金唯一事实来源：append-only JSONL 流水 + 纯函数推导视图。
接口面三个动词——

- append_trade：写侧单笔（cron 模拟记账确认 / book.py 手动建仓）；
- execute_batch：写侧批次（batch.py）——整手收敛、两项通用安全校验
  （卖量≤持仓、买额≤现金+卖出回款）、先卖后买、逐单隔离，返回结构化
  BatchResult 供入口纯渲染；策略自有约束（卫星预算等）留在消费方不进本接口；
- derive：读侧（持仓 dict 同形妙想 get_positions + entry_map + 组合敞口），
- snapshot_with_prices：读侧 + 现价装配（prices.py），供敞口/再平衡/轮动入口，
  derive 本体保持零 I/O。

口径决策见 docs/trade_ledger.md：单池名义期初、全百分比呈现、无资金流条目。
"""

from src.trade_ledger.batch import (
    BatchOrder, BatchResult, OrderOutcome, execute_batch,
)
from src.trade_ledger.ledger import (
    DEFAULT_LEDGER_PATH,
    NOMINAL_EQUITY,
    LedgerError,
    LedgerSnapshot,
    TradeRecord,
    append_trade,
    derive,
    derive_from_file,
    load_trades,
)
from src.trade_ledger.prices import (
    latest_close,
    resolve_prices,
    snapshot_with_prices,
)

__all__ = [
    "DEFAULT_LEDGER_PATH",
    "NOMINAL_EQUITY",
    "BatchOrder",
    "BatchResult",
    "LedgerError",
    "LedgerSnapshot",
    "OrderOutcome",
    "TradeRecord",
    "append_trade",
    "derive",
    "derive_from_file",
    "execute_batch",
    "load_trades",
    "latest_close",
    "resolve_prices",
    "snapshot_with_prices",
]
