# -*- coding: utf-8 -*-
"""ETF 跨资产动量轮动：判定核纯函数 + 研究池一致性 + 入口台账闭环。

规则单点（src/etf/rotation.py，研究线 unified_coverage.py）：
- 打分 = 25 根已完成日线收盘 + 当日现价共 26 点的对数价格加权回归
  （w=linspace(1,2)）→ 年化 = exp(slope×250)−1，score = 年化 × 加权 R²；
- 合格候选 0 < score < 5（严格两端）；近 3 个日环比任一 <0.95 记 0；
- 单只全仓：空仓买合格第一名；持仓=第一名不动；否则先卖后买；
  无合格候选清仓，空仓则持有现金；
- 入口 etf_rotation.run：独立台账（account=etf_rotation），dry-run 不记账，
  取数故障 fail-closed（不清仓、不记账）。

测试风格对齐 tests/test_industry_momentum.py：纯函数优先、造数注入；
入口集成用 tmp_path 独立台账，monkeypatch 截面取数，禁止真实网络。
"""
import ast
import math
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import etf_rotation as entry
from src.etf import rotation as rot
from src.etf.rotation import (
    ACCOUNT, POOL, RotationRow,
    affordable_shares, build_rows, is_eligible, momentum_score,
    plan_rotation, top_candidate,
)
from src.trade_ledger import TradeRecord, append_trade, load_trades

# ══════════════ 造数助手 ══════════════

DECISION = date(2026, 9, 22)          # 2026-09-22 周二（决策日）
PREV = date(2026, 9, 21)              # 上一交易日（周一）
END = PREV.isoformat()
GEO_STEP = 1.001


def _geo(n: int = 25, step: float = GEO_STEP, base: float = 10.0) -> list:
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


# ══════════════ momentum_score：消费侧口径 ══════════════
# 引擎公式直测见 tests/test_etf_momentum.py（趋势分单点，对拍测试已随之退役）。

