"""投资账户台账：期初快照、人工流水、收盘估值和调仓建议。"""

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import logging
import math
from pathlib import Path
import re

from data_provider import canonical_stock_code
from data_provider.codes import is_a_stock_code, is_etf_code
from data_provider.security_names import security_name
from src.trading_calendar import latest_trading_day_on_or_before

FUND_TRADES = {"fund_buy", "fund_redeem"}
FILENAME = "etf_account.json"
ACCOUNT_PATH = Path("data") / FILENAME
logger = logging.getLogger(__name__)
SHANGHAI = timezone(timedelta(hours=8))


def _today():
    return datetime.now(SHANGHAI).date()


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _money(value):
    if not _positive(value):
        raise ValueError("金额或价格须为有限正数")
    return float(value)


def _day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value or day > date.today():
        raise ValueError("日期须有效且不能晚于今天")
    return value


from src.json_store import locked as _locked, save as _json_save
from src import portfolio_config


def _save(path, state):
    if "_config_revision" in state:
        if portfolio_config.current_revision(portfolio_config.path_for(path)) != state["_config_revision"]:
            raise ValueError("配置已更新，请刷新后重试")
        state = {k: v for k, v in state.items() if k not in {"targets", "budgets", "_config", "_config_revision"}}
    _json_save(path, state)


def load(path=ACCOUNT_PATH):
    if not Path(path).exists():
        raise FileNotFoundError("投资账户尚未初始化")
    config = portfolio_config.load(portfolio_config.path_for(path))
    return _read_account(path, config)


def _read_account(path, config):
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    state.update(targets={r["code"]: r["weight"] for r in config["core"]},
                 budgets=config["budgets"], _config=config,
                 _config_revision=portfolio_config.revision(config))
    return state


