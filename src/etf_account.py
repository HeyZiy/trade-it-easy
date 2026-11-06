"""投资账户台账：期初快照、人工流水、收盘估值和调仓建议。"""

from datetime import date
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

FILENAME = "etf_account.json"
ACCOUNT_PATH = Path("data") / FILENAME
logger = logging.getLogger(__name__)


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


def snapshot(state, asof, prices=None):
    """重放已确认流水；资金流按当时可用估值增减净值份额，不计为盈利。"""
    initial = state["initial"]
    cash, units = initial["cash"], initial["equity"]
    cash_by_strategy = _cash_allocation(initial.get("cash_allocations", {}), cash)
    held = {(r.get("strategy", "unassigned"), r["code"]): {**r, "name": security_name(r["code"]), "strategy": r.get("strategy", "unassigned"), "avail_count": r["count"]} for r in initial["positions"]}
    marks = {r["code"]: r["price"] for r in initial["positions"]}
    net_capital = dict.fromkeys((*portfolio_config.STRATEGY_NAMES, "unassigned"), 0.0)
    for row in initial["positions"]:
        net_capital[row.get("strategy", "unassigned")] += row["count"] * row["price"]
    net_flows = 0.0
    # 行情价格与账户成绩分开：流水变更会使旧净值失效，不会使当天收盘价失效。
    records = iter(sorted([r for r in state["records"] if r["date"] <= asof], key=lambda r: r["date"]))
    record = next(records, None)
    for event in (e for e in state["events"] if e["date"] <= asof):
        while record and record["date"] < event["date"]:
            marks.update({r["code"]: r["close"] for r in record["positions"] if _positive(r.get("close"))})
            record = next(records, None)
        side = event["side"]
        if side in {"deposit", "withdraw"}:
            marks.update(event.get("valuation_prices", {}))
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
            cash_by_strategy = _cash_allocation(event["cash_allocations"], cash)
        elif side == "cash_transfer":
            cash_by_strategy[event["from_strategy"]] -= event["amount"]
            cash_by_strategy[event["strategy"]] += event["amount"]
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
            row = held.setdefault((strategy, code), {"code": code, "name": security_name(event["code"]), "strategy": strategy, "count": 0, "avail_count": 0})
            row["name"] = security_name(event["code"])
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

    grouped_codes = {row["code"] for row in state["_config"]["core"] if portfolio_config.grouped(row)}
    engine = ETFRebalancer(asset for asset in baseline(state) if asset.code not in grouped_codes)
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
    return portfolio_config.allocations(state["_config"])


def _fund_revision(state):
    return portfolio_config.revision(state.get("fund_positions", {}))


def _fund_market_value(qty, nav):
    amount = Decimal(str(qty)) * Decimal(str(nav))
    return float(amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def save_fund_position(path, payload):
    """保存场外持仓快照。现金、场内流水和历史收益继续按券商账户计算。"""
    with _locked(path):
        state = load(path)
        if payload.get("revision") != _fund_revision(state):
            raise ValueError("场外持仓已更新，请刷新后再编辑")
        code = payload.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("请输入六位场外基金代码")
        qty = payload.get("qty")
        if type(qty) not in (int, float) or not 0 <= qty <= 1e12 or round(qty, 6) != qty:
            raise ValueError("基金份额须为非负数，最多六位小数")
        funds = state.setdefault("fund_positions", {})
        if qty == 0:
            funds.pop(code, None)
        else:
            nav = _money(payload.get("nav"))
            if nav > 1e8 or round(nav, 8) != nav:
                raise ValueError("单位净值最多八位小数")
            nav_date = payload.get("date")
            if not isinstance(nav_date, str):
                raise ValueError("请选择净值日期")
            _day(nav_date)
            funds[code] = {"code": code, "qty": qty, "nav": nav, "date": nav_date,
                           "market_value": _fund_market_value(qty, nav)}
        _save(path, state)


def refresh_fund_navs(path, through):
    """锁外查询已录入的基金，锁内按最新份额合并更晚日期的净值。"""
    from data_provider.fund_nav import latest_fund_nav

    marks = {}
    result = {"updated": [], "unchanged": [], "failed": []}
    funds = load(path).get("fund_positions", {})
    for code, fund in funds.items():
        if fund["date"] >= through:
            result["unchanged"].append(code)
            continue
        try:
            mark = latest_fund_nav(code, through)
        except Exception:
            logger.exception("场外基金 %s 净值获取失败", code)
            mark = None
        if mark is None:
            result["failed"].append(code)
        elif mark[0] > fund["date"]:
            marks[code] = mark
        else:
            result["unchanged"].append(code)
    if not marks:
        return result

    with _locked(path):
        state = load(path)
        for code, (nav_date, nav) in marks.items():
            fund = state.get("fund_positions", {}).get(code)
            if fund is None or fund["date"] >= nav_date:
                result["unchanged"].append(code)
                continue
            fund.update(date=nav_date, nav=nav, market_value=_fund_market_value(fund["qty"], nav))
            result["updated"].append(code)
        if result["updated"]:
            _save(path, state)
    return result


def _allocation_groups(config, strategy, funds):
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
    invested = (round(sum(r["market_value"] for r in positions), 2)
                if all(r["market_value"] is not None for r in positions) else None)
    weight = state.get("budgets", {}).get(key)
    budget = round(view["equity"] * weight, 2) if weight is not None and view["equity"] is not None else None
    remaining = max(0, round(budget - invested, 2)) if budget is not None and invested is not None else None
    cash = view["cash_by_strategy"][key]
    assets = round(invested + cash, 2) if invested is not None else None
    status = ("目标比例待设置" if remaining is None
              else "已分配" if positions or cash > 0 else "暂无持仓")
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


def strategy_views(state, view, *, today=None):
    """汇总策略事实与建议；策略标识决定归属，展示顺序不参与规则判定。"""
    result = []
    for key in portfolio_config.STRATEGY_NAMES:
        row = _strategy_facts(state, view, key)
        if key == "rotation":
            from src.etf.rotation_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("rotation_signal"), row, today or _today(),
                account_revision=revision(state), dismissed=state["dismissed"])
            row["signal_date"] = state.get("rotation_signal", {}).get("date")
        if key == "quality":
            from src.quality_pool.account_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("quality_signal"), row, today or _today(),
                dismissed=state["dismissed"])
            row["signal_date"] = state.get("quality_signal", {}).get("date")
        if key == "core" and row["status"] == "已分配" and row["budget"] > 0:
            external = sum(fund["market_value"] for fund in state.get("fund_positions", {}).values())
            row["suggestions"] = suggestions(state, {**view, "positions": row["positions"],
                                                    "equity": row["budget"] + external, "cash": row["cash"]})
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