class TestMomentumScore:
    @pytest.mark.parametrize("step", [1.002, 1.001, 0.999])
    def test_geometric_series_r2_one_matches_exp_formula(self, step):
        """等比价格对数线性 → R²=1，分数精确等于 exp(slope×250)−1。"""
        closes = _geo(25, step)
        last = closes[-1] * step                    # 第 26 点继续等比
        score = momentum_score(closes, last)
        assert score == pytest.approx(step ** 250 - 1.0, rel=1e-9)

    def test_insufficient_bars_returns_nan(self):
        assert math.isnan(momentum_score(_geo(24), 10.0))       # 24+1 < 26 点
        assert math.isnan(momentum_score([], 10.0))
        # 边界：25 根历史 + 当日现价 = 26 点，够数
        assert math.isfinite(momentum_score(_geo(25), 10.0 * GEO_STEP ** 25))

    def test_nan_and_nonpositive_prices_return_nan(self):
        closes = _geo(25)
        for tail in (closes[:-1] + [float("nan")],   # 历史含 nan
                     closes[:-1] + [0.0],            # 历史含 0
                     closes[:-1] + [-1.0]):          # 历史含负价
            assert math.isnan(momentum_score(tail, closes[-1]))
        for last in (float("nan"), 0.0, -3.0,
                     float("inf"), float("-inf")):
            assert math.isnan(momentum_score(closes, last))

    def test_any_of_recent_3_days_dive_clears_to_zero(self):
        """近 3 个日环比（今日、昨日、前日的环比）任一 <0.95 → score=0。"""
        step = GEO_STEP

        # ① 今日跳水 -10%
        closes = _geo(25)
        assert momentum_score(closes, closes[-1] * 0.90) == 0.0

        # ② 昨日环比 -6%：随后今日恢复等比
        closes = _geo(25)
        closes[-1] = closes[-2] * 0.94
        assert momentum_score(closes, closes[-1] * step) == 0.0

        # ③ 前日环比 -6%：后两日继续等比（避免再触发其它日）
        closes = _geo(25)
        closes[-2] = closes[-3] * 0.94
        closes[-1] = closes[-2] * step
        assert momentum_score(closes, closes[-1] * step) == 0.0

        # 恰好 0.96（不 <0.95）不清零：分数有限且非 0
        closes = _geo(25)
        closes[-2] = closes[-3] * 0.96
        score = momentum_score(closes, closes[-1] * step)
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
        """决策日 bar 不进 25 根历史窗口；进窗口会给出完全不同的分数。"""
        closes = _geo(25)
        bars = {"510001": _dt_bars(closes)}
        bars["510001"].loc[pd.Timestamp(DECISION)] = 1000.0   # 决策日盘中 bar
        price = closes[-1] * GEO_STEP                          # 第 26 点

        rows, diag = build_rows(bars, {"510001": price},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))

        assert len(rows) == 1
        row = rows[0]
        assert row.close == pytest.approx(closes[-1])          # 最后一根历史日线
        assert row.price == pytest.approx(price)
        # 26 点全等比 → R²=1；若把决策日 1000 收盘算进历史，此断言必失败
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
        closes = _geo(25)
        bars = {"510001": _dt_bars(closes)}
        bars["510001"].loc[pd.Timestamp(DECISION)] = 12.5

        rows, diag = build_rows(bars, {}, decision_date=DECISION,
                                prev_trading_day=PREV, pool=_pool("510001"))
        assert rows[0].price == pytest.approx(12.5)            # 当日 bar 收盘
        assert rows[0].close == pytest.approx(closes[-1])
        assert diag["realtime_missing"] == 1

    def test_missing_price_falls_back_to_latest_close(self):
        closes = _geo(25)
        rows, diag = build_rows({"510001": _dt_bars(closes)}, {},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))
        assert rows[0].price == pytest.approx(closes[-1])      # 最近已完成收盘
        assert rows[0].price == pytest.approx(rows[0].close)
        assert diag["realtime_missing"] == 1

    def test_valid_price_not_counted_as_missing(self):
        closes = _geo(25)
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
        bars = {"510001": _dt_bars(_geo(25), end="2026-09-18")}   # 缺 09-21
        rows, diag = build_rows(bars, {"510001": 10.0},
                                decision_date=DECISION,
                                prev_trading_day=PREV,
                                pool=_pool("510001"))
        assert rows == []
        assert diag["skipped"]["stale"] == 1

    def test_no_prev_trading_day_uses_short_and_natural_day_stale(self):
        """日历不可用（prev=None）：不足 25 根记 short，距今 >10 自然日记 stale。"""
        bars = {
            "510001": _dt_bars(_geo(24)),                     # 24 根 → short
            "510002": _dt_bars(_geo(25), end="2026-08-20"),   # 33 天前 → stale
        }
        rows, diag = build_rows(bars, {}, decision_date=DECISION,
                                prev_trading_day=None,
                                pool=_pool("510001", "510002"))
        assert rows == []
        assert diag["skipped"]["short"] == 1
        assert diag["skipped"]["stale"] == 1

    def test_every_skip_reason_counted(self):
        bad_close = _dt_bars(_geo(25))
        bad_close.iloc[-1, 0] = 0.0
        duplicate = pd.concat([_dt_bars(_geo(25)), _dt_bars(_geo(25)).tail(1)])
        healthy = _dt_bars(_geo(25))
        bars = {
            "510001": _dt_bars(_geo(25), end="2026-09-18"),   # stale
            "510002": _dt_bars(_geo(24)),                     # short
            "510003": bad_close,                              # bad_close
            "510005": duplicate,                              # duplicate
            "510006": healthy,                                # 健康
            # 510004 缺日线 → no_bars
        }
        pool = _pool("510001", "510002", "510003", "510004", "510005", "510006")
        prices = {"510006": _geo(25)[-1] * GEO_STEP}
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
            closes = _geo(25, drift)
            bars[code] = _dt_bars(closes)
            prices[code] = closes[-1] * drift             # 26 点继续等比

        # 跳水过滤 → score=0（ineligible）
        dive = _geo(25)
        bars["510004"] = _dt_bars(dive)
        prices["510004"] = dive[-1] * 0.90
        # 阴跌 → 负分（ineligible，但非跳水清零）
        down = _geo(25, 0.998)
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
        assert top_candidate(rows) is ranked[0]
        assert top_candidate(rows).code == "510001"

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
        monkeypatch.setattr(rot, "momentum_score", lambda tail, last: score)
        rows, _ = build_rows({"510001": _dt_bars(_geo(25))},
                             {"510001": 10.0},
                             decision_date=DECISION,
                             prev_trading_day=PREV,
                             pool=_pool("510001"))
        assert rows[0].eligible is expected_eligible
        assert rows[0].rank == (1 if expected_eligible else 0)
        if expected_eligible:
            assert top_candidate(rows) is rows[0]
        else:
            assert top_candidate(rows) is None


# ══════════════ 研究池一致性（ast 解析 unified_coverage.py） ══════════════

