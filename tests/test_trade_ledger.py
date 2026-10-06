# -*- coding: utf-8 -*-
"""成交台账：写侧契约 + 读侧推导行为。

只测外部行为（写入→推导回读），不测内部 dict 结构；
tmp_path 流水文件模式对齐 test_sell_pipeline 的 ExitLedger 先例。
"""

import json

import pytest

from src.trade_ledger.ledger import (
    NOMINAL_EQUITY, LedgerError, TradeRecord, append_trade, derive,
    load_trades,
)


def _trade(date, code="600519", side="buy", qty=100, price=100.0,
           name="贵州茅台", account="core", **kw):
    return TradeRecord(date=date, code=code, name=name, side=side,
                       qty=qty, price=price, account=account, **kw)


# ── 写侧契约 ──

def test_append_rejects_non_lot_qty():
    with pytest.raises(LedgerError):
        _trade("2026-09-26", qty=150)


def test_append_rejects_bad_fields():
    with pytest.raises(LedgerError):
        _trade("2026-9-26")                 # 日期格式
    with pytest.raises(LedgerError):
        _trade("2026-09-26", side="hold")    # 非法方向
    with pytest.raises(LedgerError):
        _trade("2026-09-26", price=0)        # 非正价格
    with pytest.raises(LedgerError):
        _trade("2026-09-26", account="main")  # 未知归因标签


def test_append_load_roundtrip_with_code_normalization(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_trade(_trade("2026-09-25", code="600519"), path=path)
    append_trade(_trade("2026-09-26", code="600519", side="sell"), path=path)
    trades = load_trades(path)
    assert [t.side for t in trades] == ["buy", "sell"]
    assert trades[0].code == "600519"
    # 文件是纯 JSONL，一行一 dict
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["qty"] == 100


def test_load_corrupt_line_raises_not_skips(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_trade(_trade("2026-09-25"), path=path)
    with path.open("a", encoding="utf-8") as f:
        f.write("{ not json \n")
    with pytest.raises(LedgerError):
        load_trades(path)


def test_load_tolerates_unknown_fields(tmp_path):
    """加列演进：旧读取器遇到未来新增字段不炸。"""
    path = tmp_path / "ledger.jsonl"
    obj = {"date": "2026-09-25", "code": "600519", "name": "X",
           "side": "buy", "qty": 100, "price": 100.0, "account": "core",
           "reasons": [], "source": "book", "manual": False,
           "future_field": 42}
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    assert load_trades(path)[0].code == "600519"


# ── 读侧推导 ──

def test_derive_empty_book_is_nominal_cash():
    snap = derive([], "2026-09-26")
    assert snap.positions == [] and snap.entry_map == {}
    assert snap.cash == NOMINAL_EQUITY
    assert snap.total_assets == NOMINAL_EQUITY


def test_derive_t_plus_1_boundary():
    trades = [_trade("2026-09-24", qty=300), _trade("2026-09-26", qty=200)]
    snap = derive(trades, as_of="2026-09-26")
    p = snap.positions[0]
    assert p["count"] == 500
    assert p["avail_count"] == 300          # 当日买入不可卖
    assert snap.entry_map["600519"] == "2026-09-26"  # 入场日=最新一笔买


def test_derive_moving_average_cost_and_sell():
    trades = [
        _trade("2026-09-22", qty=100, price=100.0),
        _trade("2026-09-23", qty=100, price=200.0),   # 均价 150
        _trade("2026-09-24", side="sell", qty=100, price=180.0),
    ]
    snap = derive(trades, as_of="2026-09-25")
    p = snap.positions[0]
    assert p["count"] == 100 and p["cost_price"] == 150.0
    # 现金：期初 −10000 −20000 +18000
    assert snap.cash == pytest.approx(NOMINAL_EQUITY - 12000.0)
    assert snap.entry_map["600519"] == "2026-09-23"


def test_derive_carries_account_attribution():
    """持仓带出末次买入的 account 标签，供核心/卫星归属拆分（不再用代码名单反推）。"""
    trades = [
        _trade("2026-09-22", qty=100),                     # 未打标 → core
        _trade("2026-09-23", code="159611", name="电力ETF",
               qty=200, account="satellite"),
    ]
    snap = derive(trades, as_of="2026-09-24")
    by_code = {p["code"]: p for p in snap.positions}
    assert by_code["600519"]["account"] == "core"
    assert by_code["159611"]["account"] == "satellite"


def test_derive_oversell_breaks_consistency():
    trades = [_trade("2026-09-22", qty=100),
              _trade("2026-09-24", side="sell", qty=200)]
    with pytest.raises(LedgerError):
        derive(trades, as_of="2026-09-25")


def test_derive_full_close_drops_position():
    trades = [_trade("2026-09-22", qty=100),
              _trade("2026-09-24", side="sell", qty=100)]
    snap = derive(trades, as_of="2026-09-25")
    assert snap.positions == [] and snap.entry_map == {}


def test_derive_single_pool_across_accounts():
    """account 只是归因标签：现金/敞口单池，不分账。"""
    trades = [
        _trade("2026-09-22", code="600519", qty=100, price=100.0),
        _trade("2026-09-22", code="512880", qty=1000, price=1.0,
               name="证券ETF", account="core"),
    ]
    snap = derive(trades, as_of="2026-09-25",
                  prices={"600519": 120.0, "512880": 1.2})
    assert {p["code"] for p in snap.positions} == {"600519", "512880"}
    assert snap.invested == pytest.approx(120.0 * 100 + 1.2 * 1000)
    assert snap.cash == pytest.approx(NOMINAL_EQUITY - 11000.0)
    assert sum(p["pos_pct"] for p in snap.positions) == pytest.approx(
        snap.invested / snap.total_assets * 100)


def test_derive_missing_price_yields_zero_market_value():
    """现价取数失败口径：持仓在、市值 0（与 fail-open 敞口一致），不炸判定。"""
    snap = derive([_trade("2026-09-22", qty=100)], as_of="2026-09-25", prices={})
    p = snap.positions[0]
    assert p["count"] == 100 and p["market_value"] == 0.0
    assert p["profit_pct"] == 0.0
