"""实际交易账户的现金与份额。策略预算和组合净值由原账本计算。"""
from collections import defaultdict
from decimal import Decimal
import re


UNKNOWN = "unlocated"
TEMPLATE = {
    "accounts": {"broker": {"name": "券商", "channel": "listed"},
                 "platform1": {"name": "基金平台一", "channel": "fund"},
                 "platform2": {"name": "基金平台二", "channel": "fund"}},
    "defaults": {"listed": "broker", "fund": "platform1"}, "products": {},
}


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != {"accounts", "defaults", "products"}:
        raise ValueError("交易账户配置无效")
    accounts, defaults, products = (settings[key] for key in ("accounts", "defaults", "products"))
    if not isinstance(accounts, dict) or not 2 <= len(accounts) <= 50:
        raise ValueError("请设置场内与场外交易账户")
    names = set()
    for key, row in accounts.items():
        if (key == UNKNOWN or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key)
                or not isinstance(row, dict) or set(row) != {"name", "channel"}
                or row["channel"] not in ("listed", "fund") or not isinstance(row["name"], str)
                or not 1 <= len(row["name"].strip()) <= 40 or row["name"].strip() in names):
            raise ValueError("交易账户名称须唯一，渠道须为场内或场外")
        names.add(row["name"].strip())
    if not isinstance(defaults, dict) or set(defaults) != {"listed", "fund"} or not isinstance(products, dict):
        raise ValueError("请选择默认交易账户")
    for product, account in [*defaults.items(), *products.items()]:
        if product in products and not re.fullmatch(r"(?:listed|fund):[0-9]{6}", product):
            raise ValueError("产品默认账户须指定渠道和六位代码")
        if not isinstance(account, str) or accounts.get(account, {}).get("channel") != product.split(":")[0]:
            raise ValueError("默认交易账户与产品渠道不匹配")


def channel(event):
    return "fund" if event["side"].startswith("fund_") or event.get("kind") == "fund" else "listed"


def prepare(event, payload, view, settings, *, historical=False):
    """为新流水固化交易账户；旧流水始终从待归属余额重放。"""
    side = event["side"]
    if side in ("cash_assign", "cash_transfer"):
        return
    accounts = (settings or {}).get("accounts", {})
    kind = channel(event)
    owners = view["custody_positions"]
    account = payload.get("trading_account")
    pending = view["fund_pending"].get(event.get("pending_id") or event.get("voided_pending_id"))
    if pending:
        origin = pending.get("trading_account", UNKNOWN)
        if account is not None and account != origin:
            raise ValueError("确认或作废须沿用原申购账户")
        account = origin
    owner = event.get("from_strategy", event.get("strategy"))
    held = {r["account"]: r["qty"] for r in owners if
            (r["kind"], r["strategy"], r["code"]) == (kind, owner, event.get("code"))}
    outgoing = side in ("assign", "sell", "fund_redeem")
    if account is None:
        if historical or not settings:
            account = UNKNOWN
        elif outgoing and len(held) == 1:
            account = next(iter(held))
        elif outgoing or side in ("custody_transfer", "deposit", "withdraw", "dividend", "cash_adjust"):
            raise ValueError("请选择实际交易账户")
        else:
            account = settings["products"].get(f"{kind}:{event.get('code')}", settings["defaults"][kind])
    if not isinstance(account, str) or (account != UNKNOWN and account not in accounts):
        raise ValueError("交易账户不存在")
    if account == UNKNOWN and settings and not historical and not (pending or outgoing or side in ("custody_transfer", "cash_adjust")):
        raise ValueError("请选择实际交易账户")
    if (event.get("code") and account != UNKNOWN and accounts[account]["channel"] != kind):
        raise ValueError("交易账户与产品渠道不匹配")
    cash = view["custody_cash"].get(account, 0)
    if side == "custody_transfer":
        target = payload.get("to_account")
        if not isinstance(target, str) or target not in accounts or target == account:
            raise ValueError("请选择不同的转入账户")
        if event.get("code") and accounts[target]["channel"] != kind:
            raise ValueError("转入账户与产品渠道不匹配")
        event["to_account"] = target
    if (outgoing or side == "custody_transfer" and event.get("code")) and event["qty"] > held.get(account, 0):
        raise ValueError("份额超过所选交易账户持仓")
    if side == "buy":
        debit = round(event["price"] * event["qty"] + event["fee"], 2)
    elif side == "sell":
        debit = round(event["fee"] - event["price"] * event["qty"], 2)
    elif side in ("fund_buy", "fund_pending", "withdraw") or (side == "custody_transfer" and not event.get("code")):
        debit = event["amount"]
    else:
        debit = 0
    if debit > cash:
        raise ValueError("所选交易账户现金不足，请先登记入金或账户间划转")
    if account != UNKNOWN or settings or "trading_account" in payload:
        event["trading_account"] = account


class Balances:
    def __init__(self, initial):
        self.cash = defaultdict(Decimal, {UNKNOWN: Decimal(str(initial["cash"]))})
        self.positions = defaultdict(Decimal)
        for row in initial["positions"]:
            self.positions[UNKNOWN, "listed", row["strategy"], row["code"]] += row["count"]

    def apply(self, event, cash_change):
        account = event.get("trading_account", UNKNOWN)
        side = event["side"]
        self.cash[account] += Decimal(str(round(cash_change, 2)))
        if side == "custody_transfer" and event["kind"] == "cash":
            amount = Decimal(str(event["amount"]))
            self.cash[account] -= amount
            self.cash[event["to_account"]] += amount
            return
        if side not in ("buy", "sell", "fund_buy", "fund_confirm", "fund_redeem", "assign", "custody_transfer"):
            return
        kind = channel(event)
        strategy = event.get("strategy", "core")  # 早期场外流水默认归入长期配置。
        key = account, kind, strategy, event["code"]
        qty = Decimal(str(event["qty"]))
        if side == "assign":
            self.positions[account, kind, event["from_strategy"], event["code"]] -= qty
        if side == "custody_transfer":
            self.positions[event["to_account"], kind, strategy, event["code"]] += qty
        self.positions[key] += -qty if side in ("sell", "fund_redeem", "custody_transfer") else qty

    def view(self):
        return {"custody_cash": {k: float(v) for k, v in self.cash.items()},
                "custody_positions": [{"account": a, "kind": k, "strategy": s, "code": c, "qty": float(q)}
                                     for (a, k, s, c), q in self.positions.items() if q]}