def revision(state, asof=None):
    facts = {"initial": state["initial"], "events": [e for e in state["events"]
             if asof is None or (e["date"] <= asof and e["side"] not in {"assign", "cash_assign", "cash_transfer"})]}
    if asof is None:
        facts["budgets"] = state.get("budgets", {})
        facts["targets"] = state.get("targets", {})
        if "_config_revision" in state:
            facts["configuration"] = state["_config_revision"]
        for key in ("quality_signal", "quality_state"):
            if key in state:
                facts[key] = state[key]
        if "rotation_signal" in state:
            facts["rotation_signal"] = state["rotation_signal"]
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initialize(path, *, asof, cash, positions, targets=None, budgets=None, cash_allocations=None):
    """期初价格用于建立收益起点，不伪造成交成本或历史交易。"""
    _day(asof)
    if type(cash) not in (int, float) or not math.isfinite(cash) or cash < 0:
        raise ValueError("现金须为有限非负数")
    budgets = dict(budgets or {})
    if set(budgets) - portfolio_config.STRATEGY_NAMES.keys() or any(type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1 for w in budgets.values()) or sum(budgets.values()) > 1:
        raise ValueError("策略预算比例无效")
    rows, codes = [], set()
    for item in positions:
        code = canonical_stock_code(item["code"])
        count, price = item["count"], _money(item["price"])
        strategy = item.get("strategy", "unassigned")
        if strategy not in {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
            raise ValueError("策略标记无效")
        if (strategy, code) in codes or not _positive(count) or int(count) != count:
            raise ValueError("代码不得重复，份额须为正整数")
        codes.add((strategy, code))
        rows.append({"code": code, "count": int(count), "price": price, "strategy": strategy})
    if not rows:
        raise ValueError("初始化持仓不能为空")
    targets = {canonical_stock_code(c): float(w) for c, w in (targets or {}).items()}
    if any(not math.isfinite(w) or not 0 <= w <= 1 for w in targets.values()) or sum(targets.values()) > 1:
        raise ValueError("目标权重无效")
    targets.setdefault("CASH", max(0, 1 - sum(targets.values())))
    allocations = _cash_allocation(cash_allocations or {}, cash)
    equity = round(cash + sum(r["count"] * r["price"] for r in rows), 2)
    state = {"initial": {"date": asof, "cash": cash, "cash_allocations": allocations,
                         "positions": rows, "equity": equity},
             "targets": targets, "budgets": budgets, "events": [], "records": [], "dismissed": []}
    mark = snapshot(state, asof)
    state["records"] = [{**mark, "date": asof, "nav": 1.0, "benchmark_nav": None,
                         "issues": [], "source": "用户确认的期初估值",
                         "revision": revision(state, asof)}]
    with _locked(path):
        if Path(path).exists():
            raise ValueError("投资账户台账已存在，禁止覆盖初始化")
        config_path = portfolio_config.path_for(path)
        if config_path.exists():
            raise ValueError("账户配置已存在，请使用迁移或已有配置初始化")
        config = portfolio_config.from_legacy(state)
        portfolio_config.create(config_path, config)
        state.update(_config=config, _config_revision=portfolio_config.revision(config))
        _save(path, state)
    return state


def _cash_allocation(values, total):
    if not isinstance(values, dict) or set(values) - {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
        raise ValueError("现金分配无效")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 or round(v, 2) != v for v in values.values()):
        raise ValueError("现金分配须为非负金额，最多两位小数")
    allocated = {key: values.get(key, 0) for key in portfolio_config.STRATEGY_NAMES}
    remaining = round(total - sum(allocated.values()), 2)
    if remaining < 0:
        raise ValueError("分配金额超过账户现金")
    return {**allocated, "unassigned": remaining}


class _Replay:
    """按流水日期推进余额、持仓及记账估值。"""

    def __init__(self, state):
        initial = state["initial"]
        self.initial_equity = self.units = initial["equity"]
        self.cash = initial["cash"]
        self.cash_by_strategy = _cash_allocation(initial.get("cash_allocations", {}), self.cash)
        self.held = {}
        self.marks = {}
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
                          if _positive(row.get("close"))})
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

    def _apply_fund_trade(self, event):
        code = event["code"]
        qty, amount = Decimal(str(event["qty"])), Decimal(str(event["amount"]))
        held = self.fund_shares.get(code, Decimal(0))
        cost = self.fund_cost.get(code, Decimal(0))
        if event["side"] == "fund_buy":
            self.fund_shares[code] = held + qty
            self.fund_cost[code] = cost + amount
            if "nav" in event and event["nav_date"] >= self.fund_trade_marks.get(code, ("", None))[0]:
                self.fund_trade_marks[code] = (event["nav_date"], event["nav"])
            amount = -amount
        else:
            if qty > held:
                raise ValueError("赎回份额超过已确认持仓")
            self.fund_shares[code] = held - qty
            self.fund_cost[code] = cost * (held - qty) / held
        change = float(amount)
        self.cash += change
        self.cash_by_strategy["core"] += change
        self.net_capital["core"] -= change

    def apply(self, event):
        self._advance(event["date"])
        side = event["side"]
        strategy = event.get("strategy", "unassigned")
        if side in {"deposit", "withdraw"}:
            self.marks.update(event.get("valuation_prices", {}))
            equity = self.cash + sum(row["count"] * self.marks[row["code"]] for row in self.held.values())
            equity += sum(row["market_value"]
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
            self.cash_by_strategy = _cash_allocation(event["cash_allocations"], self.cash)
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
        fund_value = round(sum(row["market_value"] for row in funds.values()), 2)
        invested = round(sum(row["market_value"] for row in rows) + fund_value, 2)
        equity = round(self.cash + invested, 2)
        strategy_pnl = {
            key: round(sum(row["count"] * marks[row["code"]] for row in self.held.values()
                           if row["strategy"] == key) + (fund_value if key == "core" else 0) - capital, 2) or 0.0
            for key, capital in self.net_capital.items()
        }
        return {
            "positions": rows, "strategy_positions": strategy_rows, "fund_positions": funds,
            "cash": round(self.cash, 2),
            "invested": invested, "equity": equity, "units": self.units, "receivables": 0,
            "prices": marks, "strategy_pnl": strategy_pnl,
            "cash_by_strategy": {key: round(value, 2) or 0.0 for key, value in self.cash_by_strategy.items()},
            "pnl": round(equity - self.initial_equity - self.net_flows, 2) or 0.0,
        }


def snapshot(state, asof, prices=None):
    """重放已确认流水，按指定日期返回账户事实。"""
    replay = _Replay(state)
    for event in state["events"]:
        if event["date"] <= asof:
            replay.apply(event)
    return replay.view(asof, prices)


def suggestions(state, view, price_day):
    from src.etf.rebalancer import ETFRebalancer

    grouped_codes = {row["code"] for row in state["_config"]["core"] if portfolio_config.grouped(row)}
    engine = ETFRebalancer(asset for asset in baseline(state) if asset.code not in grouped_codes)
    plan = engine.build_plan(state["targets"], view["positions"], view["equity"], prices=view["prices"])
    rev, result = revision(state), []
    for order in plan.orders:
        price = view["prices"][order.code]
        identity = hashlib.sha256(
            f"{rev}:{price_day}:{order.code}:{order.action}:{order.qty}:{price}".encode()
        ).hexdigest()[:24]
        if identity in state["dismissed"]:
            continue
        result.append({"id": identity, "code": order.code, "name": order.name,
                       "side": order.action, "qty": order.qty, "price": price,
                       "amount": round(order.qty * price, 2),
                       "gap_amount": round(order.amount, 2),
                       "current_pct": order.current_pct, "target_pct": order.target_pct,
                       "current_value": round(order.current_pct * view["equity"], 2),
                       "target_value": round(order.target_pct * view["equity"], 2),
                       "price_date": price_day, "price_issue": None, "reason": order.reason})
    cash = view["cash"]
    sale_proceeds = sum(r["amount"] or 0 for r in result if r["side"] == "sell")
    projected = cash + sale_proceeds
    for item in result:
        item["cash_note"] = None
        if item["side"] != "buy" or item["amount"] is None:
            continue
        amount = item["amount"]
        if amount <= cash:
            cash -= amount
        elif amount <= projected:
            item["cash_note"] = "需先卖出回款"
            cash = 0
        else:
            item["cash_note"] = "现金不足，需补充资金或减少买入"
            cash = 0
        projected -= amount
    return result


def baseline(state):
    return portfolio_config.allocations(state["_config"])


def _fund_market_value(qty, nav):
    amount = Decimal(str(qty)) * Decimal(str(nav))
    return float(amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def _fund_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("请输入六位场外基金代码")
    return value


def _fund_event(payload, view, day):
    code = _fund_code(payload.get("code"))
    side = payload["side"]
    qty, amount = payload.get("qty"), payload.get("amount")
    if not _positive(qty) or qty > 1e9 or round(qty, 6) != qty:
        raise ValueError("确认份额须为正数，最多六位小数")
    if not _positive(amount) or round(amount, 2) != amount:
        raise ValueError("确认金额须为正数，最多两位小数")
    if side == "fund_buy" and amount > view["cash_by_strategy"]["core"]:
        raise ValueError("多资产配置现金不足，请先分配现金")
    if side == "fund_redeem" and qty > view["fund_positions"].get(code, {}).get("qty", 0):
        raise ValueError("赎回份额超过已确认持仓")
    result = {"code": code, "qty": qty, "amount": amount, "strategy": "core"}
    if side == "fund_buy" and "subscription_fee" in payload:
        fee = payload["subscription_fee"]
        if not isinstance(fee, dict) or set(fee) != {"kind", "value"}:
            raise ValueError("申购费用设置无效")
        kind, value = fee["kind"], fee["value"]
        if kind not in ("none", "rate", "fixed") or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("申购费用设置无效")
        if (kind == "none" and value != 0) or (kind == "fixed" and value >= amount):
            raise ValueError("申购费用与实际扣款不匹配")
        result["subscription_fee"] = {"kind": kind, "value": value}
    if side == "fund_buy" and (payload.get("nav") is not None or payload.get("nav_date")):
        nav, nav_date = _money(payload.get("nav")), _day(payload.get("nav_date", ""))
        if nav_date > day or nav > 1e8 or round(nav, 8) != nav:
            raise ValueError("单位净值最多八位小数，净值日期不得晚于记账日")
        result.update(nav=nav, nav_date=nav_date)
    return result


def lookup_fund_nav(_path, payload):
    """按申购适用日期查询单位净值。"""
    from data_provider.fund_nav import latest_fund_nav

    code, day = _fund_code(payload.get("code")), _day(payload.get("date", ""))
    mark = latest_fund_nav(code, day)
    if mark is None or mark[0] != day:
        raise ValueError("该买入日的单位净值暂不可用，请确认日期或稍后重试")
    return {"date": day, "nav": mark[1]}


def refresh_fund_navs(path, through):
    """查询账户内持仓的公布净值，再按日期合并到估值记录。"""
    from data_provider.fund_nav import latest_fund_nav

    result = {"updated": [], "unchanged": [], "failed": []}
    marks = {}
    funds = snapshot(load(path), through)["fund_positions"]
    for code, fund in funds.items():
        if fund["date"] == through:
            result["unchanged"].append(code)
            continue
        try:
            mark = latest_fund_nav(code, through)
        except Exception:
            logger.exception("场外基金 %s 净值获取失败", code)
            mark = None
        if mark is None:
            result["failed"].append(code)
        elif mark[0] > (fund["date"] or ""):
            marks[code] = mark
        else:
            result["unchanged"].append(code)
    if not marks:
        return result

    with _locked(path):
        state = load(path)
        current = snapshot(state, through)["fund_positions"]
        for code, (nav_date, nav) in marks.items():
            if code not in current or (current[code]["date"] or "") >= nav_date:
                result["unchanged"].append(code)
                continue
            state.setdefault("fund_navs", {}).setdefault(code, {})[nav_date] = nav
            result["updated"].append(code)
        if result["updated"]:
            _save(path, state)
    return result


def _allocation_groups(config, strategy, funds, prices):
    ungrouped_listed = {row["code"]: row for row in strategy["positions"]}
    ungrouped_funds = dict(funds)
    groups = []
    for asset in config["core"]:
        code = asset["code"]
        if code == "CASH":
            groups.append({"id": code, "name": "现金", "weight": asset["weight"],
                           "market_value": strategy["cash"], "products": []})
            continue
        listed_codes = [] if portfolio_config.fund_only(asset) else [code, *asset.get("etf_codes", [])]
        products = []
        for member in listed_codes:
            position = ungrouped_listed.pop(member, None)
            if not position:
                position = {"code": member, "name": security_name(member), "count": 0, "market_value": 0}
            products.append({**position, "kind": "listed"})
        for member in asset.get("fund_codes", []):
            position = ungrouped_funds.pop(member, None)
            products.append({**(position or {"code": member, "market_value": 0}), "kind": "fund"})
        first_product = products[0]
        values = [row["market_value"] for row in products]
        groups.append({
            "id": code,
            "name": asset.get("label") or first_product.get("name") or f"场外基金 {first_product['code']}",
            "weight": asset["weight"],
            "market_value": None if None in values else round(sum(values), 2),
            "products": products,
            "reference_price_missing": not portfolio_config.grouped(asset) and code not in prices,
        })
    groups.extend({"id": row["code"], "name": row["name"], "weight": 0, "market_value": row["market_value"],
                   "products": [{**row, "kind": "listed"}], "outside": True} for row in ungrouped_listed.values())
    groups.extend({"id": f"outside-fund:{code}", "name": f"场外基金 {code}", "weight": 0,
                   "market_value": row["market_value"], "products": [{**row, "kind": "fund"}], "outside": True}
                  for code, row in ungrouped_funds.items())
    return groups


def _strategy_facts(state, view, key):
    """按策略标识提供账户事实，不计算建议；供展示、发布和成交处理共用。"""
    name = portfolio_config.STRATEGY_NAMES[key]
    positions = [r for r in view["strategy_positions"] if r["strategy"] == key]
    funds = list(view["fund_positions"].values()) if key == "core" else []
    holdings = positions + funds
    invested = (round(sum(r["market_value"] for r in holdings), 2)
                if all(r["market_value"] is not None for r in holdings) else None)
    weight = state.get("budgets", {}).get(key)
    budget = round(view["equity"] * weight, 2) if weight is not None and view["equity"] is not None else None
    remaining = max(0, round(budget - invested, 2)) if budget is not None and invested is not None else None
    cash = view["cash_by_strategy"][key]
    assets = round(invested + cash, 2) if invested is not None else None
    status = ("目标比例待设置" if weight is None
              else "待估值" if remaining is None
              else "已分配" if holdings or cash > 0 else "暂无持仓")
    thresholds = None
    if key == "core":
        from src.etf.config import MIN_TRADE_DEVIATION, REBALANCE_SINGLE_THRESHOLD
        thresholds = {"trade": MIN_TRADE_DEVIATION, "single": REBALANCE_SINGLE_THRESHOLD}
    return {"id": key, "name": name, "account_id": "etf", "positions": positions,
            "budget_pct": weight, "budget": budget, "invested": invested,
            "pnl": view["strategy_pnl"][key] if invested is not None else None,
            "target_weights": dict(state["targets"]) if key == "core" else None,
            "rebalance_thresholds": thresholds,
            "remaining_budget": remaining, "buying_power": min(cash, remaining) if remaining is not None else None,
            "suggestions": [], "trades": [e for e in state["events"]
                                           if e.get("strategy") == key or e.get("from_strategy") == key
                                           or e.get("side") == "cash_assign"],
            "history": [], "performance": None, "benchmark_name": None,
            "cash": cash, "assets": assets, "actual_pct": assets / view["equity"] if view["equity"] and assets is not None else None,
            "deviation": round(assets - budget, 2) if budget is not None and assets is not None else None, "status": status}


def strategy_views(state, view, *, today):
    """汇总策略事实与建议；策略标识决定归属，展示顺序不参与规则判定。"""
    # 独立信号沿用其发布与成交时的账户事实；配置份额使用统一收盘估值。
    signal_view = snapshot(state, today.isoformat()) if any(
        state.get(f"{key}_signal") for key in ("rotation", "quality")
    ) else view
    result = []
    for key in portfolio_config.STRATEGY_NAMES:
        row = _strategy_facts(state, view, key)
        if key == "rotation":
            from src.etf.rotation_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("rotation_signal"), _strategy_facts(state, signal_view, key), today,
                account_revision=revision(state), dismissed=state["dismissed"])
            row["signal_date"] = state.get("rotation_signal", {}).get("date")
        if key == "quality":
            from src.quality_pool.account_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("quality_signal"), _strategy_facts(state, signal_view, key), today,
                dismissed=state["dismissed"])
            row["signal_date"] = state.get("quality_signal", {}).get("date")
        if key == "core" and row["status"] == "已分配" and row["budget"] > 0:
            row["suggestions"] = suggestions(state, {**view, "positions": row["positions"],
                                                    "equity": row["budget"], "cash": row["cash"]},
                                             view["valuation"]["date"])
        result.append(row)
    return result


