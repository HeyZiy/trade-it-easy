# -*- coding: utf-8 -*-
"""卫星仓日频截面轮动场景测试：判定核零取数，摆场景注入。

规则单点（strategy/industry_momentum.md v3_1 口径）：
- 池 = 动态规则池（513/词表/成熟/流动/相关去重），每 20 交易日重建；
- 打分 = 25 根收盘 + 当日现价的对数加权回归（年化 × R²，跳水清零）；
- 买 = score>0 且拥挤度 <90（缺数据放行）降序补空槽等权；
- 卖 = 评分排名跌出前 40%；无绝对收益止损、无市场门控。
"""
from datetime import date

import pandas as pd
import pytest

from src.etf import industry_momentum as im
from src.etf.industry_momentum import (
    TOPN, RotationRow,
    build_buy_orders, build_pool, build_rows,
    build_sell_orders, filter_by_name, momentum_score,
)
from data_provider.bars import adjust_series  # 复权单点已迁 bars
from src.mx.executor import round_lot


# ══════════════ 造数助手 ══════════════

END = "2026-09-22"


def _rows(n: int = 10, **overrides) -> list:
    """n 只健康截面：rank=i（1 最强），score 递减，全部通过准入。"""
    rows = []
    for i in range(1, n + 1):
        kw = dict(code=f"{510000 + i:06d}", name=f"行业{i}",
                  close=10.0, price=10.0, score=round(10.0 - i, 3),
                  rank=i, n=n, crowd=50.0)
        if i == 1:
            kw.update(overrides)
        rows.append(RotationRow(**kw))
    return rows


def _pos(code: str, count: int = 1000, price: float = 10.0) -> dict:
    return {"code": code, "name": f"ETF{code}", "count": count,
            "current_price": price, "cost_price": price,
            "market_value": count * price}


def _up_closes(n: int, base: float = 10.0, step: float = 1.001) -> list:
    """n 根等比上行收盘（走得直 → 高 R² 正分）。"""
    return [base * step ** k for k in range(n)]


def _bars(closes: list, amounts: list = None, end: str = END) -> pd.DataFrame:
    """日线 DataFrame（index=date_str，columns=[close, amount]）；周末端点回退交易日。"""
    end_ts = pd.Timestamp(end)
    if end_ts.weekday() >= 5:                      # 周末 → 回退到上一个周五
        end_ts -= pd.Timedelta(days=end_ts.weekday() - 4)
    idx = pd.bdate_range(end=end_ts, periods=len(closes)).strftime("%Y-%m-%d")
    amt = amounts if amounts is not None else [60_000_000.0] * len(closes)
    return pd.DataFrame({"close": closes, "amount": amt}, index=idx)


def _mature_bars(n: int = 300, end: str = END, drift: float = 1.001,
                 amount: float = 60_000_000.0) -> pd.DataFrame:
    """n 根缓升日线（300 根 ≈ 14 个月 → 首根距 END 过成熟门槛）。"""
    return _bars(_up_closes(n, step=drift), amounts=[amount] * n, end=end)


# ══════════════ momentum_score：加权回归打分 ══════════════

class TestMomentumScore:
    def test_straight_uptrend_positive(self):
        closes = _up_closes(25)
        s = momentum_score(closes, closes[-1] * 1.001)
        # 年化 1.001^250-1 ≈ 28.4% × R²≈1
        assert 0.25 < s < 0.35

    def test_dive_clears_score(self):
        closes = _up_closes(25)
        assert momentum_score(closes, closes[-1] * 0.90) == 0.0   # 当日跳水 -10%

    def test_recent_3d_dive_clears(self):
        closes = _up_closes(25)[:-3] + [10.0, 9.4, 8.9]           # 近 3 日环比 <0.95
        assert momentum_score(closes, 8.9) == 0.0

    def test_insufficient_bars_zero(self):
        assert momentum_score(_up_closes(24), 10.0) == 0.0        # <26 点

    def test_bad_price_zero(self):
        closes = _up_closes(25)
        assert momentum_score(closes, 0.0) == 0.0
        assert momentum_score(closes, float("nan")) == 0.0

    def test_negative_score_preserved(self):
        """负分=下降趋势：清零只给跳水，阴跌保留负分供排序。"""
        closes = [10.0 * 0.999 ** k for k in range(25)]
        s = momentum_score(closes, closes[-1] * 0.999)
        assert s < 0.0


# ══════════════ adjust_series：份额折算前复权 ══════════════

