"""执行检查只读取实际账户持仓及当日候选。"""

import pandas as pd

from src.quality_pool import assembly, feeds
from test_quality_pool_state import env, _patch_execute_data


def test_snapshot_uses_account_cash_budget_and_only_current_codes(env):
    px = {"600001": 10., "600002": 20., "600099": 99.}
    _patch_execute_data(env, px, suspended={"600001"})
    requested = []

    def prices(codes):
        requested.extend(codes)
        return {c: px[c] for c in codes}

    env.setattr(feeds, "fetch_realtime_prices", prices)
    facts = {"positions": [{"code": "600001", "count": 500, "avail_count": 300}],
             "cash": 1000., "budget": 20000.}
    snap = assembly.build_snapshot("2026-10-02", ["600002"], account_facts=facts)

    assert requested == ["600001", "600002"]
    assert snap.counts == {"600001": 500} and snap.avail == {"600001": 300}
    assert snap.cash == 1000. and snap.equity == 20000.
    assert snap.suspended["600001"] and not snap.suspended["600002"]