def test_pool_matches_research_unified_coverage():
    study = (Path(__file__).resolve().parents[1]
             / "research/studies/etf_rotation/unified_coverage.py")
    tree = ast.parse(study.read_text(encoding="utf-8"))

    values = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Attribute) and t.attr == "etf_pool"
                        for t in node.targets)):
            values = ast.literal_eval(node.value)
            break
    assert values, "研究线 unified_coverage.py 中未找到 self.etf_pool 列表"

    research_codes = [str(code).split(".")[0] for code in values]
    engine_codes = [code for code, _, _ in POOL]

    # 有序一致性：顺序变化也算池变更（须同步研究记录）
    assert research_codes == engine_codes
    assert len(engine_codes) == len(set(engine_codes)) == 15
    assert all(len(code) == 6 and code.isdigit() for code in engine_codes)
    assert {group for _, _, group in POOL} == {"国内权益", "海外权益", "商品", "利率"}


# ══════════════ plan_rotation：单只全仓换仓 ══════════════

class TestPlanRotation:
    TARGET_PRICE = 4.0
    OTHER_PRICE = 2.0

    def _rows(self):
        return _ranked_rows(_row("510300", 0.40, self.TARGET_PRICE, name="沪深300"),
                            _row("513100", 0.30, self.OTHER_PRICE, name="纳指100"))

    def test_flat_account_buys_top_with_all_cash(self):
        rows = self._rows()
        plan = plan_rotation(rows, [], {"510300": self.TARGET_PRICE}, 1_000_000.0)

        assert plan["target"].code == "510300"
        assert plan["holding"] is None
        assert plan["sells"] == []
        assert [o.action for o in plan["orders"]] == ["buy"]
        buy = plan["buys"][0]
        assert buy.code == "510300" and buy.price == self.TARGET_PRICE
        assert buy.shares == affordable_shares(1_000_000.0, self.TARGET_PRICE)
        assert buy.shares == 249_900
        assert buy.amount == pytest.approx(buy.shares * self.TARGET_PRICE)
        assert plan["notes"] == []

    def test_holding_is_top_candidate_no_orders(self):
        rows = self._rows()
        plan = plan_rotation(rows, [_position("510300", price=self.TARGET_PRICE)],
                             {"510300": self.TARGET_PRICE}, 400.0)

        assert plan["target"].code == "510300"
        assert plan["holding"]["code"] == "510300"
        assert plan["orders"] == [] and plan["sells"] == [] and plan["buys"] == []
        assert any("仍为合格候选第一名" in note for note in plan["notes"])

    def test_switch_sells_first_then_buys_with_proceeds(self):
        rows = self._rows()
        avail_cash = 10_000.0
        held = _position("513100", count=1000, price=self.OTHER_PRICE, name="纳指100")
        plan = plan_rotation(rows, [held],
                             {"513100": self.OTHER_PRICE,
                              "510300": self.TARGET_PRICE}, avail_cash)

        assert [o.action for o in plan["orders"]] == ["sell", "buy"]
        assert plan["sells"] == plan["orders"][:1]
        assert plan["buys"] == plan["orders"][1:]

        sell = plan["sells"][0]
        assert sell.code == "513100"
        assert sell.shares == 1000 and sell.price == self.OTHER_PRICE
        assert sell.amount == pytest.approx(2_000.0)

        buy = plan["buys"][0]
        assert buy.code == "510300"
        # 买入现金 = 原有现金 + 卖出回款
        assert buy.shares == affordable_shares(avail_cash + sell.amount,
                                               self.TARGET_PRICE)
        assert buy.shares == 2_900
        assert buy.amount == pytest.approx(2_900 * self.TARGET_PRICE)

    def test_sell_is_floored_to_whole_lot(self):
        rows = self._rows()
        plan = plan_rotation(rows, [_position("513100", count=150, price=self.OTHER_PRICE)],
                             {"513100": self.OTHER_PRICE,
                              "510300": self.TARGET_PRICE}, 0.0)
        sells = plan["sells"]
        assert len(sells) == 1 and sells[0].shares == 100

    def test_no_eligible_candidate_clears_position(self):
        rows = _ranked_rows(_row("510300", float("nan")))
        plan = plan_rotation(rows, [_position("510300", count=1000, price=4.0)],
                             {"510300": 4.0}, 0.0)

        assert plan["target"] is None
        assert [o.action for o in plan["orders"]] == ["sell"]
        sell = plan["orders"][0]
        assert sell.code == "510300" and sell.shares == 1000
        assert sell.price == 4.0
        assert "无合格候选，空仓" in sell.reason
        assert plan["buys"] == []

    def test_no_eligible_candidate_flat_account_keeps_cash(self):
        rows = _ranked_rows(_row("510300", 0.0))          # 跳水 → 不合格
        plan = plan_rotation(rows, [], {}, 1_000_000.0)

        assert plan["target"] is None
        assert plan["orders"] == []
        assert any("无合格候选，保持空仓" in note for note in plan["notes"])

    def test_holding_without_price_emits_no_sell_and_notes_it(self):
        rows = self._rows()
        held = _position("513100", price=0.0)             # 现价缺失且快照也没有
        plan = plan_rotation(rows, [held], {}, 10_000.0)

        assert plan["sells"] == []
        assert all(o.action != "sell" for o in plan["orders"])
        assert any("缺现价" in note and "无法卖出" in note
                   for note in plan["notes"])

    def test_positions_of_other_accounts_are_ignored(self):
        rows = self._rows()
        # 核心仓/卫星仓持仓（account 非 etf_rotation）不参与本策略
        others = [_position("513100", price=self.OTHER_PRICE, account="core"),
                  _position("510300", price=self.TARGET_PRICE, account="satellite")]
        plan = plan_rotation(rows, others,
                             {"510300": self.TARGET_PRICE,
                              "513100": self.OTHER_PRICE}, 1_000_000.0)

        assert plan["holding"] is None
        assert plan["sells"] == []
        assert [o.action for o in plan["orders"]] == ["buy"]
        assert plan["buys"][0].code == "510300"
        assert plan["buys"][0].shares == affordable_shares(1_000_000.0,
                                                           self.TARGET_PRICE)

    def test_multiple_holdings_warn_and_sell_all_then_buy(self):
        rows = self._rows()
        positions = [_position("513100", count=1000, price=self.OTHER_PRICE),
                     _position("159915", count=1000, price=3.0)]
        plan = plan_rotation(rows, positions,
                             {"513100": self.OTHER_PRICE, "159915": 3.0,
                              "510300": self.TARGET_PRICE}, 0.0)

        assert any("本策略应为单只" in note for note in plan["notes"])
        assert [o.code for o in plan["sells"]] == ["513100", "159915"]
        assert [o.action for o in plan["orders"]] == ["sell", "sell", "buy"]

    def test_unsellable_holding_blocks_new_buy(self):
        """旧持仓无法估价卖出时不能买入目标，否则同时持有两只（违反单只全仓）。"""
        rows = self._rows()
        held = _position("513100", price=0.0)   # 现价缺失且快照价也缺失
        plan = plan_rotation(rows, [held], {"510300": self.TARGET_PRICE},
                             1_000_000.0)

        assert plan["sells"] == [] and plan["buys"] == [] and plan["orders"] == []
        assert any("本次不买入" in note for note in plan["notes"])

    def test_holding_top_with_extra_position_sells_only_extra(self):
        """持仓已第一名但台账异常多一只：只卖多余持仓，不买、不动目标数量。"""
        rows = self._rows()
        target = _position("510300", price=self.TARGET_PRICE)
        extra = _position("159915", count=1000, price=3.0)
        plan = plan_rotation(rows, [target, extra],
                             {"510300": self.TARGET_PRICE, "159915": 3.0}, 0.0)

        assert plan["holding"]["code"] == "510300"
        assert plan["buys"] == []
        assert [o.code for o in plan["sells"]] == ["159915"]
        assert any("非目标持仓" in note for note in plan["notes"])


