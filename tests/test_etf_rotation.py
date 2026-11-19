# -*- coding: utf-8 -*-
"""跨资产动量：判定核纯函数 + 入口台账闭环。

规则单点（src/etf/rotation.py）：
- 打分 = 30 根已完成日线收盘 + 当日现价共 31 点的对数价格加权回归
  （w=linspace(1,2)）→ 年化 = exp(slope×250)−1，score = 年化 × 加权 R²；
- 合格候选 0 < score < 5（严格两端）；近 3 个日环比任一 <0.95 记 0；
- 单只全仓：空仓买合格第一名；持仓=第一名不动；否则先卖后买；
  无合格候选清仓，空仓则持有现金；
- 入口 etf_rotation.run：独立台账（account=etf_rotation），dry-run 不记账，
  取数故障 fail-closed（不清仓、不记账）。

测试风格：纯函数优先、造数注入；
入口集成用 tmp_path 独立台账，monkeypatch 截面取数，禁止真实网络。
"""
import math
from datetime import date

import pandas as pd
import pytest

from src.etf import rotation as rot
from src.etf.rotation import (
    ACCOUNT, RotationRow,
    affordable_shares, build_rows, is_eligible, ensemble_score,
    plan_rotation, top_targets,
)

# ══════════════ 造数助手 ══════════════

DECISION = date(2026, 9, 22)          # 2026-09-22 周二（决策日）
PREV = date(2026, 9, 21)              # 上一交易日（周一）
END = PREV.isoformat()
GEO_STEP = 1.001


def _geo(n: int = 30, step: float = GEO_STEP, base: float = 10.0) -> list:
    """严格几何级数收盘（对数价格共线 → 加权 R²=1）。"""
    return [base * step ** k for k in range(n)]


def _dt_bars(closes, end: str = END) -> pd.DataFrame:
    """日线 DataFrame：DatetimeIndex 升序（_prepare_bars 的输入形状）。"""
    idx = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"close": list(closes)}, index=idx)


def _pool(*codes) -> tuple:
    """单类别测试池（顺序即报告顺序，排名由评分决定）。"""
    return tuple((code, f"ETF{code}", "国内权益") for code in codes)


def _row(code: str, score: float, price: float = 10.0, name: str = None) -> RotationRow:
    """手工截面行（plan_rotation 的输入形状）。"""
    row = RotationRow(code=code, name=name or f"ETF{code}", group="国内权益",
                      close=price, price=price, score=score)
    row.eligible = is_eligible(score)
    return row


def _ranked_rows(*rows) -> list:
    """按 build_rows 的排序口径排好 rank（合格在前按 score 降序）。"""
    eligible = [r for r in rows if r.eligible]
    eligible.sort(key=lambda r: (-r.score, r.code))
    for i, row in enumerate(eligible, 1):
        row.rank = i
    return eligible + [r for r in rows if not r.eligible]


def _position(code: str, count: int = 1000, price: float = 10.0,
              account: str = ACCOUNT, name: str = None) -> dict:
    """妙想同形持仓 dict（plan_rotation 的输入形状）。"""
    return {"code": code, "name": name or f"ETF{code}", "count": count,
            "current_price": price, "cost_price": price,
            "market_value": count * price, "account": account}


# ══════════════ ensemble_score：消费侧口径 ══════════════
# 引擎公式直测见 tests/test_etf_momentum.py（趋势分单点，对拍测试已随之退役）。

