"""手动验证真实收盘行情到估值的链路，不使用生产台账。

PowerShell: $env:RUN_LIVE_CLOSE_TEST='1'
python -m pytest tests/test_portfolio_performance_live.py -q -s
默认检查本次缺价日期；可用 CLOSE_TEST_DATE 指定其他已收盘交易日。
"""

from datetime import date
import math
import os

import pytest

from src.etf.config import CORE_BASELINE
from src.trade_ledger.closing_prices import ClosingPrices
from src.trade_ledger.ledger import TradeRecord, append_trade
from src.trade_ledger.performance import collect_close, render_summary


@pytest.mark.skipif(os.getenv("RUN_LIVE_CLOSE_TEST") != "1",
                    reason="需显式启用真实行情网络测试")
def test_reported_missing_etfs_produce_complete_live_valuation(tmp_path):
    from src.trading_calendar import get_trading_dates

    asof = os.getenv("CLOSE_TEST_DATE", "2026-10-15")
    day = date.fromisoformat(asof)
    codes = "159707 159928 159934 159938 159981 512200 513100 513380 513500 515180 516560 563360".split()
    feed = ClosingPrices()
    prices = feed.close_prices(codes, asof)
    missing = sorted(set(codes) - prices.keys())
    assert not missing, f"{asof} 缺少真实收盘价：{missing}"
    print(f"{asof} closes: {prices}")

    # 独立测试账户，只验证数据和估值链路；收益不代表用户账户成绩。
    ledger = tmp_path / "live_close.jsonl"
    for code in codes:
        append_trade(TradeRecord(asof, code, code, "buy", 100, prices[code]), ledger)
    weights = {asset.code: asset.neutral_weight for asset in CORE_BASELINE}
    state = collect_close(
        ledger, asof, {"name": "ETF期初权重买入持有", "weights": weights},
        close_prices=feed.close_prices, index_prices=feed.index_prices,
        trading_dates=get_trading_dates,
    )
    row = state["records"][-1]
    assert not row["issues"], row["issues"]
    assert math.isfinite(row["nav"]) and row["nav"] == pytest.approx(1)
    assert math.isfinite(row["benchmark_nav"]) and row["benchmark_nav"] > 0
    assert row["holding_count"] == len(codes) and row["invested"] > 0
    text = render_summary(ledger, today=day)
    assert "累计收益" in text and "同期基准" in text and "仓位" in text
    assert "暂缺" not in text and "—" not in text
    assert date.fromisoformat(state["baseline_date"]) < day
    print(text)
