# -*- coding: utf-8 -*-
"""roe_rotation_v1（ROE 质量轮动平台脚本原版）的桩测试。

锁五语义：过滤链算术（基本面∩估值−ST−解禁−波动窗）/涨跌幅降序进 TOP_N /
整批满 20 交易日才卖（卖后回买不继承旧日期的回归红线）/整手且现金不透支 /
汇总块可打印。跑法同套件：python -m pytest tests/。

v1 的修订版（v1_1~v2_1）由 test_platform_roe_rotation_revisions.py 与
test_platform_roe_rank_attribution.py 接管，本文件只锚原版行为。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jq_fake import JQStub  # noqa: E402

SCRIPT = (Path(__file__).resolve().parent.parent
          / 'research' / 'studies' / 'roe_quality' / 'roe_rotation_v1.py')

CODES = ['%06d' % (600000 + i) for i in range(30)]   # 主板 30 只（裸码）
BOOST = {CODES[0], CODES[2], CODES[8]}               # 人造 20 日动量
ST, BAN = CODES[4], CODES[6]
YOUNG = CODES[28]                                    # 驱动窗口中途才满 61 根


def _world():
    rng = np.random.default_rng(11)
    cal = pd.bdate_range('2024-01-02', periods=700)
    closes = {}
    for i, c in enumerate(CODES):
        close = 10.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.018, len(cal))))
        if c == YOUNG:
            close[:250] = np.nan                     # 上市晚，第 2 个轮动日才满 61 根
        if c in BOOST:
            ramp = np.ones(len(cal))                 # 永久台阶：无断崖，不污染波动窗
            ramp[280:301] = np.linspace(1.0, 1.35, 21)
            ramp[301:] = 1.35
            close = close * ramp
        closes[c] = pd.Series(close, index=cal)
    return cal, closes


def _install_stub(cal, closes):
    stub = JQStub(cal, closes)
    # 基本面：最新报告期 2024q2（累计 ROE 9 → 年化 9×4/2=18 达标；
    # 5 → 10 在 ROE_MIN=12 之下不达标），锁住 ×4/n 年化这条路而不是直比累计值
    stub.set_date_table(pd.DataFrame({
        'code': CODES,
        'roe': [9.0 if i % 2 == 0 else 5.0 for i in range(30)],
        'inc_net_profit_year_on_year': [25.0 if i % 2 == 0 else 5.0 for i in range(30)],
        'statDate': ['2024q2'] * 30,
    }), table='indicator')
    stub.set_date_table(pd.DataFrame({
        'code': CODES, 'pe_ratio': [20.0] * 30, 'pb_ratio': [3.0] * 30}), table='valuation')
    stub.set_extras('is_st', {ST: True})
    stub.set_locked_shares(pd.DataFrame({'code': [BAN], 'day': [cal[350]]}))
    return stub.install()


def test_signal_filter_rank_and_batch_rotation():
    cal, closes = _world()
    stub = _install_stub(cal, closes)
    mod = stub.load_script(SCRIPT)
    stub.initialize()
    stub.run_days(cal[300:361])                      # 61 个交易日 = 4 个轮动日

    # 过滤链算术：偶数 15（基本面）全过估值 − ST(4) − 解禁(6) − young(28 波动窗)
    # = 12；young 第 2 个轮动日起满 61 根 → 13
    assert stub.g.batch_sizes == [12, 13, 13, 13], stub.g.batch_sizes

    sells = [o for o in stub.orders if o[0] == 'S']
    buys = [o for o in stub.orders if o[0] == 'B']
    sell_days = {o[1] for o in sells}
    buy_days = {o[1] for o in buys}

    # 买入日 = 轮动日次日；TOP_N 截断；动量前三（boost）必进首批
    assert buy_days == {cal[301].strftime('%F'), cal[321].strftime('%F'),
                        cal[341].strftime('%F')}
    for d in buy_days:
        assert sum(1 for o in buys if o[1] == d) == 10
    first_day = cal[301].strftime('%F')
    first_bought = {o[2] for o in buys if o[1] == first_day}
    assert BOOST <= first_bought, f"动量前 3 未进首批: {first_bought}"

    # 卖出只在整批到期日（20 个交易日整），卖后回买不继承旧日期（回归红线）
    assert sell_days == {cal[321].strftime('%F'), cal[341].strftime('%F')}
    for d in sell_days:
        assert sum(1 for o in sells if o[1] == d) == 10

    assert stub.portfolio.cash > -1e-6
    assert all(p.total_amount % 100 == 0
               for p in stub.portfolio.positions.values())
    mod.on_strategy_end(stub.ctx)
    assert any('ROE 质量轮动 汇总' in x for x in stub.logs), "汇总块未打印"