def publish_rotation(day, rows, prices, diag, *, path=None, dry_run=False, error=None):
    """锁内发布轮动信号并返回本次建议；账户写入校验与其他记账共用。"""
    from src.etf import rotation_advice

    path = Path(path or ACCOUNT_PATH)
    if not path.exists():
        return "投资账户尚未初始化"
    with _locked(path):
        state = load(path)
        signal = rotation_advice.make_signal(day, rows, prices, diag, error,
                                             previous=state.get("rotation_signal"))
        if signal is None:
            return "已有更新日期的账户建议，本次未覆盖"
        state["rotation_signal"] = signal
        strategy = _strategy_facts(state, snapshot(state, day), "rotation")
        suggestions, status = rotation_advice.advice(
            signal, strategy, date.fromisoformat(day),
            account_revision=revision(state), dismissed=state["dismissed"])
        if not dry_run:
            _save(path, state)
    return rotation_advice.render(suggestions, status)


def publish_quality(day, *, path=None, dry_run=False, retry=False):
    """质量池使用实际持仓独立生成名单；取数期间释放锁，提交时检查账户版本。"""
    from copy import deepcopy
    from src.quality_pool import account_advice, assembly, signal, state as pool_state
    from src.etf.rotation_advice import render

    path = Path(path or ACCOUNT_PATH)
    if not path.exists():
        return "投资账户尚未初始化"
    with _locked(path):
        account = load(path)
        expected = revision(account)
    previous = account.get("quality_signal")
    if previous and previous["date"] > day:
        return "已有更新日期的质量池建议，本次未覆盖"
    strategy = _strategy_facts(account, snapshot(account, day), "quality")
    if not strategy["budget"]:
        return "请先设置质量动量目标比例"
    if previous and previous["date"] == day and not previous.get("issues"):
        rows, status = account_advice.advice(previous, strategy, date.fromisoformat(day),
                                             dismissed=account["dismissed"])
        return render(rows, status)
    state = deepcopy(account.get("quality_state") or pool_state.empty_state())

    def commit(value, published=None):
        nonlocal expected
        if dry_run:
            return
        with _locked(path):
            current = load(path)
            if revision(current) != expected:
                raise ValueError("质量动量运行期间账户已更新，请重新运行")
            current["quality_state"] = deepcopy(value)
            current["quality_signal"] = published or {"date": day, "issues": "质量动量名单更新中，暂停建议"}
            _save(path, current)
            expected = revision(current)

    try:
        signal.run_signal(date.fromisoformat(day), dry_run=dry_run, retry=retry,
                          state=state, held_codes={p["code"] for p in strategy["positions"]},
                          state_writer=commit)
        if state.get("signal_retry"):
            raise ValueError("质量动量数据不完整，本轮暂停，等待重试")
        selected = (state.get("pending_plan") or {}).get("selected", [])
        execution_snapshot = assembly.build_snapshot(day, selected, account_facts=strategy)
        published = account_advice.make_signal(day, state, execution_snapshot, previous)
    except Exception as error:
        logger.exception("质量动量实际账户建议更新失败")
        published = {"date": day, "issues": str(error)}
    # 若用户在取数期间记账，提交失败，保留用户数据并要求重新生成建议。
    commit(state, published)
    rows, status = account_advice.advice(published, strategy, date.fromisoformat(day),
                                         dismissed=account["dismissed"])
    return render(rows, status)