class TestMomentumScore:
    @pytest.mark.parametrize("step", [1.002, 1.001, 0.999])
    def test_geometric_series_r2_one_matches_exp_formula(self, step):
        """等比价格对数线性 → R²=1，分数精确等于 exp(slope×250)−1。"""
        closes = _geo(30, step)
        last = closes[-1] * step                    # 第 31 点继续等比
        score = ensemble_score(closes, last)
        assert score == pytest.approx(step ** 250 - 1.0, rel=1e-9)

    def test_four_windows_include_the_current_price_and_average(self, monkeypatch):
        samples = []

        def score(prices):
            samples.append(prices)
            return len(prices)

        closes = _geo()
        current = closes[-1] * GEO_STEP
        monkeypatch.setattr(rot, "weighted_trend_score", score)
        assert ensemble_score(closes, current) == 23.5
        assert samples == [closes[-window:] + [current] for window in (15, 20, 25, 30)]

    def test_insufficient_bars_returns_nan(self):
        assert math.isnan(ensemble_score(_geo(29), 10.0))       # 29+1 < 31 点
        assert math.isnan(ensemble_score([], 10.0))
        # 边界：30 根历史 + 当日现价 = 31 点，够数
        assert math.isfinite(ensemble_score(_geo(30), 10.0 * GEO_STEP ** 30))

    def test_nan_and_nonpositive_prices_return_nan(self):
        closes = _geo(30)
        for tail in (closes[:-1] + [float("nan")],   # 历史含 nan
                     closes[:-1] + [0.0],            # 历史含 0
                     closes[:-1] + [-1.0]):          # 历史含负价
            assert math.isnan(ensemble_score(tail, closes[-1]))
        for last in (float("nan"), 0.0, -3.0,
                     float("inf"), float("-inf")):
            assert math.isnan(ensemble_score(closes, last))

    def test_any_of_recent_3_days_dive_clears_to_zero(self):
        """近 3 个日环比（今日、昨日、前日的环比）任一 <0.95 → score=0。"""
        step = GEO_STEP

        # ① 今日跳水 -10%
        closes = _geo(30)
        assert ensemble_score(closes, closes[-1] * 0.90) == 0.0

        # ② 昨日环比 -6%：随后今日恢复等比
        closes = _geo(30)
        closes[-1] = closes[-2] * 0.94
        assert ensemble_score(closes, closes[-1] * step) == 0.0

        # ③ 前日环比 -6%：后两日继续等比（避免再触发其它日）
        closes = _geo(30)
        closes[-2] = closes[-3] * 0.94
        closes[-1] = closes[-2] * step
        assert ensemble_score(closes, closes[-1] * step) == 0.0

        # 恰好 0.96（不 <0.95）不清零：分数有限且非 0
        closes = _geo(30)
        closes[-2] = closes[-3] * 0.96
        score = ensemble_score(closes, closes[-1] * step)
        assert math.isfinite(score) and score != 0.0


# ══════════════ is_eligible：严格两端 ══════════════

class TestIsEligible:
    @pytest.mark.parametrize("score", [0.0, 5.0, float("nan"),
                                       float("inf"), float("-inf"),
                                       -0.0001, 5.0001])
    def test_out_of_range_rejected(self, score):
        assert is_eligible(score) is False

    @pytest.mark.parametrize("score", [0.0001, 4.9999])
    def test_inside_range_eligible(self, score):
        assert is_eligible(score) is True


# ══════════════ affordable_shares：整手 + 佣金预检 ══════════════

class TestAffordableShares:
    def test_commission_drags_one_lot_below_full_cash(self):
        # 10 万 @10 元：10000 股成本 10 万，万二佣金 20 元 > 5 元下限 →
        # 必须让出一手：9900 股 = 99000 + 19.8 元 ≤ 此金
        assert affordable_shares(100_000.0, 10.0) == 9_900
        # @1 元：整手股数受万二佣金侵蚀（不会把 10 万全部打满）
        assert affordable_shares(100_000.0, 1.0) == 99_900

    def test_min_commission_floor_on_small_purchase(self):
        # 一手 1000 元：佣金按最低 5 元计，1005 元刚好够，1004 元不够
        assert affordable_shares(1_005.0, 10.0) == 100
        assert affordable_shares(1_004.0, 10.0) == 0
        assert affordable_shares(1_010.0, 10.0) == 100

    def test_insufficient_for_one_lot_returns_zero(self):
        assert affordable_shares(999.0, 10.0) == 0
        assert affordable_shares(500.0, 10.0) == 0
        assert affordable_shares(0.0, 10.0) == 0

    @pytest.mark.parametrize("cash,price", [
        (0.0, 10.0), (-100.0, 10.0), (1_000.0, 0.0), (1_000.0, -1.0),
        (math.nan, 10.0), (1_000.0, math.nan),
        (math.inf, 10.0), (1_000.0, math.inf),
    ])
    def test_invalid_inputs_return_zero(self, cash, price):
        assert affordable_shares(cash, price) == 0

    @pytest.mark.parametrize("cash,price", [
        (1_000_000.0, 4.0), (100_000.0, 10.0), (100_000.0, 1.0),
        (5_000.0, 3.33), (1_005.0, 10.0),
    ])
    def test_result_is_max_affordable_whole_lot_after_commission(self, cash, price):
        qty = affordable_shares(cash, price)
        assert qty % 100 == 0
        cost = qty * price
        assert cost + max(5.0, cost * rot.COMMISSION) <= cash + 1e-9
        next_cost = (qty + 100) * price
        assert next_cost + max(5.0, next_cost * rot.COMMISSION) > cash + 1e-9


