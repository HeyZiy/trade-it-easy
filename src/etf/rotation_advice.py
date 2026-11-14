"""轮动建议的完整规则：信号延续、首次成交买入计划、剩余份额与整组忽略。

只接收账户提供的策略事实和版本，不读取账户文件；加锁与保存由账户 module 负责。
"""

from dataclasses import asdict
import hashlib
import json
import math

from src.etf.rotation import (
    ACCOUNT, COMMISSION, MIN_COMMISSION, RotationRow, affordable_shares, plan_rotation, top_candidate,
)
from src.etf.momentum import EtfOrder
from src.advice_identity import short_digest
from data_provider.security_names import security_name


def make_signal(day, rows, prices, diag, error=None, *, previous=None):
    """更新行情并延续同目标买入计划；旧日期返回 None，不覆盖已有信号。"""
    if previous and previous["date"] > day:
        return None
    # 非合格标的可能包含 NaN；JSON 中统一为空，不影响候选排序。
    facts = [{key: (None if isinstance(value, float) and not math.isfinite(value) else value)
              for key, value in asdict(row).items() if key != "name"} for row in rows]
    signal = {"date": day, "rows": facts,
            "prices": {code: value for code, value in prices.items()
                       if isinstance(value, (int, float)) and math.isfinite(value) and value > 0},
            "issues": error or (
                "轮动行情不完整，等待更新" if not rows or diag.get("realtime_missing") or
                (diag.get("decision_date") and diag["decision_date"] != day) or
                any(diag.get("skipped", {}).get(k, 0)
                    for k in ("no_bars", "stale", "bad_close", "duplicate")) else None)}
    target = top_candidate(rows)
    intent = previous.get("buy_intent") if previous else None
    if intent and (signal["issues"] or (target and target.code == intent["code"])):
        signal["buy_intent"] = dict(intent)
    return signal


def advice(signal, strategy, today, *, account_revision, dismissed):
    """返回建议及空列表的具体原因；信号跨日失效，成交后按最新持仓重算。"""
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
    rows = [RotationRow(**{**row, "name": security_name(row["code"])}) for row in signal["rows"]]
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
        identity = short_digest(
            f"rotation:{key}:{account_revision}:{order.code}:{order.action}:{order.shares}"
        )
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
    if any(item["id"] in dismissed for item in result):
        return [], "本轮建议已忽略"
    return result, "先卖后买" if plan["sells"] and plan["buys"] else "建议已更新"


def on_event(signal, event, strategy, today, *, account_revision, dismissed):
    """首次匹配建议的买入确定本轮目标份额；后续成交及资金变化不扩大该计划。"""
    if (not signal or signal.get("buy_intent") or event["side"] != "buy"
            or event.get("strategy") != "rotation"):
        return signal
    planned, _ = advice(signal, strategy, today,
                        account_revision=account_revision, dismissed=dismissed)
    buy = next((item for item in planned
                if item["side"] == "buy" and item["code"] == event["code"]), None)
    if buy is None:
        return signal
    held = sum(p["count"] for p in strategy["positions"] if p["code"] == event["code"])
    return {**signal, "buy_intent": {"code": buy["code"], "qty": held + buy["qty"]}}


def render(suggestions, status):
    """任务报告与网页消费同一份建议结果。"""
    lines = [status]
    for item in suggestions:
        action = "卖出" if item["side"] == "sell" else "买入"
        lines.append(f"- {action} {item['name']}({item['code']}) 约 {item['qty']:,} 份"
                     f" · 约 {item['amount']:,.2f} 元")
    return "\n".join(lines)