class TestAdjustSeries:
    def test_split_continuity(self):
        """1:2 拆分（10→5）：此前价格全乘 0.5，调整后无假跳水。"""
        closes = [10.0, 10.1, 10.2, 10.1, 10.0, 5.0, 5.05, 5.1]
        adj = adjust_series(pd.Series(closes))
        assert abs(adj.iloc[-1] - 5.1) < 1e-9          # 末段不动
        assert abs(adj.iloc[4] - 5.0) < 1e-9           # 事件前一日 10.0×0.5
        rets = adj.pct_change().dropna()
        assert (rets.abs() <= 0.25).all()              # 折算缺口消失

    def test_no_event_unchanged(self):
        closes = _up_closes(30)
        adj = adjust_series(pd.Series(closes))
        assert abs(adj.iloc[-1] - closes[-1]) < 1e-9


# ══════════════ filter_by_name：513 前缀 + 词表 ══════════════

class TestFilterByName:
    def _run(self, *pairs):
        return [e["code"] for e in filter_by_name(
            [{"code": c, "name": n} for c, n in pairs])]

    def test_cross_border_prefix_and_words(self):
        kept = self._run(("513050", "中概互联网ETF"), ("513100", "纳指ETF"),
                         ("159941", "纳指ETF"), ("513180", "恒生科技ETF"),
                         ("512480", "半导体ETF"))
        assert kept == ["512480"]              # 513 前缀 + 跨境词全剔

    def test_bond_gold_wordlist(self):
        kept = self._run(("511260", "十年国债ETF"), ("518800", "黄金ETF"),
                         ("518880", "上海金ETF"), ("512400", "有色金属ETF"),
                         ("560010", "稀有金属ETF"))
        assert kept == ["512400", "560010"]    # 单字'债'/'上海金'不误杀稀有金属

    def test_broad_base_words(self):
        kept = self._run(("563360", "A500ETF基金"), ("515180", "红利ETF"),
                         ("159949", "创业板50ETF"), ("512880", "证券ETF"))
        assert kept == ["512880"]

    def test_code_zfilled(self):
        out = filter_by_name([{"code": 512880, "name": "证券ETF"}])
        assert out == [{"code": "512880", "name": "证券ETF"}]


# ══════════════ build_pool：成熟/流动/相关去重 ══════════════

class TestBuildPool:
    def test_maturity_liquidity_gates(self):
        cands = [{"code": "510001", "name": "短史"},
                 {"code": "510002", "name": "稀薄"},
                 {"code": "510003", "name": "成熟"}]
        bars = {"510001": _mature_bars(200),                        # <250 根
                "510002": _mature_bars(300, amount=40_000_000.0),   # 均额 <5000 万
                "510003": _mature_bars(300)}
        members, stats = build_pool(cands, bars, as_of=END)
        assert [m["code"] for m in members] == ["510003"]
        assert stats == {"cands": 3, "mature": 2, "liquid": 1, "members": 1}

    def test_first_bar_maturity(self):
        """K 线根数够但首根太近（上市不足 365 自然日）→ 剔除。"""
        young = _bars(_up_closes(252), end=END)     # 252 交易日 ≈ 352 自然日
        members, _ = build_pool([{"code": "510009", "name": "次新"}],
                                {"510009": young}, as_of=END)
        assert members == []

    def test_corr_dedup_keeps_most_liquid(self):
        closes = _up_closes(300)
        members, _ = build_pool(
            [{"code": "510001", "name": "同暴露A"}, {"code": "510002", "name": "同暴露B"}],
            {"510001": _bars(closes, amounts=[10 ** 8] * 300),
             "510002": _bars(closes, amounts=[2 * 10 ** 8] * 300)}, as_of=END)
        assert [m["code"] for m in members] == ["510002"]   # corr=1.0 → 留流动性高者

    def test_nan_corr_kept(self):
        """相关性不可比（日期错开无交集）→ 视为不可比，保留。"""
        members, _ = build_pool(
            [{"code": "510001", "name": "甲"}, {"code": "510002", "name": "乙"}],
            {"510001": _mature_bars(300),
             "510002": _mature_bars(300, end="2024-06-30")}, as_of=END)
        assert {m["code"] for m in members} == {"510001", "510002"}


# ══════════════ build_rows：截面 + 拥挤度 ══════════════