# ══════════════ build_rows：截面 + 回退 + 跳过计数 ══════════════

class TestBuildRows:
    def test_decision_day_bar_stays_out_of_history_window(self):
        """决策日 bar 不进 30 根历史窗口；进窗口会给出完全不同的分数。"""
        closes = _geo(30)
        bars = {"510001": _dt_bars(closes)}
        bars["510001"].loc[pd.Timestamp(DECISION)] = 1000.0   # 决策日盘中 bar
        price = closes[-1] * GEO_STEP                          # 第 31 点

        rows, diag = build_rows(bars, {"510001": price},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))

        assert len(rows) == 1
        row = rows[0]
        assert row.close == pytest.approx(closes[-1])          # 最后一根历史日线
        assert row.price == pytest.approx(price)
        # 31 点全等比 → R²=1；若把决策日 1000 收盘算进历史，此断言必失败
        assert row.score == pytest.approx(GEO_STEP ** 250 - 1.0, rel=1e-9)
        assert row.eligible is True and row.rank == 1
        assert diag == {
            "decision_date": DECISION.isoformat(),
            "prev_trading_day": PREV.isoformat(),
            "pool": 1, "n": 1, "eligible": 1, "realtime_missing": 0,
            "skipped": {"no_bars": 0, "short": 0, "stale": 0,
                        "bad_close": 0, "duplicate": 0},
        }

    def test_missing_price_falls_back_to_decision_bar_close(self):
        closes = _geo(30)
        bars = {"510001": _dt_bars(closes)}
        bars["510001"].loc[pd.Timestamp(DECISION)] = 12.5

        rows, diag = build_rows(bars, {}, decision_date=DECISION,
                                prev_trading_day=PREV, pool=_pool("510001"))
        assert rows[0].price == pytest.approx(12.5)            # 当日 bar 收盘
        assert rows[0].close == pytest.approx(closes[-1])
        assert diag["realtime_missing"] == 1

    def test_missing_price_falls_back_to_latest_close(self):
        closes = _geo(30)
        rows, diag = build_rows({"510001": _dt_bars(closes)}, {},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))
        assert rows[0].price == pytest.approx(closes[-1])      # 最近已完成收盘
        assert rows[0].price == pytest.approx(rows[0].close)
        assert diag["realtime_missing"] == 1

    def test_valid_price_not_counted_as_missing(self):
        closes = _geo(30)
        price = closes[-1] * GEO_STEP
        rows, diag = build_rows({"510001": _dt_bars(closes)},
                                {"510001": price},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))
        assert rows[0].price == pytest.approx(price)
        assert diag["realtime_missing"] == 0

    def test_last_bar_not_previous_trading_day_is_stale(self):
        """日历可用（给了 prev_trading_day）时，最后一根必须正好是上一交易日。"""
        bars = {"510001": _dt_bars(_geo(30), end="2026-09-18")}   # 缺 09-21
        rows, diag = build_rows(bars, {"510001": 10.0},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))
        assert rows == []
        assert diag["skipped"]["stale"] == 1

    def test_no_prev_trading_day_uses_short_and_natural_day_stale(self):
        """日历不可用（prev=None）：不足 30 根记 short，距今 >10 自然日记 stale。"""
        bars = {
            "510001": _dt_bars(_geo(29)),                     # 29 根 → short
            "510002": _dt_bars(_geo(30), end="2026-08-20"),   # 33 天前 → stale
        }
        rows, diag = build_rows(bars, {}, decision_date=DECISION,
                                prev_trading_day=None,
                                pool=_pool("510001", "510002"))
        assert rows == []
        assert diag["skipped"]["short"] == 1
        assert diag["skipped"]["stale"] == 1

    def test_every_skip_reason_counted(self):
        bad_close = _dt_bars(_geo(30))
        bad_close.iloc[-1, 0] = 0.0
        duplicate = pd.concat([_dt_bars(_geo(30)), _dt_bars(_geo(30)).tail(1)])
        healthy = _dt_bars(_geo(30))
        bars = {
            "510001": _dt_bars(_geo(30), end="2026-09-18"),   # stale
            "510002": _dt_bars(_geo(29)),                     # short
            "510003": bad_close,                              # bad_close
            "510005": duplicate,                              # duplicate
            "510006": healthy,                                # 健康
            # 510004 缺日线 → no_bars
        }
        pool = _pool("510001", "510002", "510003", "510004", "510005", "510006")
        prices = {"510006": _geo(30)[-1] * GEO_STEP}
        rows, diag = build_rows(bars, prices, decision_date=DECISION,
                                prev_trading_day=PREV, pool=pool)

        assert [r.code for r in rows] == ["510006"]
        assert diag["pool"] == 6 and diag["n"] == 1 and diag["eligible"] == 1
        assert diag["skipped"] == {"no_bars": 1, "short": 1, "stale": 1,
                                   "bad_close": 1, "duplicate": 1}

    def test_eligible_ranked_by_score_desc_ineligible_rank_zero(self):
        step = GEO_STEP
        drifts = {"510001": 1.003, "510002": 1.001, "510003": 1.0005}
        bars, prices = {}, {}
        for code, drift in drifts.items():
            closes = _geo(30, drift)
            bars[code] = _dt_bars(closes)
            prices[code] = closes[-1] * drift             # 31 点继续等比

        # 跳水过滤 → score=0（ineligible）
        dive = _geo(30)
        bars["510004"] = _dt_bars(dive)
        prices["510004"] = dive[-1] * 0.90
        # 阴跌 → 负分（ineligible，但非跳水清零）
        down = _geo(30, 0.998)
        bars["510005"] = _dt_bars(down)
        prices["510005"] = down[-1] * 0.998

        rows, diag = build_rows(bars, prices, decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool(*bars))
        assert diag["eligible"] == 3 and diag["n"] == 5

        ranked = [r for r in rows if r.eligible]
        assert [r.code for r in ranked] == ["510001", "510002", "510003"]
        assert [r.rank for r in ranked] == [1, 2, 3]
        assert ranked[0].score > ranked[1].score > ranked[2].score > 0
        assert top_targets(rows) == ranked[:2]

        assert rows[3].code == "510004"
        assert rows[3].score == 0.0
        assert rows[4].score < 0
        for row in rows[3:]:
            assert row.eligible is False and row.rank == 0

    @pytest.mark.parametrize("score,expected_eligible", [
        (5.0, False), (0.0, False), (4.9999, True), (-0.1, False),
    ])
    def test_score_at_filter_boundary_never_enters_candidates(
            self, monkeypatch, score, expected_eligible):
        """build_rows 用 is_eligible 严格判两端：恰为 5/0 不进候选。"""
        monkeypatch.setattr(rot, "ensemble_score", lambda tail, last: score)
        rows, _ = build_rows({"510001": _dt_bars(_geo(30))},
                             {"510001": 10.0},
                             decision_date=DECISION,
                             prev_trading_day=PREV,
                             pool=_pool("510001"))
        assert rows[0].eligible is expected_eligible
        assert rows[0].rank == (1 if expected_eligible else 0)
        if expected_eligible:
            assert top_targets(rows) == rows
        else:
            assert top_targets(rows) == []


