# -*- coding: utf-8 -*-
"""卫星仓日频截面轮动场景测试：判定核零取数，摆场景注入。

规则单点（strategy/industry_momentum.md v3_1 口径）：
- 池 = 动态规则池（513/词表/成熟/流动/相关去重），每 20 交易日重建；
- 打分 = 25 根收盘 + 当日现价的对数加权回归（年化 × R²，跳水清零）；
- 买 = score>0 降序补空槽等权，量价热度阈值为 101 分，不限制正常评分；
- 卖 = 评分排名跌出前 40%；无绝对收益止损、无市场门控。
"""
from datetime import date
import logging

import pandas as pd
import pytest

from src.etf import industry_momentum as im
from src.etf.industry_momentum import (
    TOPN, RotationRow,
    build_buy_orders, build_pool, build_rows,
    build_sell_orders, filter_by_name, held_exit_facts, momentum_score,
)
from data_provider.bars import adjust_series  # 复权单点已迁 bars
from src.mx.executor import round_lot


@pytest.mark.parametrize("prices,missing", [
    ({}, 2),
    ({"510001": 11.0}, 1),
    ({"510001": 11.0, "510002": 11.0}, 0),
])
def test_snapshot_summarizes_actual_price_fallback(monkeypatch, caplog, prices, missing):
    members = [{"code": c, "name": c} for c in ("510001", "510002")]
    monkeypatch.setattr(im, "latest_trading_day_on_or_before", lambda today: date(2026, 9, 22))
    monkeypatch.setattr(im, "load_pool_state", lambda: {"members": members})
    monkeypatch.setattr(im, "pool_needs_rebuild", lambda state, today: False)
    monkeypatch.setattr(im, "fetch_bars", lambda codes: {c: _mature_bars() for c in codes})
    monkeypatch.setattr(im, "fetch_prices", lambda codes: prices)
    with caplog.at_level(logging.WARNING):
        rows, diag = im.rotation_snapshot()
    assert len(rows) == 2 and diag["realtime_missing"] == missing
    alerts = [r.getMessage() for r in caplog.records if "[行情降级]" in r.getMessage()]
    if missing:
        assert len(alerts) == 1
        assert f"{missing}/2" in alerts[0]
        assert "最新收盘价" in alerts[0] and "非实时" in alerts[0]
    else:
        assert alerts == []


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

    def test_money_market_words(self):
        # 2026-10-06 实盘建池测出的货币/短融漏网四只；'银行''金融'不受四词误杀
        kept = self._run(("511360", "短融ETF海富通"), ("511880", "银华日利ETF"),
                         ("511990", "华宝添益ETF"), ("159003", "招商快线ETF"),
                         ("512800", "银行ETF华宝"), ("510230", "金融ETF国泰"))
        assert kept == ["512800", "510230"]

    def test_gold_code_segment_hard_excluded(self):
        # 518 段全靠代码段剔：命名不含 '黄金'/'上海金' 的两只是实盘测出的漏网
        kept = self._run(("518600", "金ETF广发"), ("518680", "金ETF富国"),
                         ("518880", "黄金ETF华安"), ("516150", "稀土ETF嘉实"),
                         ("561360", "石油ETF国泰"))
        assert kept == ["516150", "561360"]

    def test_a50_broad_base_excluded(self):
        kept = self._run(("159595", "中证A50ETF大成"), ("512250", "A50ETF招商"),
                         ("512550", "富时A50ETF嘉实"), ("588230", "科创200ETF"),
                         ("159852", "软件ETF嘉实"))
        # '200' 词（EXCLUDE_KW 逗号修复后生效）连带剔科创200（宽基，剔得正确）
        assert kept == ["159852"]

    def test_commodity_futures_kept_by_ruling(self):
        # 2026-10-06 裁决：商品期货类 ETF 留在池内（能化/有色期货按行业暴露算）
        kept = self._run(("159981", "能源化工ETF建信"), ("159980", "有色ETF大成"),
                         ("512400", "有色金属ETF南方"))
        assert kept == ["159981", "159980", "512400"]

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


# ══════════════ build_rows：截面 + 量价热度 ══════════════

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
        """成交额占比升 → 占比分位登顶；镜像对手成交额占比降 → 量价热度低。"""
        n = 300
        amt_a = [10 ** 8 * (1 + k / n) for k in range(n)]
        amt_b = [10 ** 8 * (1 - k / n) for k in range(n)]
        pool = [{"code": "510001", "name": "A"}, {"code": "510002", "name": "B"}]
        bars = {"510001": _bars(_up_closes(n), amounts=amt_a),
                "510002": _bars(_up_closes(n), amounts=amt_b)}
        rows, _ = build_rows(pool, bars, {}, as_of=END)
        crowd = {r.code: r.crowd for r in rows}
        assert crowd["510001"] > 99.0              # 成交额占比+价格双双登顶
        assert crowd["510002"] < 50.0

    def test_crowd_none_passes_gate(self):
        """成交额占比分量缺观测（amount 全缺）→ 单分量 → crowd=None → 放行。"""
        idx = pd.bdate_range(end=END, periods=300).strftime("%Y-%m-%d")
        bars = {"510001": pd.DataFrame({"close": _up_closes(300),
                                        "amount": [float("nan")] * 300}, index=idx)}
        rows, _ = build_rows([{"code": "510001", "name": "x"}], bars, {}, as_of=END)
        assert rows[0].crowd is None
        assert rows[0].buy_allowed is True