class TestBuildRows:
    def test_rank_by_score_desc(self):
        pool = [{"code": "510001", "name": "强"}, {"code": "510002", "name": "弱"}]
        bars = {"510001": _mature_bars(300, drift=1.002),
                "510002": _mature_bars(300, drift=1.0005)}
        rows, diag = build_rows(pool, bars, {}, as_of=END)
        assert [r.code for r in rows] == ["510001", "510002"]
        assert rows[0].rank == 1 and rows[0].n == 2
        assert diag["n"] == 2 and diag["pool"] == 2

    def test_stale_short_no_bars_skipped(self):
        pool = [{"code": "510001", "name": "停牌"}, {"code": "510002", "name": "短史"},
                {"code": "510003", "name": "缺数"}, {"code": "510004", "name": "健康"}]
        bars = {"510001": _mature_bars(300, end="2026-08-01"),
                "510002": _mature_bars(200),
                "510004": _mature_bars(300)}
        rows, diag = build_rows(pool, bars, {}, as_of=END)
        assert [r.code for r in rows] == ["510004"]
        assert diag["skipped"] == {"no_bars": 1, "short": 1, "stale": 1, "bad_close": 0}

    def test_realtime_missing_fallback(self):
        bars = {"510001": _mature_bars(300)}
        rows, diag = build_rows([{"code": "510001", "name": "x"}], bars, {}, as_of=END)
        assert rows[0].price == pytest.approx(rows[0].close)
        assert diag["realtime_missing"] == 1

    def test_crowd_share_and_close_components(self):
        """份额升 → 占比分位登顶；镜像对手份额降 → 拥挤度低。"""
        n = 300
        amt_a = [10 ** 8 * (1 + k / n) for k in range(n)]
        amt_b = [10 ** 8 * (1 - k / n) for k in range(n)]
        pool = [{"code": "510001", "name": "A"}, {"code": "510002", "name": "B"}]
        bars = {"510001": _bars(_up_closes(n), amounts=amt_a),
                "510002": _bars(_up_closes(n), amounts=amt_b)}
        rows, _ = build_rows(pool, bars, {}, as_of=END)
        crowd = {r.code: r.crowd for r in rows}
        assert crowd["510001"] > 99.0              # 份额+价格双双登顶
        assert crowd["510002"] < 50.0

    def test_crowd_none_passes_gate(self):
        """份额分量缺观测（amount 全缺）→ 单分量 → crowd=None → 放行。"""
        idx = pd.bdate_range(end=END, periods=300).strftime("%Y-%m-%d")
        bars = {"510001": pd.DataFrame({"close": _up_closes(300),
                                        "amount": [float("nan")] * 300}, index=idx)}
        rows, _ = build_rows([{"code": "510001", "name": "x"}], bars, {}, as_of=END)
        assert rows[0].crowd is None
        assert rows[0].buy_allowed is True


# ══════════════ build_sell_orders：唯一退出规则 ══════════════

class TestSellOrders:
    def test_healthy_holding_no_exit(self):
        rows = _rows(10)                       # 退出线 = ceil(0.4×10) = 4
        orders, notes = build_sell_orders(rows, [_pos(rows[0].code)])
        assert orders == [] and notes == []

    def test_rank_out_of_top40_exits(self):
        rows = _rows(10)
        orders, notes = build_sell_orders(rows, [_pos(rows[4].code)])
        assert len(orders) == 1
        assert orders[0].action == "sell" and orders[0].shares == 1000
        assert "跌出前40%" in orders[0].reason
        assert "第5/10名" in orders[0].reason

    def test_held_outside_cross_section_untouched(self):
        """截面外持仓（停牌剔除/池重建后出池）没有排名就没有退出判定。"""
        rows = _rows(10)
        orders, _ = build_sell_orders(rows, [_pos("599999")])
        assert orders == []

    def test_zero_count_skipped(self):
        rows = _rows(10)
        orders, _ = build_sell_orders(rows, [_pos(rows[4].code, count=0)])
        assert orders == []

    def test_rank_only_no_absolute_stop(self):
        """退出无绝对收益止损：score 为负但仍在退出线内 → 持有不卖。"""
        rows = _rows(10)
        held_row = rows[2]
        held_row.score = -8.0
        orders, _ = build_sell_orders(rows, [_pos(held_row.code)])
        assert orders == []


def test_exit_rank_uses_ceil():
    """退出线 = ceil(0.4 × n)：n=5 → 2，第 3 名即跌出。"""
    rows = _rows(5)
    assert rows[0].exit_rank == 2
    orders, _ = build_sell_orders(rows, [_pos(rows[2].code)])
    assert len(orders) == 1


# ══════════════ build_buy_orders：准入 + 前 3 等权 ══════════════

