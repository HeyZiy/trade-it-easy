"""ETF账户台账：期初快照、人工流水、收盘估值和调仓建议。"""

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from data_provider import canonical_stock_code

FILENAME = "etf_account.json"
ACCOUNT_PATH = Path("data") / FILENAME


def _today():
    return date.today()


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


@contextmanager
def _locked(path):
    """网页和收盘任务共用进程间锁，整次读改写串行完成。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".json.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _save(path, state):
    handle, temporary = tempfile.mkstemp(dir=Path(path).parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows 索引/杀毒可能短暂占用刚关闭的文件，保留原子替换语义。
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path=ACCOUNT_PATH):
    if not Path(path).exists():
        raise FileNotFoundError("ETF账户尚未初始化")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def revision(state, asof=None):
    facts = {"initial": state["initial"], "events": [e for e in state["events"]
             if asof is None or (e["date"] <= asof and e["side"] not in {"assign", "cash_assign"})]}
    if asof is None:
        facts["budgets"] = state.get("budgets", {})
        facts["targets"] = state.get("targets", {})
        if "rotation_signal" in state:
            facts["rotation_signal"] = state["rotation_signal"]
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initialize(path, *, asof, cash, positions, targets=None, budgets=None, cash_allocations=None):
    """期初价格用于建立收益起点，不伪造成交成本或历史交易。"""
    _day(asof)
    if type(cash) not in (int, float) or not math.isfinite(cash) or cash < 0:
        raise ValueError("现金须为有限非负数")
    budgets = dict(budgets or {})
    if set(budgets) - {"core", "rotation"} or any(type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1 for w in budgets.values()) or sum(budgets.values()) > 1:
        raise ValueError("策略预算比例无效")
    rows, codes = [], set()
    for item in positions:
        code = canonical_stock_code(item["code"])
        count, price = item["count"], _money(item["price"])
        strategy = item.get("strategy", "unassigned")
        if strategy not in {"core", "rotation", "unassigned"}:
            raise ValueError("策略标记无效")
        if (strategy, code) in codes or not _positive(count) or int(count) != count:
            raise ValueError("代码不得重复，份额须为正整数")
        codes.add((strategy, code))
        rows.append({"code": code, "name": item["name"], "count": int(count), "price": price, "strategy": strategy})
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
            raise ValueError("ETF账户台账已存在，禁止覆盖初始化")
        _save(path, state)
    return state


def _cash_allocation(values, total):
    if not isinstance(values, dict) or set(values) - {"core", "rotation", "unassigned"}:
        raise ValueError("现金分配无效")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 or round(v, 2) != v for v in values.values()):
        raise ValueError("现金分配须为非负金额，最多两位小数")
    core, rotation = values.get("core", 0), values.get("rotation", 0)
    remaining = round(total - core - rotation, 2)
    if remaining < 0:
        raise ValueError("分配金额超过账户现金")
    return {"core": core, "rotation": rotation, "unassigned": remaining}


def snapshot(state, asof, prices=None):
    """重放已确认流水；资金流按当时可用估值增减净值份额，不计为盈利。"""
    initial = state["initial"]
    cash, units = initial["cash"], initial["equity"]
    cash_by_strategy = _cash_allocation(initial.get("cash_allocations", {}), cash)
    held = {(r.get("strategy", "unassigned"), r["code"]): {**r, "strategy": r.get("strategy", "unassigned"), "avail_count": r["count"]} for r in initial["positions"]}
    marks = {r["code"]: r["price"] for r in initial["positions"]}
    net_capital = {"core": 0.0, "rotation": 0.0, "unassigned": 0.0}
    for row in initial["positions"]:
        net_capital[row.get("strategy", "unassigned")] += row["count"] * row["price"]
    net_flows = 0.0
    records = iter(sorted([r for r in state["records"] if r["date"] <= asof
                           and r.get("revision") == revision(state, r["date"])
                           and _positive(r.get("nav"))], key=lambda r: r["date"]))
    record = next(records, None)
    for event in (e for e in state["events"] if e["date"] <= asof):
        while record and record["date"] < event["date"]:
            marks.update({r["code"]: r["close"] for r in record["positions"] if _positive(r.get("close"))})
            record = next(records, None)
        side = event["side"]
        if side in {"deposit", "withdraw"}:
            equity = cash + sum(r["count"] * marks[r["code"]] for r in held.values())
            delta = event["amount"] * (1 if side == "deposit" else -1)
            units += delta / (equity / units)
            cash += delta
            cash_by_strategy[event.get("strategy", "unassigned")] += delta
            net_flows += delta
        elif side == "dividend":
            cash += event["amount"]
            cash_by_strategy[event.get("strategy", "unassigned")] += event["amount"]
            net_capital[event.get("strategy", "unassigned")] -= event["amount"]
        elif side == "cash_assign":
            cash_by_strategy = dict(event["cash_allocations"])
        elif side == "assign":
            code, qty = event["code"], event["qty"]
            source = held[(event["from_strategy"], code)]
            target = held.setdefault((event["strategy"], code),
                                     {"code": code, "name": source["name"], "count": 0,
                                      "avail_count": 0, "strategy": event["strategy"]})
            available = min(qty, source["avail_count"])
            source["count"] -= qty
            source["avail_count"] -= available
            target["count"] += qty
            target["avail_count"] += available
            transfer_value = qty * event.get("transfer_price", marks[code])
            net_capital[event["from_strategy"]] -= transfer_value
            net_capital[event["strategy"]] += transfer_value
        else:
            code, qty, price = event["code"], event["qty"], event["price"]
            strategy = event.get("strategy", "unassigned")
            row = held.setdefault((strategy, code), {"code": code, "name": event["name"], "strategy": strategy, "count": 0, "avail_count": 0})
            row["name"] = event["name"]
            marks[code] = price
            if side == "buy":
                row["count"] += qty
                if event["date"] < asof:
                    row["avail_count"] += qty
                amount = round(qty * price + event["fee"], 2)
                cash -= amount
                cash_by_strategy[strategy] -= amount
                net_capital[strategy] += amount
            else:
                row["count"] -= qty
                row["avail_count"] = max(0, row["avail_count"] - qty)
                amount = round(qty * price - event["fee"], 2)
                cash += amount
                cash_by_strategy[strategy] += amount
                net_capital[strategy] -= amount
    while record:
        marks.update({r["code"]: r["close"] for r in record["positions"] if _positive(r.get("close"))})
        record = next(records, None)
    marks.update(prices or {})
    strategy_rows = [{"code": row["code"], "name": row["name"], "count": row["count"],
                      "strategy": strategy, "avail_count": row["avail_count"], "cost_price": None,
                      "close": marks[row["code"]], "current_price": marks[row["code"]],
                      "market_value": round(row["count"] * marks[row["code"]], 2)}
                     for (strategy, code), row in held.items() if row["count"] > 0]
    combined = {}
    for row in strategy_rows:
        total = combined.setdefault(row["code"], {**row, "count": 0, "avail_count": 0, "market_value": 0})
        total.pop("strategy", None)
        total["count"] += row["count"]
        total["avail_count"] += row["avail_count"]
        total["market_value"] = round(total["count"] * total["close"], 2)
    rows = list(combined.values())
    invested = round(sum(r["market_value"] for r in rows), 2)
    strategy_pnl = {key: round(sum(r["count"] * marks[r["code"]]
                                  for r in held.values() if r["strategy"] == key) - capital, 2) or 0.0
                    for key, capital in net_capital.items()}
    return {"positions": rows, "strategy_positions": strategy_rows, "cash": round(cash, 2),
            "invested": invested, "equity": round(cash + invested, 2), "units": units,
            "receivables": 0, "prices": marks, "strategy_pnl": strategy_pnl,
            "cash_by_strategy": {k: round(v, 2) or 0.0 for k, v in cash_by_strategy.items()},
            "pnl": round(round(cash + invested, 2) - initial["equity"] - net_flows, 2) or 0.0}


def suggestions(state, view):
    from src.etf.rebalancer import ETFRebalancer

    engine = ETFRebalancer()
    engine.baseline = baseline(state)
    plan = engine.build_plan(state["targets"], view["positions"], view["equity"], prices=view["prices"])
    records = sorted(state["records"], key=lambda r: r["date"])
    valid = [r for r in records if _positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    price_date = valid[-1]["date"] if valid else state["initial"]["date"]
    outdated = (_today() - date.fromisoformat(price_date)).days > 7
    incomplete = bool(records and (records[-1].get("issues") or
                      (records[-1]["source"] != "用户确认的期初估值" and
                       records[-1]["revision"] != revision(state, records[-1]["date"]))))
    price_issue = None
    if outdated:
        price_issue = "估值超过7天，待更新"
    elif incomplete:
        price_issue = "估值不完整，待更新"
    rev, result = revision(state), []
    for order in plan.orders:
        identity = hashlib.sha256(f"{rev}:{len(state['records'])}:{order.code}:{order.action}:{order.qty}".encode()).hexdigest()[:24]
        if identity in state["dismissed"]:
            continue
        price = view["prices"].get(order.code)
        if not _positive(price):
            continue  # 新标的缺少估值价格时不凑单。
        result.append({"id": identity, "code": order.code, "name": order.name,
                       "side": order.action, "qty": None if price_issue else order.qty, "price": price,
                       "amount": None if price_issue else round(order.qty * price, 2),
                       "gap_amount": round(order.amount, 2),
                       "current_pct": order.current_pct, "target_pct": order.target_pct,
                       "current_value": round(order.current_pct * view["equity"], 2),
                       "target_value": round(order.target_pct * view["equity"], 2),
                       "price_date": price_date, "price_issue": price_issue, "reason": order.reason})
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
    from src.etf.config import AssetAllocation, AssetType

    names = {r["code"]: r["name"] for r in state["initial"]["positions"]}
    result = []
    for code, weight in state["targets"].items():
        asset_type = AssetType.EQUITY
        name = names.get(code, code)
        if code == "CASH":
            asset_type, name = AssetType.CASH, "现金"
        elif code == "159934":
            asset_type = AssetType.GOLD
        result.append(AssetAllocation(code, name, asset_type, weight, 0))
    return result


def strategy_views(state, view, *, today=None):
    """汇总策略名下的持仓与现金；目标比例只决定预算，不自动分配资金。"""
    result = []
    for key, name in (("core", "核心配置"), ("rotation", "跨资产轮动")):
        positions = [r for r in view["strategy_positions"] if r["strategy"] == key]
        invested = round(sum(r["market_value"] for r in positions), 2)
        weight = state.get("budgets", {}).get(key)
        budget = round(view["equity"] * weight, 2) if weight is not None else None
        remaining = max(0, round(budget - invested, 2)) if budget is not None else None
        result.append({"id": key, "name": name, "account_id": "etf", "positions": positions,
                       "budget_pct": weight, "budget": budget, "invested": invested,
                       "pnl": view["strategy_pnl"][key],
                       "target_weights": dict(state["targets"]) if key == "core" else None,
                       "remaining_budget": remaining, "buying_power": None,
                       "suggestions": [], "trades": [e for e in state["events"] if e.get("strategy") == key],
                       "history": [], "performance": None, "benchmark_name": None})
    for row in result:
        owned_cash = view["cash_by_strategy"][row["id"]]
        actual_assets = round(row["invested"] + owned_cash, 2)
        row.update(cash=owned_cash, assets=actual_assets,
                   actual_pct=actual_assets / view["equity"] if view["equity"] else None,
                   deviation=round(actual_assets - row["budget"], 2) if row["budget"] is not None else None)
        if row["id"] == "rotation":
            from src.etf.rotation_advice import advice
            row["suggestions"], row["advice_status"] = advice(state, row, today or _today())
            row["signal_date"] = state.get("rotation_signal", {}).get("date")
        if row["remaining_budget"] is None:
            row["status"] = "目标比例待设置"
            continue
        row["buying_power"] = min(owned_cash, row["remaining_budget"])
        row["status"] = "已分配" if row["positions"] or owned_cash > 0 else "暂无持仓"
        if row["id"] == "core":
            row["target_weights"] = dict(state["targets"])
            if row["status"] == "已分配" and row["budget"] > 0:
                row["suggestions"] = suggestions(state, {**view, "positions": row["positions"],
                                                        "equity": row["budget"], "cash": owned_cash})
    return result


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
        side = payload.get("side")
        if side not in {"buy", "sell", "dividend", "deposit", "withdraw", "assign", "cash_assign"}:
            raise ValueError("不支持的记账类型")
        event = {"id": request_id, "date": day, "side": side, "request": payload}
        view = snapshot(state, day)
        if side == "cash_assign":
            event["cash_allocations"] = _cash_allocation(payload.get("cash_allocations"), view["cash"])
        elif side == "assign":
            source, target = payload.get("from_strategy"), payload.get("strategy")
            if source not in {"core", "rotation", "unassigned"} or target not in {"core", "rotation", "unassigned"} or source == target:
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
            if strategy not in {"core", "rotation", "unassigned"}:
                raise ValueError("策略标记无效")
            if side == "buy" and round(qty * price + fee, 2) > view["cash_by_strategy"][strategy]:
                raise ValueError("该策略现金不足，请先分配现金")
            if side == "sell" and round(view["cash_by_strategy"][strategy] + qty * price - fee, 2) < 0:
                raise ValueError("该策略现金不足以扣除费用")
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > 80:
                raise ValueError("请填写有效的标的名称")
            event.update(code=code, name=name, qty=int(qty), price=price, fee=float(fee), strategy=strategy)
        else:
            strategy = payload.get("strategy", "unassigned")
            if strategy not in {"core", "rotation", "unassigned"}:
                raise ValueError("策略标记无效")
            amount = round(_money(payload.get("amount")), 2)
            if amount <= 0 or (side == "withdraw" and amount > view["cash_by_strategy"][strategy]):
                raise ValueError("金额无效或超过实际现金")
            if side == "withdraw" and amount >= view["equity"]:
                raise ValueError("暂不支持全额清户，请保留正的账户资产")
            event["amount"] = amount
            event["strategy"] = strategy
        signal = state.get("rotation_signal")
        if side == "buy" and event.get("strategy") == "rotation" and signal and not signal.get("buy_intent"):
            # 第一笔实际成交时锁定本轮份额；尚未成交时，现金或预算变化仍可完整重算。
            planned = strategy_views(state, view)[1]["suggestions"]
            buy = next((item for item in planned if item["side"] == "buy" and item["code"] == event["code"]), None)
            if buy:
                held = sum(p["count"] for p in view["strategy_positions"]
                           if p["strategy"] == "rotation" and p["code"] == event["code"])
                signal["buy_intent"] = {"code": buy["code"], "qty": held + buy["qty"]}
        state["events"].append(event)
        _save(path, state)
        return event


def set_budgets(path, payload):
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重试")
        budgets = payload.get("budgets")
        if not isinstance(budgets, dict) or set(budgets) - {"core", "rotation"}:
            raise ValueError("预算配置无效")
        if any(type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1 for w in budgets.values()) or sum(budgets.values()) > 1:
            raise ValueError("预算合计不能超过100%")
        state["budgets"] = budgets
        _save(path, state)


def dismiss(path, payload):
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        views = strategy_views(state, snapshot(state, _today().isoformat()))
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
        record = {**view, "date": asof, "nav": None if issues else view["equity"] / view["units"],
                  "benchmark_nav": None, "issues": issues, "revision": revision(state, asof),
                  "source": "收盘行情"}
        state["records"] = sorted([r for r in state["records"] if r["date"] != asof] + [record],
                                  key=lambda r: r["date"])
        if not dry_run:
            _save(path, state)
        return state


def account_data(path):
    state = load(path)
    today = _today().isoformat()
    view = snapshot(state, today)
    records = sorted([r for r in state["records"] if r["date"] <= today], key=lambda r: r["date"])
    valid = [r for r in records if _positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    valid_dates = {r["date"] for r in valid}
    latest = valid[-1] if valid else None
    warnings = list(records[-1]["issues"]) if records else []
    if records and (not latest or latest["date"] != records[-1]["date"]):
        warnings.append("最新成绩待重新估值，曲线保留最近有效数据。")
    return {"id": "etf", "name": "ETF账户", "can_record": True, **view,
            "performance": latest, "trades": [{k: v for k, v in e.items() if k != "request"}
                                              for e in reversed(state["events"])][:20],
            "start_date": state["initial"]["date"], "initial_equity": state["initial"]["equity"],
            "revision": revision(state), "suggestions": [],
            "strategies": strategy_views(state, view),
            "valuation_date": latest["date"] if latest else state["initial"]["date"],
            "has_events": bool(state["events"]), "benchmark_name": None, "warnings": warnings,
            "history": [{"date": r["date"], "nav": r["nav"] if r["date"] in valid_dates else None,
                         "benchmark_nav": None} for r in records]}


def strategy_snapshot(path=ACCOUNT_PATH):
    from src.trade_ledger.ledger import LedgerSnapshot

    state = load(path)
    view = snapshot(state, date.today().isoformat())
    core = strategy_views(state, view)[0]
    if core["status"] != "已分配":
        raise ValueError("核心配置：" + core["status"])
    return LedgerSnapshot(positions=core["positions"], entry_map={}, cash=core["buying_power"],
                          invested=core["invested"], total_assets=core["budget"])


def render_summary(path=ACCOUNT_PATH):
    view = account_data(path)
    return (f"## ETF账户\n\n总资产 {view['equity']:,.2f} 元｜现金 {view['cash']:,.2f} 元｜"
            f"持仓 {len(view['positions'])} 只\n\n策略只提供建议，实际成交由人工确认记账。")
