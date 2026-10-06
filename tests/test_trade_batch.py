# -*- coding: utf-8 -*-
"""批次 interface 的真实台账闭环：预检零追加，逐笔事实与结果一致。"""

import math

import pytest

from src.trade_ledger import (
    BatchOrder, LedgerError, NOMINAL_EQUITY, TradeRecord, append_trade,
    derive, execute_batch, load_trades,
)


def _buy(code, qty, price, name="乙", reason="r_buy"):
    return BatchOrder(code, name, "buy", qty, price, reason)


def _sell(code, qty, price, name="甲", reason="r_sell"):
    return BatchOrder(code, name, "sell", qty, price, reason)


def _run(orders, path, **kwargs):
    kwargs.setdefault("account", "satellite")
    kwargs.setdefault("trade_date", "2026-09-27")
    return execute_batch(orders, path=path, **kwargs)


def _seed(path, holdings=None, cash=None, day="2026-09-25", account="core"):
    """先写合法建仓；需要受限现金时用另一笔真实持仓占用资金。"""
    holdings = holdings or {}
    for code, qty in holdings.items():
        append_trade(TradeRecord(day, code, code, "buy", qty, 1.0,
                                 account=account), path=path)
    if cash is not None:
        used = NOMINAL_EQUITY - sum(holdings.values()) - cash
        if used > 0:
            append_trade(TradeRecord(day, "600999", "资金占用持仓", "buy",
                                     100, used / 100, account=account), path=path)
    return load_trades(path)


def _snapshot(path):
    return derive(load_trades(path), "2026-09-27")


class TestHappyPath:
    def test_sells_first_then_buys(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 1000}, cash=100.0)
        result = _run([_buy("512880", 2000, 1.0), _sell("510300", 1000, 4.0)], path)
        assert result.abort is None
        written = load_trades(path)[len(before):]
        assert [t.side for t in written] == ["sell", "buy"]
        assert all(t.account == "satellite" for t in written)
        assert written[0].reasons == ["r_sell"]
        assert _snapshot(path).cash == 2100.0

    def test_statuses_rendered(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 1000})
        result = _run([_sell("510300", 1000, 4.0)], path)
        outcome = result.outcomes[0]
        assert outcome.status == "ok" and outcome.qty == 1000 and outcome.amount == 4000.0
        assert _snapshot(path).held_counts() == {}

    def test_account_is_attribution_not_a_separate_pool(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 100}, cash=0, account="core")
        result = _run([_sell("510300", 100, 4), _buy("512880", 400, 1)],
                      path, account="satellite")
        assert result.abort is None and all(o.status == "ok" for o in result.outcomes)
        assert _snapshot(path).cash == 0
        assert _snapshot(path).held_counts()["512880"] == 400


class TestLotConvergence:
    def test_sell_floor_and_one_outcome_per_input(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 200, "512880": 200})
        result = _run([_sell("510300", 150, 4), _sell("512880", 90, 1)], path)
        assert len(result.outcomes) == 2
        by_code = {o.order.code: o for o in result.outcomes}
        assert by_code["510300"].qty == 100 and by_code["510300"].status == "ok"
        assert by_code["512880"].status == "skipped"
        assert [t.qty for t in load_trades(path)[len(before):]] == [100]

    @pytest.mark.parametrize("requested", [25, 150])
    def test_buy_round_lot(self, tmp_path, requested):
        path = tmp_path / "ledger.jsonl"
        result = _run([_buy("512880", requested, 1)], path)
        assert result.outcomes[0].qty == 100
        assert _snapshot(path).held_counts() == {"512880": 100}


