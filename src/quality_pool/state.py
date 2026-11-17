# -*- coding: utf-8 -*-
"""每日名单、失败恢复标记与未完成退出队列的持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

from src.quality_pool.config import STATE_PATH, STRATEGY_VERSION

STATE_VERSION = STRATEGY_VERSION

_EMPTY = {
    "version": STATE_VERSION,
    "pending_plan": None,
    "exit_queue": {},
    "signal_retry": False,
    "last_signal_date": None,
}


def empty_state() -> dict:
    return dict(_EMPTY, exit_queue={})


def restore(raw: dict) -> dict:
    if raw.get("version") != STATE_VERSION:
        raise ValueError("策略状态文件版本不兼容")
    return {**_EMPTY, **raw, "exit_queue": dict(raw.get("exit_queue") or {})}


def load_state(path: Union[str, Path] = STATE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_state()
    return restore(json.loads(p.read_text(encoding="utf-8")))


def save_state(state: dict, path: Union[str, Path] = STATE_PATH) -> None:
    """同目录临时文件替换；写入失败时保留上一份完整状态。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=p.parent,
                prefix=p.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def put_plan(state: dict, plan: Optional[dict]) -> None:
    state["pending_plan"] = (dict(plan, strategy_version=STATE_VERSION)
                             if plan is not None else None)


def pop_plan(state: dict) -> Optional[dict]:
    plan = state.get("pending_plan")
    state["pending_plan"] = None
    return plan