# ══════════════ held_exit_facts：退出事实无状态重算 ══════════════

class TestHeldExitFacts:
    def _facts(self, closes, price, cost):
        return held_exit_facts(list(closes), price, cost)

    def test_insufficient_history_not_evaluated(self):
        f = self._facts(_up_closes(10), 10.0, 10.0)
        assert f["evaluated"] is False

    def test_latch_set_by_historical_high(self):
        closes = _up_closes(30, step=1.005)        # 末值 ≈ 成本×1.128 → 置位
        f = self._facts(closes, price=closes[-1] * 0.95, cost=10.0)
        assert f["latched"] is True and f["stop_ratio"] == 1.01

    def test_no_latch_keeps_cost_stop(self):
        closes = _up_closes(30, step=1.001)        # 末值 ≈ 成本×1.03 → 未置位
        f = self._facts(closes, price=closes[-1], cost=10.0)
        assert f["latched"] is False and f["stop_ratio"] == 0.92

    def test_latch_by_today_price(self):
        closes = _up_closes(30, step=1.001)        # 历史未达 +12%
        f = self._facts(closes, price=11.3, cost=10.0)   # 今日现价 +13%
        assert f["latched"] is True

    def test_below_stop_boundary_with_latch(self):
        closes = _up_closes(30, step=1.005)
        f = self._facts(closes, price=10.05, cost=10.0)  # 1.005 ≤ 1.01 → 触发
        assert f["below_stop"] is True
        f2 = self._facts(closes, price=10.20, cost=10.0)  # 1.02 > 1.01 → 未触发
        assert f2["below_stop"] is False

    def test_neg_run_counts_trailing_negative_scores(self):
        # 30 根缓升 + 10 根逐日微跌（无单日 -5%，不触跳水；末 3 日 score≤0）
        up = _up_closes(30, step=1.01)
        down = [up[-1] * (1 - 0.03 * k) for k in range(1, 11)]
        f = self._facts(up + down, price=down[-1], cost=10.0)
        assert f["evaluated"] is True
        assert f["neg_run"] >= 3

    def test_neg_run_zero_on_uptrend(self):
        f = self._facts(_up_closes(40, step=1.001), price=_up_closes(41)[-1],
                        cost=10.0)
        assert f["neg_run"] == 0

    def test_bad_cost_not_evaluated(self):
        f = self._facts(_up_closes(40), 10.0, 0.0)
        assert f["evaluated"] is False


# ══════════════ build_sell_orders：自身退出（score 确认 + 限亏 + 保本损） ══════════════

def _sat(code="510001", name="测试ETF", count=1000, price=10.0, cost=None,
         account="satellite"):
    return {"code": code, "name": name, "count": count, "account": account,
            "current_price": price, "cost_price": cost if cost is not None else price}


def _hist(closes):
    idx = pd.bdate_range(end=END, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"close": list(closes),
                         "amount": [60_000_000.0] * len(closes)}, index=idx)