class TestSafetyAbort:
    def test_sell_exceeds_holding_adds_nothing(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 1000})
        result = _run([_sell("510300", 2000, 4), _buy("512880", 100, 1)], path)
        assert "卖出 2000 股 > 持仓 1000 股" in result.abort
        assert result.outcomes == [] and load_trades(path) == before

    def test_empty_ledger_cannot_sell(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        result = _run([_sell("510300", 100, 1)], path)
        assert result.abort and not path.exists()

    def test_same_code_total_sell_is_checked(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 100})
        result = _run([_sell(" 510300 ", 100, 4), _sell("510300", 100, 4)], path)
        assert "累计卖出 200 股 > 持仓 100 股" in result.abort
        assert result.outcomes == [] and load_trades(path) == before

    def test_buy_exceeds_cash_plus_proceeds(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 1000}, cash=2000)
        result = _run([_sell("510300", 1000, 4), _buy("512880", 7000, 1)], path)
        assert "买入总额 7,000 元 > 可用资金 2,000 元 + 卖出回款 4,000 元" in result.abort
        assert load_trades(path) == before

    def test_checks_use_converged_numbers(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 100})
        result = _run([_sell("510300", 150, 4)], path)
        assert result.abort is None and _snapshot(path).held_counts() == {}

    def test_external_sell_is_seen_before_batch_execution(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 1000})
        old = _snapshot(path)
        append_trade(TradeRecord("2026-09-26", "510300", "ETF", "sell", 500, 1), path)
        before = load_trades(path)
        assert old.held_counts()["510300"] == 1000
        result = _run([_sell("510300", 1000, 1)], path)
        assert "持仓 500 股" in result.abort and load_trades(path) == before

    def test_external_cash_use_is_seen_before_batch_execution(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, cash=300)
        old = _snapshot(path)
        append_trade(TradeRecord("2026-09-26", "159915", "ETF", "buy", 100, 2), path)
        before = load_trades(path)
        assert old.cash == 300
        result = _run([_buy("512880", 200, 1)], path)
        assert result.abort and load_trades(path) == before

    def test_future_ledger_cannot_supply_historical_batch(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 100}, day="2026-09-28")
        result = _run([_sell("510300", 100, 1)], path)
        assert "早于现有流水" in result.abort and load_trades(path) == before

    def test_corrupt_ledger_stops_without_writing(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        path.write_text("{bad json\n", encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(LedgerError):
            _run([_buy("512880", 100, 1)], path)
        assert path.read_bytes() == before

    def test_finite_orders_with_overflowing_total_add_nothing(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 200})
        result = _run([_sell("510300", 100, 1e306), _sell("510300", 100, 1e306)], path)
        assert "累计成交金额或资金非有限数" in result.abort
        assert load_trades(path) == before
        assert math.isfinite(_snapshot(path).cash)


class TestPerOrderIsolation:
    def test_invalid_order_does_not_block_others(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 1000, "512880": 1000})
        result = _run([_sell("510300", 1000, 0), _sell("512880", 1000, 4)], path)
        assert {o.order.code: o.status for o in result.outcomes} == {
            "510300": "failed", "512880": "ok"}
        assert [t.code for t in load_trades(path)[len(before):]] == ["512880"]
        assert _snapshot(path).held_counts() == {"510300": 1000}

    def test_failed_sale_cannot_fund_buy_but_smaller_buy_continues(self, tmp_path, monkeypatch):
        import src.trade_ledger.batch as batch
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 100}, cash=100)
        original = batch.append_trade

        def fail_sell(record, path):
            if record.side == "sell":
                raise OSError("simulated write failure")
            return original(record, path=path)

        monkeypatch.setattr(batch, "append_trade", fail_sell)
        result = _run([_sell("510300", 100, 4), _buy("512880", 300, 1),
                       _buy("159915", 100, 1)], path)
        assert result.abort is None
        assert [o.status for o in result.outcomes] == ["failed", "failed", "ok"]
        assert "实际台账现金" in result.outcomes[1].message
        assert _snapshot(path).cash == 0
        assert _snapshot(path).held_counts()["159915"] == 100
        assert "512880" not in _snapshot(path).held_counts()

    def test_written_sale_followed_by_error_uses_actual_ledger_cash(self, tmp_path, monkeypatch):
        import src.trade_ledger.batch as batch
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 100}, cash=0)
        original = batch.append_trade

        def written_then_error(record, path):
            result = original(record, path=path)
            if record.side == "sell":
                raise OSError("error after append")
            return result

        monkeypatch.setattr(batch, "append_trade", written_then_error)
        result = _run([_sell("510300", 100, 4), _buy("512880", 400, 1)], path)
        assert [o.status for o in result.outcomes] == ["failed", "ok"]
        assert _snapshot(path).cash == 0
        assert _snapshot(path).held_counts()["512880"] == 400

    @pytest.mark.parametrize("price", [math.nan, math.inf, -math.inf, 0, -1, 1e308])
    def test_invalid_price_never_enters_ledger(self, tmp_path, price):
        path = tmp_path / "ledger.jsonl"
        result = _run([_buy("512880", 100, price), _buy("159915", 100, 1)], path)
        assert [o.status for o in result.outcomes] == ["failed", "ok"]
        assert all(math.isfinite(t.price) and t.price > 0 for t in load_trades(path))
        assert _snapshot(path).held_counts() == {"159915": 100}

    def test_unknown_side_is_reported(self, tmp_path):
        result = _run([BatchOrder("512880", "ETF", "hold", 100, 1)],
                      tmp_path / "ledger.jsonl")
        assert len(result.outcomes) == 1 and result.outcomes[0].status == "failed"