# ══════════════ 入口集成：独立台账闭环（monkeypatch 取数，禁网络） ══════════════

DECISION_STR = DECISION.isoformat()
ENTRY_DIAG = {
    "decision_date": DECISION_STR,
    "prev_trading_day": PREV.isoformat(),
    "pool": 15, "n": 2, "eligible": 2, "realtime_missing": 0,
    "skipped": {"no_bars": 0, "short": 0, "stale": 0,
                "bad_close": 0, "duplicate": 0},
}


def _entry_rows():
    return [
        RotationRow(code="510300", name="沪深300", group="国内权益",
                    close=4.0, price=4.0, score=0.40, eligible=True, rank=1),
        RotationRow(code="513100", name="纳指100", group="海外权益",
                    close=2.0, price=2.0, score=0.30, eligible=True, rank=2),
    ]


@pytest.fixture
def entry_env(monkeypatch, tmp_path):
    """入口运行环境：独立 tmp 台账 + 固定截面（不触网）。"""
    ledger = tmp_path / "trade_ledger_etf_rotation.jsonl"
    monkeypatch.setattr(entry, "LEDGER_PATH", ledger)
    # run() 仅在持仓缺价时才调用 resolve_prices（真实实现会取数）——测试里禁网
    monkeypatch.setattr(entry, "resolve_prices", lambda codes: {})

    state = {"rows": _entry_rows(),
             "prices": {"510300": 4.0, "513100": 2.0}}
    calls = []

    def fake_snapshot(decision_date=None):
        calls.append(decision_date)
        diag = dict(ENTRY_DIAG,
                    n=len(state["rows"]),
                    eligible=sum(1 for r in state["rows"] if r.eligible))
        return state["rows"], dict(state["prices"]), diag

    monkeypatch.setattr(entry, "rotation_snapshot", fake_snapshot)
    return SimpleNamespace(ledger=ledger, state=state, calls=calls)


