from types import SimpleNamespace

import pandas as pd
import pytest

from research.studies.etf import v1_local
from research.tools.backtest.engine import round_trip_stats


@pytest.mark.parametrize("multiplier", [3.0, 1.015])
def test_run_applies_adjusted_return_before_selling_after_missing_bar(monkeypatch, multiplier):
    code = "510300"
    days = pd.Index(["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"])
    values = {
        "close": [9.0, 9.0, None, 9.9 / multiplier],
        "hfq_close": [9.0, 9.0, None, 9.9],
        "amount": [1e8, 1e8, None, 1e8],
        "quality": ["ok", "ok", None, "ok"],
    }
    frames = {field: pd.DataFrame({code: column}, index=days)
              for field, column in values.items()}
    data = SimpleNamespace(
        trading_days=lambda: days,
        securities_table=pd.DataFrame({"ok_rows": [3]}, index=[code]),
        _frame=frames.__getitem__,
    )
    monkeypatch.setattr(v1_local, "LocalEtfData", lambda _: data)
    bt = v1_local.MomentumLocal(capital=10_000)
    bt.k.SCORE_EXIT_CONFIRM = 1
    monkeypatch.setattr(bt, "rebuild_pool", lambda loc, held: [code])
    monkeypatch.setattr(
        bt, "rank_pool",
        lambda loc, held: ([{"code": code, "score": -1 if loc == 3 else 1}], True)
        if loc != 2 else ([], False),
    )

    nav, _ = bt.run("2024-01-06", "2024-01-10")

    buy, sell = [row for row in bt.account.ledger if row["side"] in {"buy", "sell"}]
    assert sell["quantity"] == pytest.approx(buy["quantity"] * multiplier)
    assert sell["price"] == 9.9 / multiplier
    assert nav.index[0] == pd.Timestamp("2024-01-08")
    assert nav.iloc[1] == nav.iloc[0]
    assert nav.iloc[-1] == pytest.approx(
        buy["cash_after"] + sell["quantity"] * sell["price"] - sell["fee"]
    )
    assert not bt.account.positions
    stats = round_trip_stats(bt.account.ledger)
    assert stats["完整回合数"] == 1
    assert stats["期末未平批次"] == 0
    expected_return = (sell["quantity"] * sell["price"] - sell["fee"]) / (
        buy["quantity"] * buy["price"] + buy["fee"]
    ) - 1
    assert stats["平均盈利%"] == pytest.approx(expected_return * 100)