class TestExecutionPrice:
    def test_rounded_down_price_used_for_check_and_outcome(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, cash=100)
        result = _run([_buy("512880", 100, 1.00004)], path)
        assert result.abort is None and result.outcomes[0].status == "ok"
        assert result.outcomes[0].amount == 100
        assert _snapshot(path).cash == 0

    def test_rounded_up_price_can_abort(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        before = _seed(path, cash=100)
        result = _run([_buy("512880", 100, 1.00006)], path)
        assert result.abort and load_trades(path) == before

    def test_float_noise_does_not_reject_exact_cash_purchase(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, cash=0.01)
        # 多笔小额追加令 float 余额有累加尾差，成交额本身恰好等于余额。
        result = _run([_buy("512880", 100, 0.0001)], path)
        assert result.abort is None and result.outcomes[0].status == "ok"
        assert _snapshot(path).cash == pytest.approx(0, abs=1e-7)


class TestEmptyBatch:
    def test_no_orders_no_abort(self, tmp_path):
        result = _run([], tmp_path / "ledger.jsonl")
        assert result.abort is None and result.outcomes == []


class TestStrategyCallers:
    def test_etf_ignores_stale_allocation_holdings_for_write_safety(self, tmp_path):
        from types import SimpleNamespace
        import etf_observe
        from src.etf.rebalancer import RebalanceOrder

        path = tmp_path / "ledger.jsonl"
        before = _seed(path, {"510300": 100})
        allocation = {
            "plan": SimpleNamespace(orders=[RebalanceOrder(
                "510300", "ETF", "sell", 200, 200, 0.2, 0, "test")]),
            "positions": [{"code": "510300", "count": 200, "current_price": 1}],
            "prices": {}, "total_assets": NOMINAL_EQUITY, "cash": NOMINAL_EQUITY,
        }
        report = etf_observe._execute_batch(allocation, ledger_path=path)
        assert "持仓 100 股" in report and load_trades(path) == before

    def test_industry_writes_without_caller_cash_or_holdings(self, tmp_path):
        import industry_momentum
        from src.etf.industry_momentum import RotationOrder

        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 100}, cash=0)
        orders = [RotationOrder("510300", "ETF", "sell", 100, 4, 400, "exit"),
                  RotationOrder("512880", "ETF", "buy", 400, 1, 400, "entry")]
        abort, lines = industry_momentum._execute(orders, "2026-09-27", ledger_path=path)
        assert abort is None and len(lines) == 2
        assert _snapshot(path).cash == 0
        assert _snapshot(path).held_counts()["512880"] == 400


class TestOutcomeLine:
    def test_ok_line(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        _seed(path, {"510300": 1000})
        result = _run([_sell("510300", 1000, 4, name="甲")], path)
        assert result.outcomes[0].outcome_line == (
            "✅ SELL 甲(510300) 1000股 ≈ 4,000元 —— 已记账（r_sell）")

    def test_skipped_line_appears_once(self, tmp_path):
        result = _run([_sell("512880", 90, 1, name="乙")], tmp_path / "ledger.jsonl")
        assert len(result.outcomes) == 1
        assert result.outcomes[0].outcome_line == "⚠️ 跳过 SELL 乙(512880)：股数不足一手"

    def test_failed_line(self, tmp_path):
        result = _run([_sell("510300", 1000, 0, name="甲")], tmp_path / "ledger.jsonl")
        assert result.outcomes[0].status == "failed"
        assert result.outcomes[0].outcome_line.startswith(
            "❌ SELL 甲(510300) 1000股 —— 记账失败: ")