# ══════════════ plan_rotation：单只全仓换仓 ══════════════

class TestPlanRotation:
    def test_two_candidates_split_available_budget_and_exclude_third(self):
        rows = _ranked_rows(_row("510300", 1, 2), _row("513100", .8, 2), _row("501018", .7, 2))
        plan = plan_rotation(rows, [], {}, 4000, budget=2000)
        assert plan["targets"] == ["510300", "513100"]
        assert [(order.code, order.shares) for order in plan["buys"]] == [
            ("510300", 400), ("513100", 400)]
        assert sum(order.amount + 5 for order in plan["buys"]) <= 2000

    def test_rank_swap_and_weight_drift_do_not_trade(self):
        rows = _ranked_rows(_row("513100", 3, 5), _row("510300", 1, 2))
        held = [_position("510300", count=200), _position("513100", count=1000)]
        plan = plan_rotation(rows, held, {"510300": 2, "513100": 5}, 1200)
        assert plan["orders"] == []

    def test_member_change_exits_and_trims_before_buying(self):
        rows = _ranked_rows(_row("510300", 1.2, 2), _row("513100", 1, 2))
        held = [_position("510300", 1500), _position("159915", 150),
                _position("513100", account="core")]
        plan = plan_rotation(rows, held, {"159915": 2, "510300": 2}, 700)
        assert [(order.action, order.code, order.shares) for order in plan["orders"]] == [
            ("sell", "510300", 600), ("sell", "159915", 150), ("buy", "513100", 900)]
        assert plan["sells"][1].reason == "已移出池子，兜底卖出"

    def test_only_one_eligible_allocates_all_available_cash(self):
        rows = _ranked_rows(_row("510300", 1, 2), _row("513100", 0, 2))
        buy, = plan_rotation(rows, [], {}, 2000)["orders"]
        assert (buy.code, buy.action, buy.shares) == ("510300", "buy", 900)

    def test_no_candidates_liquidates_all_including_odd_lots(self):
        rows = _ranked_rows(_row("510300", float("nan")))
        held = [_position("510300", 1050), _position("513100", 50)]
        plan = plan_rotation(rows, held, {}, 0)
        assert not plan["targets"] and not plan["buys"]
        assert [(order.code, order.shares) for order in plan["sells"]] == [
            ("510300", 1050), ("513100", 50)]

    def test_holding_without_price_blocks_the_plan(self):
        rows = _ranked_rows(_row("510300", 1))
        plan = plan_rotation(rows, [_position("513100", price=0)], {}, 10000)
        assert not plan["orders"]
        assert "缺现价" in plan["issue"]


