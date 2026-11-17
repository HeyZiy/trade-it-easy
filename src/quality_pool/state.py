# -*- coding: utf-8 -*-
"""每日名单、失败恢复标记与未完成退出队列的持久化。"""

from __future__ import annotations

from typing import Optional

from src.quality_pool.config import STRATEGY_VERSION

_EMPTY = {
    "version": STRATEGY_VERSION,
    "pending_plan": None,
    "exit_queue": {},
    "signal_retry": False,
    "last_signal_date": None,
}


def empty_state() -> dict:
    return dict(_EMPTY, exit_queue={})


def restore(raw: dict) -> dict:
    if raw.get("version") != STRATEGY_VERSION:
        raise ValueError("策略状态文件版本不兼容")
    return {**_EMPTY, **raw, "exit_queue": dict(raw.get("exit_queue") or {})}


def put_plan(state: dict, plan: Optional[dict]) -> None:
    state["pending_plan"] = (dict(plan, strategy_version=STRATEGY_VERSION)
                             if plan is not None else None)
