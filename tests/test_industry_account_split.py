from datetime import timedelta
import json
from pathlib import Path

import pytest

from deploy.split_industry_account import apply_split, prepare_split
from src.trade_ledger.ledger import TradeRecord, append_trade, derive, load_trades
from src.trade_ledger.performance import _fingerprint, performance_path


def calendar(start, end):
    return [start + timedelta(days=n) for n in range((end - start).days + 1)
            if (start + timedelta(days=n)).weekday() < 5]


class Prices:
    def close_prices(self, codes, asof):
        return {code: (10 if asof < "2026-10-13" else 12) for code in codes}

    def index_prices(self, codes, asof):
        return {code: 100 for code in codes}


def shared_ledger(root):
    path = root / "data" / "trade_ledger.jsonl"
    for trade in (TradeRecord("2026-10-12", "510300", "核心", "buy", 100, 10),
                  TradeRecord("2026-10-12", "512500", "行业", "buy", 200, 10, account="satellite"),
                  TradeRecord("2026-10-13", "512500", "行业", "sell", 100, 12, account="satellite")):
        append_trade(trade, path)
    performance_path(path).write_text(json.dumps({"benchmark": {"name": "原参考", "weights": {"CASH": 1}},
                                                  "records": [{"date": "2026-10-12"}, {"date": "2026-10-13"}]}), encoding="utf-8")
    return path


def test_split_preserves_exact_trades_and_rebuilds_separate_performance(tmp_path):
    source = shared_ledger(tmp_path)
    original = source.read_bytes()
    plan = prepare_split(tmp_path, Prices(), calendar)
    assert source.read_bytes() == original  # dry-run is read only
    archive = apply_split(plan)
    target = source.with_name("trade_ledger_industry_momentum.jsonl")
    assert (archive / source.name).read_bytes() == original
    assert load_trades(source) == [t for t in load_trades(archive / source.name) if t.account == "core"]
    assert load_trades(target) == [t for t in load_trades(archive / source.name) if t.account == "satellite"]
    for path, expected in ((source, "510300"), (target, "512500")):
        trades = load_trades(path)
        state = json.loads(performance_path(path).read_text(encoding="utf-8"))
        row = state["records"][-1]
        assert row["fingerprint"] == _fingerprint(trades, row["date"])
        assert [p["code"] for p in row["positions"]] == [expected]
        assert row["issues"] == []
    assert derive(load_trades(source), "2026-10-13").cash == 999000
    assert derive(load_trades(target), "2026-10-13").cash == 999200
    with pytest.raises(ValueError, match="拒绝重复迁移"):
        prepare_split(tmp_path, Prices(), calendar)


def test_split_refuses_missing_latest_prices_without_touching_ledger(tmp_path):
    source = shared_ledger(tmp_path)
    original = source.read_bytes()

    class Missing(Prices):
        def close_prices(self, codes, asof):
            return {}

    with pytest.raises(ValueError, match="最新成绩不完整"):
        prepare_split(tmp_path, Missing(), calendar)
    assert source.read_bytes() == original
    assert not source.with_name("trade_ledger_industry_momentum.jsonl").exists()


def test_split_refuses_source_changed_after_preparation(tmp_path):
    source = shared_ledger(tmp_path)
    plan = prepare_split(tmp_path, Prices(), calendar)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="原始数据已变化"):
        apply_split(plan)


def test_manual_book_routes_satellite_to_independent_account():
    from types import SimpleNamespace
    from book import _ledger_path
    from src.etf.industry_account import LEDGER_PATH
    from src.etf_account import ACCOUNT_PATH

    assert _ledger_path(SimpleNamespace(path=None, account="satellite")) == LEDGER_PATH
    assert Path(_ledger_path(SimpleNamespace(path=None, account="core"))) == ACCOUNT_PATH
    assert _ledger_path(SimpleNamespace(path="explicit.jsonl", account="satellite")) == "explicit.jsonl"
