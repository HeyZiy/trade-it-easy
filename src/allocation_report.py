"""收盘后的权益与固收组合再平衡建议。"""

from collections import defaultdict
import json
from pathlib import Path

from data_provider import fund_names
from src import account_valuation, json_store, portfolio_config
from src.account_advice import allocation_view
from src.advice_identity import short_digest
from src.task_io import notify, save_report


def allocation_actions(state, day):
    view = account_valuation.valuation_view(state, day, day.isoformat())
    if view["equity"] is None:
        return None
    config = state["_config"]
    names = fund_names.names_for({code for row in portfolio_config.allocation_rows(config)
                                  for code in row.get("fund_codes", ())})
    actions = {}
    for key, name in portfolio_config.ALLOCATION_NAMES.items():
        strategy = allocation_view(state, view, key, names=names)
        if strategy["status"] != "已分配":
            continue
        grouped = {row["code"] for row in config.get(key, ()) if portfolio_config.grouped(row)}
        orders = {order["code"]: order for order in strategy["suggestions"]}
        for group in strategy["allocation_groups"]:
            code = group["id"]
            order = orders.get(code)
            if order:
                direction = "卖出" if order["side"] == "sell" else "买入"
                note = f"，{order['cash_note']}" if order["cash_note"] else ""
                detail = f"{group['name']}（{code}）{order['qty']:,} 份，约 {order['amount']:,.2f} 元{note}"
            elif code in grouped and group["trade_eligible"]:
                direction = "卖出" if group["gap_amount"] > 0 else "买入"
                products = "、".join(row["code"] for row in group["products"])
                label = group["name"]
                if group["name_fund_code"] and not group["products"][0].get("name"):
                    fund_code = group["name_fund_code"]
                    label = fund_names.lookup({fund_code}).get(fund_code, "场外基金")
                detail = f"{label}（{products}）约 {abs(group['gap_amount']):,.2f} 元"
                if len(group["products"]) > 1:
                    detail += "，需确认交易品种"
            else:
                continue
            triggers = []
            if group["absolute_review"]:
                triggers.append(f"绝对偏离 {group['absolute_deviation'] * 100:.2f} 个百分点")
            if group["relative_review"]:
                triggers.append(f"相对偏离 {group['relative_deviation']:.2%}")
            actions[f"{key}:{code}:{direction}"] = (name, f"{direction} {detail}｜{'、'.join(triggers)}")
    return actions


def render_report(day, actions, first_seen):
    groups = defaultdict(list)
    for key, (name, action) in actions.items():
        pending = "**仍待处理**｜" if first_seen.get(key, day) < day else ""
        groups[name].append(f"- {pending}{action}")
    sections = [f"# 收盘再平衡建议（{day}）"]
    for name, lines in groups.items():
        sections.extend((f"## {name}", "\n".join(lines)))
    return "\n\n".join(sections)


def publish(day, actions, *, no_notify=False, reports_dir="reports"):
    """记录持续触发的方向，并允许重试未送达的当日报告。"""
    path = Path(reports_dir) / "allocation_rebalance_pending.json"
    with json_store.locked(path):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = {}
        if previous.get("date", "") > day:
            return True
        first_seen = {key: previous.get("first_seen", {}).get(key, day) for key in actions}
        notified = previous.get("notified") if previous.get("date") == day else None
        current = {"date": day, "first_seen": first_seen, "notified": notified}
        if not actions:
            json_store.save(path, current)
            return True
        report = render_report(day, actions, first_seen)
        digest = short_digest(report)
        save_report(report, "allocation_rebalance", reports_dir, report_date=day)
        failed = not no_notify and digest != notified and not notify(report)
        if not no_notify and not failed:
            current["notified"] = digest
        json_store.save(path, current)
        return not failed
