# -*- coding: utf-8 -*-
"""trade_ledger.execute_batch 批次语义测试。

安全不变量（卖量≤持仓、买额≤现金+回款、先卖后买、逐单隔离、整手收敛）
从入口脚本迁入接口后，第一次可全测。tmp_path 台账文件模式对齐
test_sell_pipeline 先例。
"""

from src.trade_ledger import BatchOrder, execute_batch, load_trades


def _buy(code, qty, price, name="乙", reason="r_buy"):
    return BatchOrder(code=code, name=name, side="buy", qty=qty,
                      price=price, reason=reason)


def _sell(code, qty, price, name="甲", reason="r_sell"):
    return BatchOrder(code=code, name=name, side="sell", qty=qty,
                      price=price, reason=reason)


def _run(orders, path, **kw):
    kw.setdefault("account", "satellite")
    kw.setdefault("trade_date", "2026-09-27")
    return execute_batch(orders, path=path, **kw)


class TestHappyPath:
    def test_sells_first_then_buys(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_buy("512880", 2000, 1.0), _sell("510300", 1000, 4.0)],
                   f, held_counts={"510300": 1000}, cash=100.0)
        assert res.abort is None
        trades = load_trades(f)
        assert [t.side for t in trades] == ["sell", "buy"]
        assert all(t.account == "satellite" for t in trades)
        assert trades[0].reasons == ["r_sell"]

    def test_statuses_rendered(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 1000, 4.0)], f,
                   held_counts={"510300": 1000}, cash=0.0)
        oc = res.outcomes[0]
        assert oc.status == "ok" and oc.qty == 1000 and oc.amount == 4000.0


class TestLotConvergence:
    def test_sell_floor_lot_and_skip(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 150, 4.0), _sell("512880", 90, 1.0)],
                   f, held_counts={"510300": 200, "512880": 200}, cash=0.0)
        by_code = {oc.order.code: oc for oc in res.outcomes}
        assert by_code["510300"].qty == 100 and by_code["510300"].status == "ok"
        assert by_code["512880"].status == "skipped"
        assert [t.qty for t in load_trades(f)] == [100]

    def test_buy_round_lot(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        _run([_buy("512880", 150, 1.0)], f, held_counts={}, cash=1000.0)
        assert [t.qty for t in load_trades(f)] == [100]


class TestSafetyAbort:
    def test_sell_exceeds_holding_aborts_zero_write(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 2000, 4.0), _buy("512880", 100, 1.0)],
                   f, held_counts={"510300": 1000}, cash=10000.0)
        assert "卖出 2000 股 > 持仓 1000 股" in res.abort
        assert res.outcomes == [] and not f.exists()

    def test_buy_exceeds_cash_plus_proceeds_aborts(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 1000, 4.0), _buy("512880", 7000, 1.0)],
                   f, held_counts={"510300": 1000}, cash=2000.0)
        assert "买入总额 7,000 元 > 可用资金 2,000 元 + 卖出回款 4,000 元" in res.abort
        assert not f.exists()

    def test_checks_use_converged_numbers(self, tmp_path):
        # 收敛后口径：请求卖 150 收敛为 100 ≤ 持仓 100，不得误中止
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 150, 4.0)], f,
                   held_counts={"510300": 100}, cash=0.0)
        assert res.abort is None


class TestPerOrderIsolation:
    def test_single_failure_does_not_block_batch(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        # price=0 触发 TradeRecord 校验失败（LedgerError），其余照常记账
        res = _run([_sell("510300", 1000, 0.0), _sell("512880", 1000, 4.0,
                                                      name="丙")],
                   f, held_counts={"510300": 1000, "512880": 1000}, cash=0.0)
        statuses = {oc.order.code: oc.status for oc in res.outcomes}
        assert statuses == {"510300": "failed", "512880": "ok"}
        assert "price" in res.outcomes[0].message
        assert [t.code for t in load_trades(f)] == ["512880"]


class TestEmptyBatch:
    def test_no_orders_no_abort(self, tmp_path):
        res = _run([], tmp_path / "ledger.jsonl", held_counts={}, cash=0.0)
        assert res.abort is None and res.outcomes == []


class TestOutcomeLine:
    """outcome_line 渲染单点：三分支文案钉死（两入口报告直接收集此行）。"""

    def test_ok_line(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("510300", 1000, 4.0, name="甲", reason="r_sell")], f,
                   held_counts={"510300": 1000}, cash=0.0)
        assert res.outcomes[0].outcome_line == (
            "✅ SELL 甲(510300) 1000股 ≈ 4,000元 —— 已记账（r_sell）")

    def test_skipped_line(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        res = _run([_sell("512880", 90, 1.0, name="乙")], f,
                   held_counts={"512880": 200}, cash=0.0)
        assert res.outcomes[0].outcome_line == "⚠️ 跳过 SELL 乙(512880)：股数不足一手"

    def test_failed_line(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        # price=0 触发 TradeRecord 校验失败（LedgerError）
        res = _run([_sell("510300", 1000, 0.0, name="甲")], f,
                   held_counts={"510300": 1000}, cash=0.0)
        oc = res.outcomes[0]
        assert oc.status == "failed"
        assert oc.outcome_line.startswith("❌ SELL 甲(510300) 1000股 —— 记账失败: ")
