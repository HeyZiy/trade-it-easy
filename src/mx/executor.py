# -*- coding: utf-8 -*-
"""
===================================
整手收敛单点 — A 股可执行性口径
===================================

全部从此取整手口径，勿在他处重写。

本模块是纯计算模块：持仓事实来源是名义成交台账（docs/trade_ledger.md），
不承担任何下单执行。
"""

LOT_SIZE = 100


def round_lot(qty: float) -> int:
    """买侧整手：向下取整到整手后有量即强凑至少一手。

    买方向唯一口径：rebalancer 核心买与卫星动量买都走此函数。
    """
    return max(LOT_SIZE, int(qty // LOT_SIZE) * LOT_SIZE)


def floor_lot(shares: float) -> int:
    """卖侧整手：钳到整手向下取整，不足一手返回 0（调用方跳过该笔，勿记必拒订单）。"""
    return int(shares // LOT_SIZE) * LOT_SIZE