def _make_event(state, payload, request_id):
    """校验一笔流水，不落盘；更正时可重放后续全部流水。"""
    day = _day(payload.get("date", ""))
    side = payload.get("side")
    if side not in {"buy", "sell", "dividend", "deposit", "withdraw", "assign", "cash_assign", "cash_transfer"}:
        raise ValueError("不支持的记账类型")
    event = {"id": request_id, "date": day, "side": side, "request": payload}
    view = snapshot(state, day)
    if side == "cash_assign":
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
        event = _make_event(state, payload, request_id)
        view = snapshot(state, day)
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


def cash_rebalance(path, payload, *, live_quotes=None):
    """按同一组展示估值预览现金再平衡，不保存；持仓超配时按现金缺口比例分配。"""
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        view = account_data(path, live_quotes=live_quotes)
        if view["equity"] is None:
            raise ValueError("部分持仓待估值，请先刷新完整行情再进行现金再平衡")
        strategies = {r["id"]: r for r in view["strategies"]}
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


def event_context(path, payload):
    """返回目标流水发生前的余额与持仓，供更正表单校验。"""
    state = load(path)
    if payload.get("revision") != revision(state):
        raise ValueError("账户已更新，请刷新后重新核对")
    index = next((i for i, e in enumerate(state["events"]) if e["id"] == payload.get("event_id")), None)
    if index is None:
        raise ValueError("流水不存在")
    event = state["events"][index]
    before = {**state, "events": state["events"][:index]}
    return {**snapshot(before, event["date"]), "event": {k: v for k, v in event.items() if k != "request"},
            "revision": revision(state), "start_date": state["initial"]["date"],
            "min_date": state["events"][index - 1]["date"] if index else state["initial"]["date"],
            "max_date": state["events"][index + 1]["date"] if index + 1 < len(state["events"]) else _today().isoformat()}


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
        context = event_context(path, payload)
        index = next(i for i, e in enumerate(state["events"]) if e["id"] == payload["event_id"])
        old = state["events"][index]
        day = _day(payload.get("date", ""))
        if not context["min_date"] <= day <= context["max_date"]:
            raise ValueError("更正日期须在相邻流水日期之间")
        replay = {**state, "events": []}
        for i, stored in enumerate(state["events"]):
            candidate = payload if i == index else stored
            try:
                event = _make_event(replay, candidate, stored["id"])
            except (ValueError, KeyError, ZeroDivisionError) as error:
                raise ValueError(f"更正会使第{i + 1}笔流水（{candidate['date']}）无效：{error}") from error
            if i != index:
                # 后续流水只重新校验，保留实际记账时确定的划转估值与资金流价格。
                event = stored
            replay["events"].append(event)
        state["events"] = replay["events"]
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