class TestBuyOrders:
    def test_top3_equal_weight(self):
        rows = _rows(10)
        orders, notes = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert len(orders) == TOPN
        assert [o.code for o in orders] == [r.code for r in rows[:3]]
        # 每只 = min(20万/3, min(10万, 0+20万)/3) = 10万/3 → round_lot
        per = 100_000 / TOPN
        assert all(o.shares == round_lot(per / o.price) for o in orders)

    def test_zero_score_excluded(self):
        """score=0（跳水/数据失败）不买，顺延下一只。"""
        rows = _rows(10, score=0.0)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert rows[0].code not in {o.code for o in orders}
        assert len(orders) == TOPN

    def test_negative_score_excluded(self):
        rows = _rows(10, score=-3.0)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert len(orders) == TOPN and rows[0].code not in {o.code for o in orders}

    def test_crowd_lock_excludes_buy(self):
        rows = _rows(10, crowd=90.0)           # rank1 拥挤度 ≥90 → 禁买，顺延
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert rows[0].code not in {o.code for o in orders}
        assert len(orders) == TOPN

    def test_missing_data_passes(self):
        rows = _rows(10, crowd=None)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert len(orders) == TOPN

    def test_held_codes_fill_slots(self):
        rows = _rows(10)
        held = {rows[0].code, rows[1].code}
        orders, _ = build_buy_orders(rows, held, 1_000_000, 20_000, 200_000)
        assert [o.code for o in orders] == [rows[2].code]

    def test_slots_full_no_buys(self):
        rows = _rows(10)
        held = {r.code for r in rows[:3]}
        orders, notes = build_buy_orders(rows, held, 1_000_000, 30_000, 200_000)
        assert orders == [] and notes == []

    def test_held_outside_universe_no_slot(self):
        """池外旧持仓不占槽（无排名无法退出，也不应挤占轮动名额）。"""
        rows = _rows(10)
        orders, _ = build_buy_orders(rows, {"599999"}, 1_000_000, 0.0, 200_000)
        assert len(orders) == TOPN

    def test_budget_cap_no_buys(self):
        rows = _rows(10)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 100_000, 200_000)
        assert orders == []

    def test_sizing_capped_by_avail_cash(self):
        """可用现金 < 预算/3 时按现金均分。"""
        rows = _rows(10)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 6_000)
        per = 6_000 / TOPN
        assert all(o.shares == round_lot(per / o.price) for o in orders)

    def test_buy_price_is_realtime_quote(self):
        """成交价口径 = 当日现价（实时报价），不是最后收盘。"""
        rows = _rows(10)
        for r in rows:
            r.price = 10.5
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert all(o.price == 10.5 for o in orders)


# ══════════════ 池快照与重建节奏 ══════════════

class TestPoolState:
    def test_empty_state_needs_rebuild(self):
        assert im.pool_needs_rebuild({}) is True
        assert im.pool_needs_rebuild({"members": []}) is True

    def test_trading_day_cadence(self, monkeypatch):
        state = {"rebuilt_on": "2026-09-01", "members": [{"code": "512880"}]}
        monkeypatch.setattr(im, "get_trading_dates",
                            lambda s, e: [date(2026, 9, d) for d in range(2, 22)])
        assert im.pool_needs_rebuild(state, date(2026, 9, 22)) is True    # 满 20 交易日
        monkeypatch.setattr(im, "get_trading_dates",
                            lambda s, e: [date(2026, 9, d) for d in range(2, 21)])
        assert im.pool_needs_rebuild(state, date(2026, 9, 22)) is False   # 19 个

    def test_calendar_down_fallback(self, monkeypatch):
        """交易日历不可用 → 自然日 ≥28 天回退。"""
        state = {"rebuilt_on": "2026-09-10", "members": [{"code": "512880"}]}
        monkeypatch.setattr(im, "get_trading_dates", lambda s, e: [])
        assert im.pool_needs_rebuild(state, date(2026, 9, 30)) is False   # 20 天
        assert im.pool_needs_rebuild(state, date(2026, 10, 10)) is True   # 30 天

    def test_state_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(im, "POOL_STATE_PATH", tmp_path / "pool.json")
        assert im.load_pool_state() == {}
        im.save_pool_state([{"code": "512880", "name": "证券ETF"}], "2026-10-03")
        state = im.load_pool_state()
        assert state["rebuilt_on"] == "2026-10-03"
        assert state["members"] == [{"code": "512880", "name": "证券ETF"}]

    def test_bad_state_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(im, "POOL_STATE_PATH", tmp_path / "pool.json")
        tmp_path.joinpath("pool.json").write_text("{broken", encoding="utf-8")
        assert im.load_pool_state() == {}


# ══════════════ 判定核与常量的口径锚 ══════════════

def test_rules_match_v3_1_constants():
    """与 research/studies/industry_momentum/lM_v3_1.py 定稿常量一一对应（漂移即测试红）。"""
    assert im.TOPN == 3
    assert im.EXIT_RANK_PCT == 0.40
    assert im.CROWD_PCT_MAX == 90.0
    assert im.LIQ_AMT20_MIN == 50_000_000.0
    assert im.MATURE_DAYS == 365
    assert im.CORR_DEDUPE == 0.90
    assert im.REBUILD_EVERY == 20
    assert im.SCORE_DAYS == 25 and im.DIVE_RATIO == 0.95
    assert im.CROSS_BORDER_PREFIX == "513"
    assert im.EXCLUDE_KW[-2:] == ("债", "上海金")     # v3_1 单变量改动在位
    assert im.SATELLITE_BUDGET_RATIO == 0.10