class TestEntryIntegration:
    def test_dry_run_reports_plan_without_touching_ledger(self, entry_env):
        report = entry.run(dry_run=True, trade_date=DECISION_STR)

        assert entry_env.calls == [DECISION]              # 决策日按 trade_date 传递
        assert not entry_env.ledger.exists()              # 台账零写入
        assert "dry-run" in report
        assert "试运行，未记账" in report
        assert "## 执行明细" in report
        assert "[DRY] BUY 沪深300(510300) 249900股" in report
        assert "已记账" not in report

    def test_real_run_records_one_buy_into_strategy_ledger(self, entry_env):
        report = entry.run(dry_run=False, trade_date=DECISION_STR)

        trades = load_trades(entry_env.ledger)
        assert len(trades) == 1
        trade = trades[0]
        assert trade.date == DECISION_STR
        assert trade.side == "buy"
        assert trade.code == "510300"
        assert trade.name == "沪深300"
        assert trade.account == ACCOUNT
        assert trade.qty == affordable_shares(1_000_000.0, 4.0) == 249_900
        assert trade.price == 4.0

        assert "## 执行明细" in report
        assert "✅ BUY 沪深300(510300) 249900股" in report
        assert "已记账 1 笔（买入 1，卖出 0）" in report
        assert "执行后持仓" in report
        assert "沪深300(510300) 249900股" in report

    def test_repeat_run_same_day_does_not_buy_again(self, entry_env):
        entry.run(dry_run=False, trade_date=DECISION_STR)
        before = load_trades(entry_env.ledger)
        assert len(before) == 1

        report = entry.run(dry_run=False, trade_date=DECISION_STR)

        assert load_trades(entry_env.ledger) == before    # 无新增流水
        assert "无调仓指令" in report
        assert "仍为合格候选第一名" in report
        assert "已记账 0 笔（买入 0，卖出 0）" in report

    def test_no_eligible_candidate_flat_account_writes_nothing(self, entry_env):
        entry_env.state["rows"] = [
            RotationRow(code="510300", name="沪深300", group="国内权益",
                        close=4.0, price=4.0, score=0.0,
                        eligible=False, rank=0),
        ]
        report = entry.run(dry_run=False, trade_date=DECISION_STR)

        assert not entry_env.ledger.exists()              # 空仓无指令 → 零写入
        assert "无合格候选 → 空仓" in report
        assert "无调仓指令" in report
        assert "保持空仓" in report

    def test_no_rows_fail_closed_keeps_existing_ledger(self, entry_env):
        old = TradeRecord(date="2026-09-18", code="513100", name="纳指100",
                          side="buy", qty=1000, price=2.0, account=ACCOUNT)
        append_trade(old, path=entry_env.ledger)
        before = load_trades(entry_env.ledger)

        entry_env.state["rows"] = []
        entry_env.state["prices"] = {"513100": 2.0}
        report = entry.run(dry_run=False, trade_date=DECISION_STR)

        assert load_trades(entry_env.ledger) == before    # 不清仓、不记账
        assert "## ⛔ 本次不执行" in report
        assert "fail-closed" in report

    def test_holding_missing_price_fail_closed_keeps_ledger(self, entry_env):
        old = TradeRecord(date="2026-09-18", code="513100", name="纳指100",
                          side="buy", qty=1000, price=2.0, account=ACCOUNT)
        append_trade(old, path=entry_env.ledger)
        before = load_trades(entry_env.ledger)

        # 持仓码不在截面现价里，resolve_prices 兜底也被禁用 → 持仓现价缺失
        entry_env.state["prices"] = {}
        report = entry.run(dry_run=False, trade_date=DECISION_STR)

        assert load_trades(entry_env.ledger) == before
        assert "## ⛔ 本次不执行" in report
        assert "持仓现价缺失" in report