class TestSellOrders:
    def _run(self, pos, bars, entry=None):
        em = {pos["code"]: entry} if entry else None
        return build_sell_orders([], [pos], bars, {}, em)

    def test_healthy_holding_no_exit(self):
        hist = _up_closes(40, step=1.001)
        pos = _sat(price=hist[-1], cost=hist[0])
        orders, notes = self._run(pos, {"510001": _hist(hist)})
        assert orders == [] and notes == []

    def test_score_negative_3days_exits(self):
        # 买在顶部后单边缓跌：score 逐日转负且 3 日确认满，价格未触限亏线
        # （不置保本：全程未达 +12%；未破 -8%：现价 12.49 > 成本×0.92）
        hist = [13.0 * (1 - 0.001 * k) for k in range(40)]
        pos = _sat(price=hist[-1], cost=hist[0])
        orders, _ = self._run(pos, {"510001": _hist(hist)})
        assert len(orders) == 1
        assert "score转负x3日" in orders[0].reason
        assert orders[0].shares == 1000

    def test_score_negative_2days_holds(self):
        up = _up_closes(40, step=1.01)
        hist = up + [up[-1] * 0.995, up[-1] * 0.99]
        pos = _sat(price=hist[-1], cost=hist[0])
        orders, _ = self._run(pos, {"510001": _hist(hist)})
        assert orders == []

    def test_cost_stop_exits(self):
        hist = _up_closes(40, step=1.001)          # 成本 10.0，历史未达 +12%
        pos = _sat(price=9.0, cost=hist[0])        # 现价 9.0 ≤ 成本×0.92
        orders, _ = self._run(pos, {"510001": _hist(hist)})
        assert len(orders) == 1 and "跌破成本-8%" in orders[0].reason

    def test_breakeven_exit(self):
        up = _up_closes(25, step=1.005)            # 峰 ≈ +12.8% → 置位
        down = [up[-1] * (1 - 0.023 * k) for k in range(1, 6)]   # 回到成本附近
        hist = up + down
        pos = _sat(price=hist[-1], cost=hist[0])   # ≈10.0 ∈ (9.2, 10.1]
        orders, _ = self._run(pos, {"510001": _hist(hist)})
        assert len(orders) == 1 and "保本损" in orders[0].reason

    def test_core_account_ignored(self):
        hist = _up_closes(40, step=1.001)
        pos = _sat(price=9.0, cost=hist[0], account="core")   # 核心仓不参与
        orders, notes = self._run(pos, {"510001": _hist(hist)})
        assert orders == [] and notes == []

    def test_missing_bars_notes_hold(self):
        pos = _sat()
        orders, notes = self._run(pos, {})
        assert orders == [] and notes and "无K线" in notes[0]

    def test_insufficient_history_holds(self):
        pos = _sat(price=10.0, cost=10.0)
        orders, notes = self._run(pos, {"510001": _hist(_up_closes(10))})
        assert orders == [] and "数据不足" in notes[0]

    def test_out_of_pool_direct_evaluation(self):
        """出池持仓直评：rows 为空（不在截面）照样按自身规则退出——池洞结构性消失。"""
        up = _up_closes(30, step=1.01)
        down = [up[-1] * (1 - 0.03 * k) for k in range(1, 11)]
        pos = _sat(code="599999", price=down[-1], cost=up[0])
        orders, _ = self._run(pos, {"599999": _hist(up + down)})
        assert len(orders) == 1

    def test_zero_count_skipped(self):
        up = _up_closes(30, step=1.01)
        down = [up[-1] * (1 - 0.03 * k) for k in range(1, 11)]
        pos = _sat(price=down[-1], cost=up[0], count=0)
        orders, _ = self._run(pos, {"510001": _hist(up + down)})
        assert orders == []


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

    def test_crowd_gate_open_at_90(self):
        """v3_3_2：准入闸常开（CROWD_PCT_MAX=101），量价热度 90 不再禁买。"""
        rows = _rows(10, crowd=90.0)
        orders, _ = build_buy_orders(rows, set(), 1_000_000, 0.0, 200_000)
        assert rows[0].code in {o.code for o in orders}

    def test_crowd_extreme_excludes_buy(self):
        rows = _rows(10, crowd=101.0)          # ≥101 才触发，正常评分范围内不可达
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

    def test_held_outside_universe_occupies_slot(self):
        """出池持仓受管 → 照样占槽（池洞消失的另一面：出池不释放名额）。"""
        rows = _rows(10)
        orders, _ = build_buy_orders(rows, {"599999"}, 1_000_000, 0.0, 200_000)
        assert len(orders) == TOPN - 1

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

def test_rules_match_v3_3_2_constants():
    """与 lM_v3_3_2 定稿常量一一对应（漂移即红）。

    v3_3_2 = score 出场 3 日确认 + -8% 限亏 + 保本损抬升 + 量价热度闸常开 +
    出池持仓直评（出场侧六次换血与本地反事实的收敛结果，2026-10-07 收线）。
    """
    assert im.TOPN == 3
    assert im.CROWD_PCT_MAX == 101.0
    assert im.STOP_COST_PCT == 0.08
    assert im.BREAKEVEN_TRIGGER == 0.12
    assert im.BREAKEVEN_STOP == 0.01
    assert im.SCORE_EXIT_CONFIRM == 3
    assert im.LIQ_AMT20_MIN == 50_000_000.0
    assert im.MATURE_DAYS == 365
    assert im.CORR_DEDUPE == 0.90
    assert im.REBUILD_EVERY == 20
    assert im.SCORE_DAYS == 25 and im.DIVE_RATIO == 0.95
    assert im.EXCLUDE_CODE_PREFIXES == ("513", "518")   # 518 段 = v3_1 后补
    # 词尾 v3_1 单变量改动在位；其后仅允许显式记录的实盘补词（当前 = 货币四词）
    assert im.EXCLUDE_KW[-6:] == ("债", "上海金", "短融", "日利", "添益", "快线")
    assert "A50" in im.EXCLUDE_KW                       # 宽基补词，不在词尾
    assert im.SATELLITE_BUDGET_RATIO == 0.10
