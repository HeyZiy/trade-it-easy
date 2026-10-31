"""共享账户的轮动建议：保存行情事实，每次按已确认持仓重算，不写成交。"""

from dataclasses import asdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path

from src.etf.rotation import (
    ACCOUNT, COMMISSION, MIN_COMMISSION, RotationRow, affordable_shares, plan_rotation, top_candidate,
)
from src.etf.momentum import EtfOrder


def make_signal(day, rows, prices, diag, error=None):
    # 非合格标的可能包含 NaN；JSON 中统一为空，不影响候选排序。
    facts = [{key: (None if isinstance(value, float) and not math.isfinite(value) else value)
              for key, value in asdict(row).items()} for row in rows]
    return {"date": day, "rows": facts,
            "prices": {code: value for code, value in prices.items()
                       if isinstance(value, (int, float)) and math.isfinite(value) and value > 0},
            "issues": error or (
                "轮动行情不完整，等待更新" if not rows or diag.get("realtime_missing") or
                (diag.get("decision_date") and diag["decision_date"] != day) or
                any(diag.get("skipped", {}).get(k, 0)
                    for k in ("no_bars", "stale", "bad_close", "duplicate")) else None)}


def advice(state, strategy, today):
    """返回建议及空列表的具体原因；信号跨日失效，成交后按最新持仓重算。"""
    from src.etf_account import revision

    signal = state.get("rotation_signal")
    if not signal:
        return [], "等待交易日更新"
    if signal["date"] != today.isoformat():
        return [], "建议已过期，等待交易日更新"
    if signal["issues"]:
        return [], signal["issues"]
    if strategy["budget"] is None:
        return [], "请先设置目标比例"
    if strategy["budget"] <= 0:
        return [], "目标比例为零"

    prices = signal["prices"]
    positions = [{**row, "account": ACCOUNT} for row in strategy["positions"]]
    rows = [RotationRow(**row) for row in signal["rows"]]
    if any(row["code"] not in prices for row in positions) or any(
            row.eligible and (row.code not in prices or row.price != prices[row.code]) for row in rows):
        return [], "持仓或候选价格缺失，等待更新"
    target = top_candidate(rows)
    # 人工分批换仓可能短暂同时持有新旧标的；保留已买入的目标份额。
    positions.sort(key=lambda row: row["code"] != (target.code if target else None))
    plan = plan_rotation(rows, positions, prices, strategy["cash"])
    # 人工持仓可能留有不足一手的零股；清仓建议包含全部份额，不留下旧标的再买新标的。
    plan["sells"] = [EtfOrder(p["code"], p["name"], "sell", p["count"], prices[p["code"]],
                              p["count"] * prices[p["code"]],
                              "换入第一名" if target else "无合格候选，保持现金")
                     for p in positions if not target or p["code"] != target.code]
    plan["sells"].sort(key=lambda order: -order.amount)
    if any(order.shares > next(p["avail_count"] for p in positions if p["code"] == order.code)
           for order in plan["sells"]):
        return [], "部分份额当日不可卖，待可卖后更新建议"
    proceeds = sum(o.amount - round(max(MIN_COMMISSION, o.amount * COMMISSION), 2)
                   for o in plan["sells"])
    if strategy["cash"] + proceeds < 0:
        return [], "策略现金不足以扣除卖出费用"

    # 规则核按全部资金规划；账户侧另外限制新买入不得超过策略目标资产。
    for order in plan["buys"]:
        order.shares = min(order.shares, affordable_shares(strategy["budget"], order.price))
        order.amount = order.shares * order.price
    intent = signal.get("buy_intent")
    if target and intent and intent["code"] == target.code:
        held = next((p["count"] for p in positions if p["code"] == target.code), 0)
        intended = min(intent["qty"], affordable_shares(strategy["budget"], target.price))
        qty = min(max(0, intended - held) // 100 * 100,
                  affordable_shares(strategy["cash"] + proceeds, target.price))
        plan["buys"] = [EtfOrder(target.code, target.name, "buy", qty, target.price,
                                 qty * target.price, "完成本轮剩余买入")] if qty else []
    plan["orders"] = plan["sells"] + plan["buys"]
    orders = [order for order in plan["orders"] if order.shares > 0]
    if not orders:
        if positions and not intent:
            return [], "继续持有，仍为第一名"
        if positions and intent and positions[0]["count"] >= intent["qty"]:
            return [], "继续持有，仍为第一名"
        if plan["target"] is None:
            return [], "无合格候选，保持现金"
        return [], "可用资金不足一手，请分配现金或调整目标比例"

    key = hashlib.sha256(json.dumps(signal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result = []
    for order in orders:
        identity = hashlib.sha256(
            f"rotation:{key}:{revision(state)}:{order.code}:{order.action}:{order.shares}".encode()
        ).hexdigest()[:24]
        current = next((p for p in positions if p["code"] == order.code), None)
        current_value = round(current["count"] * prices[order.code], 2) if current else 0
        result.append({"id": identity, "code": order.code, "name": order.name,
                       "side": order.action, "qty": order.shares, "price": order.price,
                       "amount": round(order.amount, 2), "gap_amount": round(order.amount, 2),
                       "current_value": current_value,
                       "target_value": 0 if order.action == "sell" else round(current_value + order.amount, 2),
                       "price_date": signal["date"], "price_issue": None,
                       "reason": order.reason,
                       "cash_note": "需先卖出回款" if order.action == "buy" and plan["sells"] else None})
    # 先卖后买属于一组建议，忽略卖出时不能留下依赖其回款的买入。
    if any(item["id"] in state["dismissed"] for item in result):
        return [], "本轮建议已忽略"
    return result, "先卖后买" if plan["sells"] and plan["buys"] else "建议已更新"


def publish(day, rows, prices, diag, *, path, dry_run=False, error=None):
    """只更新行情信号；持仓、资金、净值与流水保持不变。"""
    from src.etf_account import _locked, _save, load, snapshot, strategy_views

    path = Path(path)
    if not path.exists():
        return "ETF账户尚未初始化"
    with _locked(path):
        state = load(path)
        existing = state.get("rotation_signal")
        if existing and existing["date"] > day:
            return "已有更新日期的账户建议，本次未覆盖"
        state["rotation_signal"] = make_signal(day, rows, prices, diag, error)
        # 报告与网页使用同一份规划；日期由调用方明确传入。
        view = snapshot(state, day)
        target = top_candidate(rows)
        previous_intent = existing.get("buy_intent") if existing else None
        if previous_intent and (state["rotation_signal"]["issues"] or
                                (target and target.code == previous_intent["code"])):
            state["rotation_signal"]["buy_intent"] = previous_intent
        strategy = strategy_views(state, view, today=date.fromisoformat(day))[1]
        if not dry_run:
            _save(path, state)
    lines = [strategy["advice_status"]]
    for item in strategy["suggestions"]:
        action = "卖出" if item["side"] == "sell" else "买入"
        lines.append(f"- {action} {item['name']}({item['code']}) 约 {item['qty']:,} 份"
                     f" · 约 {item['amount']:,.2f} 元")
    return "\n".join(lines)
