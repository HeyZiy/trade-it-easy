"""质量动量实际账户建议：复用策略判定，冻结本轮份额，只消费人工确认成交。"""

from dataclasses import asdict, replace
import json

from src.advice_identity import short_digest
from src.quality_pool import config, execution


def make_signal(day, state, snapshot, previous=None):
    """同日已发布的调仓目标不重建；次日只重试实际账户的未完成退出。"""
    from src.quality_pool.signal import SignalPlan

    if previous and previous["date"] == day and not previous.get("issues"):
        return previous
    raw = state.get("pending_plan")
    plan = SignalPlan.from_stored(raw, day)
    exits = {c: r for c, r in state.get("exit_queue", {}).items()
             if snapshot.counts.get(c, 0) > 0}
    if plan:
        exits.update(plan.exit_reasons)
        exits = {c: r for c, r in exits.items() if c not in plan.selected}
    state["exit_queue"] = exits
    if any(c not in snapshot.prices for c in snapshot.counts):
        raise ValueError("质量动量实际持仓价格缺失，暂停建议")
    # 先形成预算内的目标，实际可买数量由 advice 按已分配现金收紧。
    # 否则尚未分配现金的首次接入会消耗整轮名单，随后20个交易日都无法入场。
    planning = replace(snapshot, cash=max(snapshot.cash, snapshot.equity))
    result = (execution.plan_round(plan.selected, exits, snapshot.equity / config.TOP_N,
                                   planning, deferred=plan.deferred, industries=plan.industries)
              if plan else execution.plan_exit_retry(exits, snapshot))
    counts = dict(snapshot.counts)
    orders = []
    for trade in result.trades:
        counts[trade.code] = counts.get(trade.code, 0) + (trade.qty if trade.side == "buy" else -trade.qty)
        orders.append({**asdict(trade), "target_count": counts[trade.code], "remaining": trade.qty})
    state["pending_plan"] = None
    signal = {"date": day, "prices": snapshot.prices, "orders": orders,
              "budget": snapshot.equity, "issues": None,
              "blocked": [asdict(b) for b in result.blocked], "round": plan is not None,
              "next_signal_date": state.get("next_signal_date")}
    signal["id"] = short_digest(json.dumps(signal, sort_keys=True))
    return signal


def advice(signal, strategy, today, *, dismissed):
    if not signal:
        return [], "等待交易日更新"
    if signal["date"] != today.isoformat():
        return [], "建议已过期，等待交易日更新"
    if signal.get("issues"):
        return [], signal["issues"]
    if strategy["budget"] is None or strategy["budget"] <= 0:
        return [], "请先设置质量动量目标比例"
    orders = signal["orders"]
    identity = lambda order: f"quality:{signal['id']}:{order['side']}:{order['code']}"
    if any(identity(order) in dismissed for order in orders):
        return [], "本轮建议已忽略"
    prices = signal["prices"]
    held = {p["code"]: p for p in strategy["positions"]}
    if any(c not in prices for c in held):
        return [], "持仓价格缺失，等待更新"
    invested = sum(p["count"] * prices[c] for c, p in held.items())
    # 同一轮不能因资金转入或目标预算上调而扩大；预算下调立即收紧买入。
    budget = min(signal["budget"], strategy["budget"])
    result = []
    available = strategy["cash"]
    sale_value = 0
    for order in orders:
        code, side, price = order["code"], order["side"], prices[order["code"]]
        position = held.get(code, {})
        count = position.get("count", 0)
        delta = order["target_count"] - count
        qty = min(order["remaining"], max(0, delta if side == "buy" else -delta))
        if side == "sell":
            qty = min(qty, position.get("avail_count", 0)) // 100 * 100
        else:
            headroom = max(0, budget - invested + sale_value)
            qty = min(qty, execution.affordable_shares(min(available, headroom), price)) // 100 * 100
        if qty <= 0:
            continue
        amount = round(qty * price, 2)
        fee = round(max(config.MIN_COMMISSION, amount * config.FEE_COMMISSION), 2)
        if side == "sell":
            if available + amount < fee:
                return [], "策略现金不足以扣除卖出费用"
            available += amount - fee
            sale_value += amount
        else:
            available -= amount + fee
            invested += amount
        result.append({"id": identity(order), "code": code, "name": order["name"],
                       "side": side, "qty": qty, "price": price, "amount": amount,
                       "gap_amount": amount, "price_date": signal["date"], "price_issue": None,
                       "reason": "质量动量等权调整" if order["reason"] == "equal_weight" else "确认出池",
                       "cash_note": "需先卖出回款" if side == "buy" and sale_value else None})
    if result:
        return result, "先卖后买" if sale_value and any(r["side"] == "buy" for r in result) else "建议已更新"
    if signal["blocked"]:
        return [], "部分标的受交易状态、行业名额或资金限制，等待下轮更新"
    if any(o["side"] == "buy" and o["remaining"] > 0 for o in orders):
        return [], "可用资金或预算不足一手，请分配现金或调整目标比例；买入仅限本调仓日"
    return [], ("本轮建议已完成或资金不足一手" if signal["round"] else
                f"非调仓日，仅检查退出；下次调仓 {signal.get('next_signal_date') or '待定'}")


def on_event(signal, event):
    """已确认的分批成交消耗本轮数量；之后资金变化不重复已完成买入。"""
    if not signal or event["side"] not in {"buy", "sell"} or signal["date"] != event["date"]:
        return signal
    return {**signal, "orders": [
        {**order, "remaining": max(0, order["remaining"] - event["qty"])}
        if order["code"] == event["code"] and order["side"] == event["side"] else order
        for order in signal.get("orders", [])]}