class TestConfigurablePool:
    def test_pool_from_config_requires_explicit_pool(self):
        with pytest.raises(ValueError, match="rotation.pool"):
            rot.pool_from_config({})
        config = {"rotation": {"pool": [{"code": "510300", "group": "国内权益"},
                                        {"code": "518880", "group": "商品"}]}}
        pool = rot.pool_from_config(config)
        assert [(code, group) for code, _, group in pool] == [
            ("510300", "国内权益"), ("518880", "商品")]

    def test_snapshot_uses_config_pool_and_prices_out_of_pool_holding(self, monkeypatch):
        """池外持仓不打分但一并取现价（兜底卖出用）；池内持仓不重复取价。"""
        from data_provider.etf import EASTMONEY, EtfBars

        bars = {"510300": EtfBars(_dt_bars(_geo()), EASTMONEY)}
        fetched = {}
        monkeypatch.setattr(rot, "latest_trading_day_on_or_before", lambda *a, **k: PREV)
        monkeypatch.setattr(rot, "fetch_bars",
                            lambda codes, **k: fetched.update(bars=list(codes)) or bars)
        monkeypatch.setattr(rot, "fetch_prices",
                            lambda codes: fetched.update(prices=list(codes))
                            or {code: 10.0 for code in codes})
        rows, prices, diag = rot.rotation_snapshot(
            DECISION, pool=_pool("510300"), held_codes=["513100", "510300"])
        assert fetched["bars"] == ["510300"]
        assert fetched["prices"] == ["510300", "513100"]
        assert [row.code for row in rows] == ["510300"]
        assert prices["513100"] == 10.0
        assert diag["pool"] == 1
        assert diag["bar_sources"] == {"510300": EASTMONEY}


# ══════════════ 入口集成：独立台账闭环（monkeypatch 取数，禁网络） ══════════════

DECISION_STR = DECISION.isoformat()
