"""投资账户的流水校验与重放，根据已确认流水推导账户事实。"""

from bisect import bisect_right
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import math
import re

from data_provider.codes import canonical_stock_code, is_a_stock_code, is_etf_code
from data_provider.security_names import security_name
from src import portfolio_config

FUND_TRADES = {"fund_buy", "fund_redeem", "fund_pending", "fund_confirm", "fund_void"}


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def money(value):
    if not positive(value):
        raise ValueError("金额或价格须为有限正数")
    return float(value)


def validate_day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value or day > date.today():
        raise ValueError("日期须有效且不能晚于今天")
    return value


def cash_allocation(values, total):
    if not isinstance(values, dict) or set(values) - {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
        raise ValueError("现金分配无效")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 or round(v, 2) != v for v in values.values()):
        raise ValueError("现金分配须为非负金额，最多两位小数")
    allocated = {key: values.get(key, 0) for key in portfolio_config.STRATEGY_NAMES}
    remaining = round(total - sum(allocated.values()), 2)
    if remaining < 0:
        raise ValueError("分配金额超过账户现金")
    return {**allocated, "unassigned": remaining}


def fund_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("请输入六位场外基金代码")
    return value


def _fund_market_value(qty, nav):
    amount = Decimal(str(qty)) * Decimal(str(nav))
    return float(amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def _subscription_fee(fee, amount):
    fee = portfolio_config.validate_subscription_fee(fee)
    if fee["kind"] == "fixed" and fee["value"] >= amount:
        raise ValueError("申购费用与实际扣款不匹配")
    return fee


def net_subscription(amount, fee):
    net, value = Decimal(str(amount)), Decimal(str(fee["value"]))
    if fee["kind"] == "rate":
        net /= 1 + value / 100
    elif fee["kind"] == "fixed":
        net -= value
    return net.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def holding_kind(code):
    if is_a_stock_code(code):
        return "stock"
    # 行情路由还把 16、18 码族归入基金接口，对账时将它们单列。
    if is_etf_code(code) and not code.startswith(("16", "18")):
        return "etf"
    return "other"


def _trade_fee(rules, code, side, qty, price):
    kind = holding_kind(code)
    rule = rules.get(kind)
    if rule is None:
        return {}
    amount = qty * Decimal(str(price))
    commission = max(Decimal(str(rule["min_commission"])), amount * Decimal(str(rule["commission_per_10000"])) / 10000)
    stamp_rate = rules.get("stock_sell_stamp_duty_per_10000", 0) if kind == "stock" and side == "sell" else 0
    stamp_duty = amount * Decimal(str(stamp_rate)) / 10000
    commission = commission.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    stamp_duty = stamp_duty.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return {"fee": float(commission + stamp_duty), "commission": float(commission), "stamp_duty": float(stamp_duty)}


def _fund_event(payload, view, day):
    side = payload["side"]
    if side == "fund_void":
        # 作废只引用在途申购，不带 code/amount：金额与标的取自原申购流水。
        pending_id = payload.get("voided_pending_id")
        pending = view["fund_pending"].get(pending_id)
        if not pending:
            raise ValueError("待确认申购不存在或已确认")
        return {"code": pending["code"], "amount": pending["amount"],
                "strategy": "core", "voided_pending_id": pending_id}
    code = fund_code(payload.get("code"))
    amount = payload.get("amount")
    if not positive(amount) or round(amount, 2) != amount:
        raise ValueError("实际金额须为正数，最多两位小数")
    if side in {"fund_buy", "fund_pending"} and amount > view["cash_by_strategy"]["core"]:
        raise ValueError("多资产配置现金不足，请先分配现金")
    result = {"code": code, "amount": amount, "strategy": "core"}
    if side == "fund_pending":
        nav_date = validate_day(payload.get("nav_date", ""))
        if nav_date > day:
            raise ValueError("买入日不能晚于记账日")
        fee = _subscription_fee(payload.get("subscription_fee"), amount)
        if net_subscription(amount, fee) <= 0:
            raise ValueError("扣除申购费后的金额不足一分")
        return {**result, "nav_date": nav_date, "subscription_fee": fee}
    qty = payload.get("qty")
    if not positive(qty) or qty > 1e9 or round(qty, 6) != qty:
        raise ValueError("确认份额须为正数，最多六位小数")
    if side == "fund_redeem" and qty > view["fund_positions"].get(code, {}).get("qty", 0):
        raise ValueError("赎回份额超过已确认持仓")
    result["qty"] = qty
    if side in {"fund_buy", "fund_confirm"} and "subscription_fee" in payload:
        result["subscription_fee"] = _subscription_fee(payload["subscription_fee"], amount)
    if side == "fund_confirm" or (side == "fund_buy" and (payload.get("nav") is not None or payload.get("nav_date"))):
        nav, nav_date = money(payload.get("nav")), validate_day(payload.get("nav_date", ""))
        if nav_date > day or nav > 1e8 or round(nav, 8) != nav:
            raise ValueError("单位净值最多八位小数，净值日期不得晚于记账日")
        result.update(nav=nav, nav_date=nav_date)
    if side == "fund_confirm":
        pending_id = payload.get("pending_id")
        pending = view["fund_pending"].get(pending_id)
        if not pending or any(result.get(key) != pending[key]
                              for key in ("code", "amount", "nav_date", "subscription_fee")):
            raise ValueError("确认流水须对应同一笔待确认申购")
        result["pending_id"] = pending_id
    return result


def make_event(payload, request_id, day, view, *, config=None):
    """用流水发生前的账户事实校验并生成流水。"""
    side = payload.get("side")
    if side not in {"buy", "sell", "dividend", "deposit", "withdraw", "assign", "cash_assign", "cash_transfer", *FUND_TRADES}:
        raise ValueError("不支持的记账类型")
    event = {"id": request_id, "date": day, "side": side, "request": payload}
    if side in FUND_TRADES:
        fund_payload = payload
        fund_subscription_fee = (config or {}).get("fund_subscription_fee")
        if side in {"fund_pending", "fund_buy"} and fund_subscription_fee is not None:
            fund_payload = {**payload, "subscription_fee": fund_subscription_fee}
        event.update(_fund_event(fund_payload, view, day))
    elif side == "cash_assign":
        event["cash_allocations"] = cash_allocation(payload.get("cash_allocations"), view["cash"])
    elif side == "cash_transfer":
        source, target = payload.get("from_strategy"), payload.get("strategy")
        if source not in view["cash_by_strategy"] or target not in view["cash_by_strategy"] or source == target:
            raise ValueError("请选择不同的现金划转来源和目标")
        amount = round(money(payload.get("amount")), 2)
        if amount <= 0 or amount > view["cash_by_strategy"][source]:
            raise ValueError("划转金额无效或超过来源策略现金")
        event.update(from_strategy=source, strategy=target, amount=amount)
    elif side == "assign":
        source, target = payload.get("from_strategy"), payload.get("strategy")
        if source not in {*portfolio_config.STRATEGY_NAMES, "unassigned"} or target not in {*portfolio_config.STRATEGY_NAMES, "unassigned"} or source == target:
            raise ValueError("请选择不同的分配来源和目标")
        code, qty = canonical_stock_code(payload.get("code", "")), payload.get("qty")
        held = next((r for r in view["strategy_positions"] if r["code"] == code and r["strategy"] == source), None)
        if not positive(qty) or int(qty) != qty or not held or qty > held["count"]:
            raise ValueError("分配份额无效或超过来源持仓")
        event.update(code=code, name=held["name"], qty=int(qty), from_strategy=source, strategy=target,
                     transfer_price=view["prices"][code])
    elif side in {"buy", "sell"}:
        qty, price = payload.get("qty"), money(payload.get("price"))
        if not positive(qty) or int(qty) != qty or (side == "buy" and qty % 100):
            raise ValueError("份额须为正整数，买入须为100份的整数倍")
        qty = int(qty)
        code = canonical_stock_code(payload.get("code", ""))
        if len(code) != 6 or not code.isdigit():
            raise ValueError("请输入六位证券代码")
        if "fee" not in payload and config is not None:
            event.update(_trade_fee(config.get("trade_fees", {}), code, side, qty, price))
        fee = payload.get("fee", event.get("fee", 0))
        if type(fee) not in (int, float) or not math.isfinite(fee) or fee < 0:
            raise ValueError("手续费须为有限非负数")
        if side == "sell":
            held = next((r for r in view["strategy_positions"] if r["code"] == code and r["strategy"] == payload.get("strategy", "unassigned")), None)
            if not held or qty > held["count"]:
                raise ValueError("卖出份额超过实际持仓")
        strategy = payload.get("strategy", "unassigned")
        if strategy not in {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
            raise ValueError("策略标记无效")
        if side == "buy" and round(qty * price + fee, 2) > view["cash_by_strategy"][strategy]:
            raise ValueError("该策略现金不足，请先分配现金")
        if side == "sell" and round(view["cash_by_strategy"][strategy] + qty * price - fee, 2) < 0:
            raise ValueError("该策略现金不足以扣除费用")
        event.update(code=code, qty=qty, price=price, fee=float(fee), strategy=strategy)
    else:
        strategy = payload.get("strategy", "unassigned")
        if strategy not in {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
            raise ValueError("策略标记无效")
        amount = round(money(payload.get("amount")), 2)
        if amount <= 0 or (side == "withdraw" and amount > view["cash_by_strategy"][strategy]):
            raise ValueError("金额无效或超过实际现金")
        if side == "withdraw" and amount >= view["equity"]:
            raise ValueError("暂不支持全额清户，请保留正的账户资产")
        event["amount"] = amount
        event["strategy"] = strategy
        if side in {"deposit", "withdraw"}:
            event["valuation_prices"] = dict(view["prices"])
            funds = view["fund_positions"]
            if funds:
                event["fund_valuation_prices"] = {code: row["book_price"] for code, row in funds.items()}
    return event


class _Replay:
    """按流水日期推进余额、持仓及记账估值。"""

    def __init__(self, state):
        initial = state["initial"]
        self.initial_equity = self.units = initial["equity"]
        self.cash = initial["cash"]
        self.cash_by_strategy = cash_allocation(initial.get("cash_allocations", {}), self.cash)
        self.held = {}
        self.marks = {}
        self.fund_pending = {}
        self.fund_shares = {}
        self.fund_cost = {}
        self.fund_trade_marks = {}
        self.fund_history = {code: sorted(history.items()) for code, history in state.get("fund_navs", {}).items()}
        self.net_capital = dict.fromkeys((*portfolio_config.STRATEGY_NAMES, "unassigned"), 0.0)
        for row in initial["positions"]:
            code = row["code"]
            strategy = row.get("strategy", "unassigned")
            self.held[strategy, code] = {**row, "name": security_name(code),
                                         "strategy": strategy, "avail_count": row["count"]}
            self.marks[code] = row["price"]
            self.net_capital[strategy] += row["count"] * row["price"]
        self.net_flows = 0.0
        self.day = None
        self.day_closes = {}
        self.records = iter(sorted(state["records"], key=lambda row: row["date"]))
        self.record = next(self.records, None)

    def _advance(self, day):
        if day == self.day:
            return
        if self.day is not None:
            self.marks.update(self.day_closes)
            for row in self.held.values():
                row["avail_count"] = row["count"]
        self.day = day
        self.day_closes = {}
        while self.record and self.record["date"] <= day:
            # 当日收盘价可以用于查看和校验，流水推进时沿用交易前已有的价格。
            marks = self.day_closes if self.record["date"] == day else self.marks
            marks.update({row["code"]: row["close"] for row in self.record["positions"]
                          if positive(row.get("close"))})
            self.record = next(self.records, None)

    def _fund_mark(self, code):
        marks = self.fund_history.get(code, ())
        index = bisect_right(marks, (self.day, math.inf)) - 1
        published = marks[index] if index >= 0 else ("", None)
        confirmed = self.fund_trade_marks.get(code, ("", None))
        return max((confirmed, published), key=lambda mark: mark[0])

    def _fund_positions(self, prices=None):
        rows = {}
        for code, qty in self.fund_shares.items():
            if qty <= 0:
                continue
            nav_date, nav = self._fund_mark(code)
            # 缺净值时，内部资金流暂用成本单价，展示与收盘成绩标为待估值。
            basis = nav if nav is not None else float(self.fund_cost[code] / qty)
            value = _fund_market_value(qty, (prices or {}).get(code, basis))
            rows[code] = {"code": code, "qty": float(qty), "nav": nav, "date": nav_date or None,
                          "market_value": value, "book_price": basis}
        return rows

    def _pending_fund_value(self):
        return round(math.fsum(row["amount"] for row in self.fund_pending.values()), 2)

    def _apply_fund_trade(self, event):
        side = event["side"]
        if side == "fund_pending":
            self.fund_pending[event["id"]] = {k: v for k, v in event.items() if k != "request"}
            change = -event["amount"]
        elif side == "fund_void":
            # 作废补偿流水：撤销在途申购，现金原路退回（扣款失败/误记）。
            del self.fund_pending[event["voided_pending_id"]]
            change = event["amount"]
        else:
            change = self._apply_fund_shares(event)
        self.cash += change
        self.cash_by_strategy["core"] += change
        self.net_capital["core"] -= change

    def _apply_fund_shares(self, event):
        side = event["side"]
        code = event["code"]
        qty, amount = Decimal(str(event["qty"])), Decimal(str(event["amount"]))
        held = self.fund_shares.get(code, Decimal(0))
        cost = self.fund_cost.get(code, Decimal(0))
        if side in {"fund_buy", "fund_confirm"}:
            self.fund_shares[code] = held + qty
            self.fund_cost[code] = cost + amount
            if "nav" in event and event["nav_date"] >= self.fund_trade_marks.get(code, ("", None))[0]:
                self.fund_trade_marks[code] = (event["nav_date"], event["nav"])
            if side == "fund_confirm":
                del self.fund_pending[event["pending_id"]]
                return 0.0
            return -float(amount)
        if qty > held:
            raise ValueError("赎回份额超过已确认持仓")
        self.fund_shares[code] = held - qty
        self.fund_cost[code] = cost * (held - qty) / held
        return float(amount)

    def apply(self, event):
        self._advance(event["date"])
        side = event["side"]
        strategy = event.get("strategy", "unassigned")
        if side in {"deposit", "withdraw"}:
            self.marks.update(event.get("valuation_prices", {}))
            equity = self.cash + sum(row["count"] * self.marks[row["code"]] for row in self.held.values())
            equity += self._pending_fund_value() + sum(row["market_value"]
                          for row in self._fund_positions(event.get("fund_valuation_prices")).values())
            delta = event["amount"] * (1 if side == "deposit" else -1)
            self.units += delta / (equity / self.units)
            self.cash += delta
            self.cash_by_strategy[strategy] += delta
            self.net_flows += delta
        elif side in FUND_TRADES:
            self._apply_fund_trade(event)
        elif side == "dividend":
            self.cash += event["amount"]
            self.cash_by_strategy[strategy] += event["amount"]
            self.net_capital[strategy] -= event["amount"]
        elif side == "cash_assign":
            self.cash_by_strategy = cash_allocation(event["cash_allocations"], self.cash)
        elif side == "cash_transfer":
            self.cash_by_strategy[event["from_strategy"]] -= event["amount"]
            self.cash_by_strategy[event["strategy"]] += event["amount"]
        elif side == "assign":
            code, qty = event["code"], event["qty"]
            strategy = event["strategy"]
            source = self.held[event["from_strategy"], code]
            target = self.held.setdefault((strategy, code), {
                "code": code, "name": source["name"], "count": 0, "avail_count": 0, "strategy": strategy,
            })
            available = min(qty, source["avail_count"])
            source["count"] -= qty
            source["avail_count"] -= available
            target["count"] += qty
            target["avail_count"] += available
            transfer_value = qty * event.get("transfer_price", self.marks[code])
            self.net_capital[event["from_strategy"]] -= transfer_value
            self.net_capital[strategy] += transfer_value
        else:
            code, qty, price = event["code"], event["qty"], event["price"]
            name = security_name(code)
            row = self.held.setdefault((strategy, code), {
                "code": code, "name": name, "strategy": strategy, "count": 0, "avail_count": 0,
            })
            row["name"] = name
            self.marks[code] = price
            if side == "buy":
                row["count"] += qty
                amount = round(qty * price + event["fee"], 2)
                self.cash -= amount
                self.cash_by_strategy[strategy] -= amount
                self.net_capital[strategy] += amount
            else:
                row["count"] -= qty
                row["avail_count"] = max(0, row["avail_count"] - qty)
                amount = round(qty * price - event["fee"], 2)
                self.cash += amount
                self.cash_by_strategy[strategy] += amount
                self.net_capital[strategy] -= amount

    def view(self, day, prices=None):
        self._advance(day)
        marks = {**self.marks, **self.day_closes, **(prices or {})}
        strategy_rows = [
            {"code": code, "name": row["name"], "count": row["count"],
             "strategy": strategy, "avail_count": row["avail_count"], "cost_price": None,
             "close": marks[code], "current_price": marks[code],
             "market_value": round(row["count"] * marks[code], 2)}
            for (strategy, code), row in self.held.items() if row["count"] > 0
        ]
        combined = {}
        for row in strategy_rows:
            total = combined.setdefault(row["code"], {**row, "count": 0, "avail_count": 0, "market_value": 0})
            total.pop("strategy", None)
            total["count"] += row["count"]
            total["avail_count"] += row["avail_count"]
            total["market_value"] = round(total["count"] * total["close"], 2)
        rows = list(combined.values())
        funds = self._fund_positions()
        pending_value = self._pending_fund_value()
        fund_value = round(sum(row["market_value"] for row in funds.values()) + pending_value, 2)
        invested = round(sum(row["market_value"] for row in rows) + fund_value, 2)
        equity = round(self.cash + invested, 2)
        strategy_pnl = {
            key: round(sum(row["count"] * marks[row["code"]] for row in self.held.values()
                           if row["strategy"] == key) + (fund_value if key == "core" else 0) - capital, 2) or 0.0
            for key, capital in self.net_capital.items()
        }
        return {
            "positions": rows, "strategy_positions": strategy_rows, "fund_positions": funds,
            "fund_pending": dict(self.fund_pending), "pending_fund_value": pending_value,
            "cash": round(self.cash, 2),
            "invested": invested, "equity": equity, "units": self.units, "receivables": 0,
            "prices": marks, "strategy_pnl": strategy_pnl,
            "cash_by_strategy": {key: round(value, 2) or 0.0 for key, value in self.cash_by_strategy.items()},
            "pnl": round(equity - self.initial_equity - self.net_flows, 2) or 0.0,
        }


def snapshot(state, asof, prices=None):
    """重放流水，按指定日期返回账户事实。"""
    replay = _Replay(state)
    for event in state["events"]:
        if event["date"] <= asof:
            replay.apply(event)
    return replay.view(asof, prices)


def replay_edit(state, index, payload, min_day, max_day):
    """应用更正并验证其余流水，返回可保存的完整流水序列。"""
    old = state["events"][index]
    if old["side"] in {"fund_pending", "fund_confirm"} or payload.get("side") in {"fund_pending", "fund_confirm"}:
        if old["side"] != payload.get("side"):
            raise ValueError("不能更改场外申购流水的类型")
    if old["side"] == "fund_confirm":
        if any(payload.get(key) != old[key] for key in ("date", "code", "amount", "nav", "nav_date", "subscription_fee")):
            raise ValueError("已确认申购仅可更正份额")
        payload = {**payload, "pending_id": old["pending_id"]}
    day = validate_day(payload.get("date", ""))
    if not min_day <= day <= max_day:
        raise ValueError("更正日期须在相邻流水日期之间")
    replay = _Replay(state)
    events = []
    for i, stored in enumerate(state["events"]):
        candidate = payload if i == index else stored
        try:
            event_day = day if i == index else validate_day(stored["date"])
            checked = make_event(candidate, stored["id"], event_day, replay.view(event_day))
            if i == index and "commission" in stored and all(
                checked.get(key) == stored[key] for key in ("side", "code", "qty", "price", "fee")
            ):
                checked.update(commission=stored["commission"], stamp_duty=stored["stamp_duty"])
            # 已确认流水沿用记录的划转估值和资金流价格。
            event = checked if i == index else stored
            replay.apply(event)
        except (ValueError, KeyError, ZeroDivisionError) as error:
            raise ValueError(f"更正会使第{i + 1}笔流水（{candidate['date']}）无效：{error}") from error
        events.append(event)
    return events