def account_data(path, *, live_quotes=None):
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
    quote_revision = hashlib.sha256(json.dumps([revision(state), state["records"]],
                                               sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # 调仓建议仍按保存的估值生成；盘中报价只覆盖展示估值，不进入历史或成交校验。
    strategies = strategy_views(state, view)
    # 默认价格只取同一日的完整收盘记录；成交价只用于记账，不补展示估值。
    priced_record = next((r for r in reversed(records) if not r.get("issues")), None)
    price_day = priced_record["date"] if priced_record else state["initial"]["date"]
    prices = (dict(priced_record["closing_prices"]) if priced_record and "closing_prices" in priced_record
              else {r["code"]: r["close"] for r in (priced_record or {}).get("positions", [])
                    if _positive(r.get("close"))})
    if not priced_record:
        prices = {r["code"]: r["price"] for r in state["initial"]["positions"]}
    valuation = {"mode": "initial" if price_day == state["initial"]["date"] else "close",
                 "date": price_day, "fetched_at": None}
    quote_refresh = None
    quotes = {}
    if live_quotes:
        applicable = (live_quotes.get("revision") == quote_revision
                      and live_quotes.get("day") == today)
        quote_refresh = {key: live_quotes.get(key) for key in
                         ("running", "fetched_at", "missing", "error")}
        if live_quotes.get("day") == today:
            quote_refresh["funds"] = live_quotes.get("funds")
        quote_refresh["applicable"] = applicable
        quotes = live_quotes.get("quotes", {}) if applicable else {}
        live_prices = {code: item["price"] for code, item in quotes.items() if _positive(item.get("price"))}
        held_codes = {row["code"] for row in view["positions"]}
        complete = bool(held_codes) and held_codes <= live_prices.keys()
        quote_refresh["estimated"] = complete
        if applicable and not complete and not quote_refresh.get("missing"):
            quote_refresh["missing"] = sorted(held_codes - live_prices.keys())
        if complete:
            prices = live_prices
            valuation.update(mode="live", date=today, fetched_at=live_quotes.get("fetched_at"))
    view = snapshot(state, today, prices)
    missing = sorted({r["code"] for r in view["positions"] if not _positive(prices.get(r["code"]))})
    valuation["missing"] = missing
    view["prices"] = prices
    for row in view["positions"] + view["strategy_positions"]:
        row["quote_fetched_at"] = quotes.get(row["code"], {}).get("fetched_at") if valuation["mode"] == "live" else None
        row["quote_missing"] = bool(quote_refresh and quote_refresh.get("missing"))
        if row["code"] in missing:
            row.update(close=None, current_price=None, market_value=None)
            if row.get("strategy"):
                view["strategy_pnl"][row["strategy"]] = None
    if missing:
        view.update(invested=None, equity=None, pnl=None)
    grouped_values = {"etf": [], "stock": [], "other": []}
    for row in view["positions"]:
        row["kind"] = _holding_kind(row["code"])
        grouped_values[row["kind"]].append(row["market_value"])
    holdings_by_kind = {kind: round(math.fsum(values), 2) if None not in values else None
                        for kind, values in grouped_values.items() if kind != "other" or values}
    net_contributed = round(state["initial"]["equity"] + math.fsum(
        event["amount"] if event["side"] == "deposit" else -event["amount"]
        for event in state["events"] if event["date"] <= today and event["side"] in {"deposit", "withdraw"}), 2)
    for strategy in strategies:
        facts = _strategy_facts(state, view, strategy["id"])
        for key in ("positions", "budget", "invested", "pnl", "assets", "actual_pct",
                    "deviation", "remaining_budget", "buying_power", "cash"):
            strategy[key] = facts[key]
        if strategy["id"] == "core":
            funds = state.get("fund_positions", {})
            external = round(sum(fund["market_value"] for fund in funds.values()), 2)
            strategy["allocation_groups"] = _allocation_groups(state["_config"], strategy, funds)
            strategy["external_fund_value"] = external
            for key in ("budget", "assets"):
                strategy[f"allocation_{key}"] = round(strategy[key] + external, 2) if strategy[key] is not None else None
    return {"id": "etf", "name": "投资账户", "can_record": True, **view,
            "holdings_by_kind": holdings_by_kind, "net_contributed": net_contributed,
            "fund_revision": _fund_revision(state), "fund_codes": sorted(state.get("fund_positions", {})),
            "performance": latest, "trades": [{**{k: v for k, v in e.items() if k != "request"},
                         **({"name": security_name(e["code"])} if e.get("code") else {})}
                       for e in reversed(state["events"])],
            "start_date": state["initial"]["date"], "initial_equity": state["initial"]["equity"],
            "revision": revision(state), "config_revision": state["_config_revision"], "suggestions": [],
            "strategies": strategies, "quote_refresh": quote_refresh, "quote_revision": quote_revision,
            "valuation_date": price_day, "valuation": valuation,
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
        if state.get("fund_positions") or any(portfolio_config.grouped(row) for row in config["core"]):
            return config, None
        view = snapshot(state, _today().isoformat())
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
