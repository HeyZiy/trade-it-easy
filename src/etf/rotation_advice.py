"""轮动建议、分批调仓进度与整组忽略。"""

import hashlib
import json
import math
from dataclasses import asdict

from data_provider.security_names import security_name
from src.advice_identity import short_digest
from src.etf.rotation import ACCOUNT, TARGET_COUNT, RotationRow, plan_rotation, top_targets

RULESET = "four-window-top2"


def _market_incomplete(day, rows, diag) -> bool:
    if not rows or diag.get("realtime_missing"):
        return True
    decision_day = diag.get("decision_date")
    if decision_day and decision_day != day:
        return True
    skipped = diag.get("skipped", {})
    return any(skipped.get(reason, 0) for reason in ("no_bars", "stale", "bad_close", "duplicate"))


def make_signal(day, rows, prices, diag, error=None, *, previous=None) -> dict | None:
    """目标组合不变时延续已开始的调仓；旧日期不覆盖已有信号。"""
    if previous and previous["date"] > day:
        return None
    facts = [{key: (None if isinstance(value, float) and not math.isfinite(value) else value)
              for key, value in asdict(row).items() if key != "name"} for row in rows]
    valid_prices = {code: value for code, value in prices.items()
                    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0}
    issue = error or ("轮动行情不完整，等待更新" if _market_incomplete(day, rows, diag) else None)
    signal = {"date": day, "ruleset": RULESET, "rows": facts,
              "prices": valid_prices, "issues": issue}
    rebalance = previous.get("rebalance") if previous else None
    if rebalance is not None and (signal["issues"] or
                                  rebalance.keys() == {row.code for row in top_targets(rows)}):
        signal["rebalance"] = dict(rebalance)
    return signal


def _no_order_status(targets, positions, rebalance) -> str:
    if not targets:
        return "无合格候选，保持现金"
    if positions.keys() == set(targets):
        if rebalance is None or all(positions[code]["count"] == shares
                                    for code, shares in rebalance.items()):
            return "继续持有，目标组合未变"
    return "可用资金不足一手，请分配现金或调整目标比例"


def advice(signal, strategy, today, *, account_revision, dismissed) -> tuple[list[dict], str]:
    """返回建议及空列表的具体原因；信号跨日失效，成交后按最新持仓重算。"""
    if not signal:
        return [], "等待交易日更新"
    if signal["date"] != today.isoformat() or signal.get("ruleset") != RULESET:
        return [], "建议已过期，等待交易日更新"
    if signal["issues"]:
        return [], signal["issues"]
    budget = strategy["budget"]
    if budget is None:
        return [], "请先设置目标比例"
    if budget <= 0:
        return [], "目标比例为零"

    prices = signal["prices"]
    positions = {row["code"]: {**row, "account": ACCOUNT} for row in strategy["positions"]}
    rows = [RotationRow(**{**row, "name": security_name(row["code"])}) for row in signal["rows"]]
    if any(code not in prices for code in positions) or any(
            row.code not in prices or row.price != prices[row.code] for row in top_targets(rows)):
        return [], "持仓或候选价格缺失，等待更新"
    rebalance = signal.get("rebalance")
    plan = plan_rotation(rows, list(positions.values()), prices, strategy["cash"],
                         budget=budget, rebalance=rebalance)
    if plan["issue"]:
        return [], plan["issue"]
    if any(order.shares > positions[order.code]["avail_count"] for order in plan["sells"]):
        return [], "部分份额当日不可卖，待可卖后更新建议"
    if not plan["orders"]:
        return [], _no_order_status(plan["targets"], positions, rebalance)

    key = hashlib.sha256(json.dumps(signal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    has_sells = bool(plan["sells"])
    result = []
    for order in plan["orders"]:
        identity = short_digest(
            f"rotation:{key}:{account_revision}:{order.code}:{order.action}:{order.shares}"
        )
        current_shares = positions.get(order.code, {}).get("count", 0)
        change = order.shares if order.action == "buy" else -order.shares
        amount = round(order.amount, 2)
        result.append({"id": identity, "code": order.code, "name": order.name,
                       "side": order.action, "qty": order.shares, "price": order.price,
                       "amount": amount, "gap_amount": amount,
                       "current_value": round(current_shares * order.price, 2),
                       "target_value": round((current_shares + change) * order.price, 2),
                       "price_date": signal["date"], "price_issue": None,
                       "reason": order.reason,
                       "cash_note": "需先卖出回款" if has_sells and order.action == "buy" else None})
    if any(item["id"] in dismissed for item in result):
        return [], "本轮建议已忽略"
    return result, "先卖后买" if has_sells and plan["buys"] else "建议已更新"


def _planned_shares(rows, held, orders) -> dict[str, int]:
    target_codes = [row["code"] for row in rows if row["eligible"]][:TARGET_COUNT]
    shares = {code: held.get(code, 0) for code in target_codes}
    for order in orders:
        code = order["code"]
        if code in shares:
            change = order["qty"] if order["side"] == "buy" else -order["qty"]
            shares[code] += change
    return shares


def on_event(signal, event, strategy, today, *, account_revision, dismissed) -> dict | None:
    """首笔匹配成交固定组合份额；完成后不因价格或现金变化继续配平。"""
    if (not signal or event.get("date") != signal["date"] or event["side"] not in {"buy", "sell"}
            or event.get("strategy") != "rotation"):
        return signal
    planned, _ = advice(signal, strategy, today,
                        account_revision=account_revision, dismissed=dismissed)
    code = event["code"]
    side = event["side"]
    if not any(item["side"] == side and item["code"] == code for item in planned):
        return signal
    held = {pos["code"]: pos["count"] for pos in strategy["positions"]}
    rebalance = signal.get("rebalance")
    if rebalance is None:
        rebalance = _planned_shares(signal["rows"], held, planned)

    change = event["qty"] if side == "buy" else -event["qty"]
    held[code] = held.get(code, 0) + change
    if code in rebalance:
        remaining = rebalance[code] - held[code]
        if (side == "buy" and remaining < 0) or (side == "sell" and remaining > 0):
            rebalance = {**rebalance, code: held[code]}
    updated = dict(signal)
    if {code: qty for code, qty in held.items() if qty > 0} != rebalance:
        updated["rebalance"] = rebalance
    else:
        updated.pop("rebalance", None)
    return updated


def render(suggestions, status) -> str:
    """任务报告与网页消费同一份建议结果。"""
    lines = [status]
    for item in suggestions:
        action = "卖出" if item["side"] == "sell" else "买入"
        lines.append(f"- {action} {item['name']}({item['code']}) 约 {item['qty']:,} 份"
                     f" · 约 {item['amount']:,.2f} 元")
    return "\n".join(lines)