def _make_event(payload, request_id, day, view):
    """用流水发生前的账户事实校验并生成流水。"""
    side = payload.get("side")
    if side not in {"buy", "sell", "dividend", "deposit", "withdraw", "assign", "cash_assign", "cash_transfer", *FUND_TRADES}:
        raise ValueError("不支持的记账类型")
    event = {"id": request_id, "date": day, "side": side, "request": payload}
    if side in FUND_TRADES:
        event.update(_fund_event(payload, view, day))
    elif side == "cash_assign":
        event["cash_allocations"] = _cash_allocation(payload.get("cash_allocations"), view["cash"])
    elif side == "cash_transfer":
        source, target = payload.get("from_strategy"), payload.get("strategy")
        if source not in view["cash_by_strategy"] or target not in view["cash_by_strategy"] or source == target:
            raise ValueError("请选择不同的现金划转来源和目标")
        amount = round(_money(payload.get("amount")), 2)
        if amount <= 0 or amount > view["cash_by_strategy"][source]:
            raise ValueError("划转金额无效或超过来源策略现金")
        event.update(from_strategy=source, strategy=target, amount=amount)
    elif side == "assign":
        source, target = payload.get("from_strategy"), payload.get("strategy")
        if source not in {*portfolio_config.STRATEGY_NAMES, "unassigned"} or target not in {*portfolio_config.STRATEGY_NAMES, "unassigned"} or source == target:
            raise ValueError("请选择不同的分配来源和目标")
        code, qty = canonical_stock_code(payload.get("code", "")), payload.get("qty")
        held = next((r for r in view["strategy_positions"] if r["code"] == code and r["strategy"] == source), None)
        if not _positive(qty) or int(qty) != qty or not held or qty > held["count"]:
            raise ValueError("分配份额无效或超过来源持仓")
        event.update(code=code, name=held["name"], qty=int(qty), from_strategy=source, strategy=target,
                     transfer_price=view["prices"][code])
    elif side in {"buy", "sell"}:
        qty, price = payload.get("qty"), _money(payload.get("price"))
        fee = payload.get("fee", 0)
        if not _positive(qty) or int(qty) != qty or (side == "buy" and qty % 100):
            raise ValueError("份额须为正整数，买入须为100份的整数倍")
        if type(fee) not in (int, float) or not math.isfinite(fee) or fee < 0:
            raise ValueError("手续费须为有限非负数")
        code = canonical_stock_code(payload.get("code", ""))
        if len(code) != 6 or not code.isdigit():
            raise ValueError("请输入六位证券代码")
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
        event.update(code=code, qty=int(qty), price=price, fee=float(fee), strategy=strategy)
    else:
        strategy = payload.get("strategy", "unassigned")
        if strategy not in {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
            raise ValueError("策略标记无效")
        amount = round(_money(payload.get("amount")), 2)
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


def record_event(path, payload):
    """锁内去重和版本检查，避免双击、重试或旧页面重复改账。"""
    with _locked(path):
        state = load(path)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError("缺少有效的记账标识")
        duplicate = next((e for e in state["events"] if e["id"] == request_id), None)
        if duplicate:
            if duplicate["request"] != payload:
                raise ValueError("同一记账标识的内容不一致")
            return duplicate
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        day = _day(payload.get("date", ""))
        last_day = state["events"][-1]["date"] if state["events"] else state["initial"]["date"]
        if day < last_day:
            raise ValueError("记账日期不能早于期初或上笔流水")
        view = snapshot(state, day)
        event = _make_event(payload, request_id, day, view)
        side = event["side"]
        if event.get("strategy") == "rotation":
            from src.etf.rotation_advice import on_event
            signal = on_event(state.get("rotation_signal"), event,
                              _strategy_facts(state, view, "rotation"), _today(),
                              account_revision=revision(state), dismissed=state["dismissed"])
            if signal is not None:
                state["rotation_signal"] = signal
        if event.get("strategy") == "quality" or event.get("from_strategy") == "quality":
            from src.quality_pool.account_advice import on_event
            if state.get("quality_signal"):
                state["quality_signal"] = on_event(state["quality_signal"], event)
            quality_state = state.get("quality_state")
            if quality_state and quality_state.get("pending_plan") and side in {"buy", "sell", "assign"}:
                # 名单生成后、建议发布前持仓变化，重试时须按新持仓重新选股。
                quality_state.update(pending_plan=None, signal_retry=True)
        state["events"].append(event)
        _save(path, state)
        return event


def cash_rebalance(path, payload):
    """按同一组展示估值预览现金再平衡，不保存；持仓超配时按现金缺口比例分配。"""
    today = _today()
    price_day = valuation_day(today)
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        view = _valuation_view(state, today, price_day)
        if view["equity"] is None:
            raise ValueError("部分持仓待估值，请先补齐估值数据再进行现金再平衡")
        strategies = {key: _strategy_facts(state, view, key) for key in portfolio_config.STRATEGY_NAMES}
        configured = set(state["budgets"])
        if not configured:
            raise ValueError("请先设置策略目标比例")
        cents = lambda value: int(round(value * 100))
        # 未设置目标的策略维持原现金；剩余目标中扣除这些策略已有资产。
        fixed = {key: cents(row["cash"]) for key, row in strategies.items() if key not in configured}
        available = cents(view["cash"]) - sum(fixed.values())
        desired = {key: max(0, cents(strategies[key]["budget"]) - cents(strategies[key]["invested"]))
                   for key in sorted(configured)}
        unassigned_invested = sum(r["market_value"] for r in view["strategy_positions"] if r["strategy"] == "unassigned")
        residual_target = cents(view["equity"]) - sum(cents(strategies[k]["budget"]) for k in configured)
        residual_target -= sum(cents(strategies[k]["assets"]) for k in fixed)
        desired["unassigned"] = max(0, residual_target - cents(unassigned_invested))
        demand = sum(desired.values())
        if demand > available:
            allocated = {key: value * available // demand for key, value in desired.items()}
            order = sorted(desired, key=lambda key: (-(desired[key] * available % demand), key))
            for key in order[:available - sum(allocated.values())]:
                allocated[key] += 1
        else:
            allocated = dict(desired)
            allocated["unassigned"] += available - demand
        allocated.update(fixed)
        rows = []
        for key in (*portfolio_config.STRATEGY_NAMES, "unassigned"):
            row = strategies.get(key)
            invested = row["invested"] if row else unassigned_invested
            target = row["budget"] if row else max(0, residual_target) / 100
            after_cash = allocated[key] / 100
            rows.append({"id": key, "name": row["name"] if row else "未分配",
                         "before_cash": view["cash_by_strategy"][key], "after_cash": after_cash,
                         "change": round(after_cash - view["cash_by_strategy"][key], 2),
                         "deviation": round(invested + after_cash - target, 2) if target is not None else None,
                         "holding_overweight": target is not None and invested > target})
        return {"cash_allocations": {key: allocated[key] / 100 for key in portfolio_config.STRATEGY_NAMES},
                "rows": rows, "shortfall": max(0, demand - available) / 100,
                "valuation": view["valuation"]}


def _edit_location(state, payload):
    if payload.get("revision") != revision(state):
        raise ValueError("账户已更新，请刷新后重新核对")
    events = state["events"]
    index = next((i for i, event in enumerate(events) if event["id"] == payload.get("event_id")), None)
    if index is None:
        raise ValueError("流水不存在")
    min_day = events[index - 1]["date"] if index else state["initial"]["date"]
    max_day = events[index + 1]["date"] if index + 1 < len(events) else _today().isoformat()
    return index, min_day, max_day


def event_context(path, payload):
    """返回目标流水发生前的余额与持仓，供更正表单校验。"""
    state = load(path)
    index, min_day, max_day = _edit_location(state, payload)
    event = state["events"][index]
    before = {**state, "events": state["events"][:index]}
    return {**snapshot(before, event["date"]), "event": {k: v for k, v in event.items() if k != "request"},
            "revision": payload["revision"], "start_date": state["initial"]["date"],
            "min_date": min_day, "max_date": max_day}


def edit_event(path, payload):
    """原子更正有效流水，保留修改记录；重放校验后续所有交易。"""
    with _locked(path):
        state = load(path)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError("缺少有效的记账标识")
        previous = next((r for r in state.get("event_edits", []) if r["id"] == request_id), None)
        if previous:
            if previous["request"] != payload:
                raise ValueError("同一记账标识的内容不一致")
            return previous["after"]
        index, min_day, max_day = _edit_location(state, payload)
        old = state["events"][index]
        day = _day(payload.get("date", ""))
        if not min_day <= day <= max_day:
            raise ValueError("更正日期须在相邻流水日期之间")
        replay = _Replay(state)
        events = []
        for i, stored in enumerate(state["events"]):
            candidate = payload if i == index else stored
            try:
                event_day = day if i == index else _day(stored["date"])
                checked = _make_event(candidate, stored["id"], event_day, replay.view(event_day))
                # 已确认流水沿用记录的划转估值和资金流价格。
                event = checked if i == index else stored
                replay.apply(event)
            except (ValueError, KeyError, ZeroDivisionError) as error:
                raise ValueError(f"更正会使第{i + 1}笔流水（{candidate['date']}）无效：{error}") from error
            events.append(event)
        state["events"] = events
        state.setdefault("event_edits", []).append({"id": request_id, "event_id": old["id"],
                                                   "request": payload, "before": old,
                                                   "after": state["events"][index]})
        state["dismissed"] = []
        # 已有操作计划基于旧账，不再沿用；下一次策略运行重新生成。
        for key in ("rotation_signal", "quality_signal"):
            if key in state:
                state[key] = {"date": _today().isoformat(), "issues": "历史流水已更正，等待重新生成建议"}
        if state.get("quality_state"):
            state["quality_state"].update(pending_plan=None, signal_retry=True)
        _save(path, state)
        return state["events"][index]


def set_budgets(path, payload):
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重试")
        budgets = payload.get("budgets")
        if not isinstance(budgets, dict) or set(budgets) - portfolio_config.STRATEGY_NAMES.keys():
            raise ValueError("预算配置无效")
        if any(type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1 for w in budgets.values()) or sum(budgets.values()) > 1:
            raise ValueError("预算合计不能超过100%")
        config = {**state["_config"], "budgets": budgets}
        portfolio_config.save_config(portfolio_config.path_for(path), config, state["_config_revision"])


def dismiss(path, payload):
    today = _today()
    price_day = valuation_day(today)
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        views = strategy_views(state, _valuation_view(state, today, price_day), today=today)
        available = {r["id"] for strategy in views for r in strategy["suggestions"]}
        if payload.get("id") not in available:
            raise ValueError("建议已变化，请刷新后重试")
        state["dismissed"].append(payload["id"])
        _save(path, state)


def collect_close(path, asof, close_prices, *, dry_run=False):
    with _locked(path):
        state = load(path)
        if asof <= state["initial"]["date"]:
            return state
        current = snapshot(state, asof)
        prices = close_prices([r["code"] for r in current["positions"]], asof)
        missing = [r["code"] for r in current["positions"] if not _positive(prices.get(r["code"]))]
        issues = ["缺少当日收盘价：" + "、".join(missing)] if missing else []
        view = snapshot(state, asof, {c: p for c, p in prices.items() if _positive(p)})
        missing_funds = [code for code, row in view["fund_positions"].items() if row["nav"] is None]
        if missing_funds:
            issues.append("缺少场外基金净值：" + "、".join(missing_funds))
        record = {**view, "date": asof, "nav": None if issues else view["equity"] / view["units"],
                  "benchmark_nav": None, "issues": issues, "revision": revision(state, asof),
                  "closing_prices": {c: float(prices[c]) for c in prices if _positive(prices[c])},
                  "source": "收盘行情"}
        state["records"] = sorted([r for r in state["records"] if r["date"] != asof] + [record],
                                  key=lambda r: r["date"])
        if not dry_run:
            _save(path, state)
        return state


def _holding_kind(code):
    if is_a_stock_code(code):
        return "stock"
    # 行情路由还把 16、18 码族归入基金接口，对账时将它们单列。
    if is_etf_code(code) and not code.startswith(("16", "18")):
        return "etf"
    return "other"


def valuation_day(today):
    """场内持仓全天按北京时间前一交易日估值。"""
    day = latest_trading_day_on_or_before(today - timedelta(days=1), fallback=None)
    return day.isoformat() if day else None


def _closing_prices(state, day):
    if day is None:
        return {}
    saved = state.get("valuation_closes", {})
    prices = dict(saved["prices"]) if saved.get("date") == day else {}
    for record in state["records"]:
        if record["date"] != day or record["source"] != "收盘行情":
            continue
        marks = record.get("closing_prices")
        if marks is None and not record.get("issues"):
            marks = {row["code"]: row["close"] for row in record["positions"]}
        prices.update(marks or {})
    return {code: price for code, price in prices.items() if _positive(price)}


def refresh_close_prices(path, today, price_day, close_prices):
    """盘前采集与手动刷新共同补齐同日收盘价；取数期间允许记账。"""
    if price_day is None:
        raise ValueError("交易日历不可用，无法确认估值日")
    state = load(path)
    codes = {row["code"] for row in snapshot(state, today.isoformat())["positions"]}
    for asset in state["_config"]["core"]:
        if asset["code"] != "CASH" and not portfolio_config.fund_only(asset):
            codes.update((asset["code"], *asset.get("etf_codes", ())))
    known = _closing_prices(state, price_day)
    needed = codes - known.keys()
    if needed:
        fetched = close_prices(sorted(needed), price_day)
        with _locked(path):
            state = load(path)
            known = _closing_prices(state, price_day)
            # 同一估值日只补缺价，防止覆盖并行任务刚补到的价格。
            additions = {code: fetched[code] for code in needed - known.keys()
                         if _positive(fetched.get(code))}
            saved = state.get("valuation_closes", {})
            if additions and saved.get("date", "") <= price_day:
                state["valuation_closes"] = {"date": price_day, "prices": {**known, **additions}}
                _save(path, state)
                known.update(additions)
    return sorted(codes - known.keys())


def _valuation_view(state, today, price_day):
    prices = _closing_prices(state, price_day)
    view = snapshot(state, today.isoformat(), prices)
    missing = {row["code"] for row in view["positions"] if row["code"] not in prices}
    for row in view["positions"] + view["strategy_positions"]:
        if row["code"] in missing:
            row.update(close=None, current_price=None, market_value=None)
            if row.get("strategy"):
                view["strategy_pnl"][row["strategy"]] = None
    missing_funds = []
    for code, row in view["fund_positions"].items():
        row.pop("book_price")
        if row["nav"] is None:
            row["market_value"] = None
            missing_funds.append(code)
    if missing_funds:
        view["strategy_pnl"]["core"] = None
    if missing or missing_funds:
        view.update(invested=None, equity=None, pnl=None)
    view["prices"] = prices
    view["valuation"] = {"date": price_day, "missing": sorted(missing)}
    if missing_funds:
        view["valuation"]["missing_funds"] = sorted(missing_funds)
    return view


def account_data(path):
    state = load(path)
    day = _today()
    today = day.isoformat()
    price_day = valuation_day(day)
    view = _valuation_view(state, day, price_day)
    records = sorted([r for r in state["records"] if r["date"] <= today], key=lambda r: r["date"])
    valid = [r for r in records if _positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    valid_dates = {r["date"] for r in valid}
    latest = valid[-1] if valid else None
    warnings = list(records[-1]["issues"]) if records else []
    if records and (not latest or latest["date"] != records[-1]["date"]):
        warnings.append("最新成绩待重新估值，曲线保留最近有效数据。")
    strategies = strategy_views(state, view, today=day)
    grouped_values = {"etf": [], "stock": [], "other": []}
    for row in view["positions"]:
        row["kind"] = _holding_kind(row["code"])
        grouped_values[row["kind"]].append(row["market_value"])
    funds = view["fund_positions"]
    if funds:
        grouped_values["fund"] = [row["market_value"] for row in funds.values()]
    holdings_by_kind = {kind: round(math.fsum(values), 2) if None not in values else None
                        for kind, values in grouped_values.items() if kind != "other" or values}
    net_contributed = round(state["initial"]["equity"] + math.fsum(
        event["amount"] if event["side"] == "deposit" else -event["amount"]
        for event in state["events"] if event["date"] <= today and event["side"] in {"deposit", "withdraw"}), 2)
    core = next(strategy for strategy in strategies if strategy["id"] == "core")
    core["allocation_groups"] = _allocation_groups(state["_config"], core, funds, view["prices"])
    core["allocation_budget"] = core["budget"]
    core["allocation_assets"] = core["assets"]
    fund_subscription_fees = {event["code"]: event["subscription_fee"] for event in state["events"]
                              if event["side"] == "fund_buy" and "subscription_fee" in event}
    return {"id": "etf", "name": "投资账户", "can_record": True, **view,
            "holdings_by_kind": holdings_by_kind, "net_contributed": net_contributed,
            "fund_subscription_fees": fund_subscription_fees,
            "performance": latest, "trades": [{**{k: v for k, v in e.items() if k != "request"},
                         **({"name": f"场外基金 {e['code']}" if e["side"] in FUND_TRADES
                             else security_name(e["code"])} if e.get("code") else {})}
                       for e in reversed(state["events"])],
            "start_date": state["initial"]["date"], "initial_equity": state["initial"]["equity"],
            "revision": revision(state), "config_revision": state["_config_revision"], "suggestions": [],
            "strategies": strategies, "valuation_date": price_day,
            "has_events": bool(state["events"]), "benchmark_name": None, "warnings": warnings,
            "history": [{"date": r["date"], "nav": r["nav"] if r["date"] in valid_dates else None,
                         "benchmark_nav": None} for r in records]}


def strategy_snapshot(path=None):
    """读取同一批次的配置与核心账户事实，返回 (config, snapshot)。

    配置校验失败直接抛出；账户不可用时 snapshot 为 None。
    与网页写入共用账户锁，仅在读取期间持锁，派生计算在释放锁后完成。
    """
    from src.trade_ledger.ledger import LedgerSnapshot

    path = Path(path or ACCOUNT_PATH)
    with _locked(path):
        config = portfolio_config.load(portfolio_config.path_for(path))
        try:
            state = _read_account(path, config)
        except Exception:
            logger.warning("投资账户读取失败，跳过持仓对照", exc_info=True)
            return config, None
    try:
        view = snapshot(state, _today().isoformat())
        if view["fund_positions"] or any(portfolio_config.grouped(row) for row in config["core"]):
            return config, None
        core = _strategy_facts(state, view, "core")
        if core["status"] != "已分配":
            raise ValueError("多资产配置：" + core["status"])
        snap = LedgerSnapshot(positions=core["positions"], entry_map={}, cash=core["buying_power"],
                              invested=core["invested"], total_assets=core["budget"])
    except Exception:
        logger.warning("投资账户快照不可用，跳过持仓对照", exc_info=True)
        snap = None
    return config, snap


def render_summary(path=ACCOUNT_PATH):
    view = account_data(path)
    equity = f"{view['equity']:,.2f}" if view['equity'] is not None else "待估值"
    return (f"## 投资账户\n\n总资产 {equity} 元｜现金 {view['cash']:,.2f} 元｜"
            f"持仓 {len(view['positions'])} 只\n\n策略只提供建议，实际成交由人工确认记账。")
