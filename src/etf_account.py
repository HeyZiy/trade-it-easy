"""投资账户台账：期初快照、人工流水、收盘估值和调仓建议。"""

from decimal import Decimal, ROUND_HALF_UP
import logging
import math
from pathlib import Path
from uuid import uuid4

from data_provider import canonical_stock_code, fund_names
from data_provider.codes import A_STOCK_PREFIXES, ETF_PREFIXES
from data_provider.security_names import security_name
from src import account_advice, account_valuation, portfolio_config
from src.account_advice import baseline, strategy_views, suggestions
from src.account_replay import (
    FUND_TRADES, cash_allocation, fund_code, holding_kind, make_event, money,
    net_subscription, positive, replay_edit, snapshot, validate_day,
)
from src.account_store import ACCOUNT_PATH, FILENAME, load, read_account, revision
from src.account_store import save as _save
from src.account_valuation import valuation_day
from src.json_store import locked as _locked
from src.trading_calendar import SHANGHAI, shanghai_today as _today

ETF_HOLDING_PREFIXES = tuple(prefix for prefix in ETF_PREFIXES if prefix not in {"16", "18"})
logger = logging.getLogger(__name__)


def initialize(path, *, asof, cash, positions, targets=None, budgets=None, cash_allocations=None):
    """期初价格用于建立收益起点，不伪造成交成本或历史交易。"""
    validate_day(asof)
    if type(cash) not in (int, float) or not math.isfinite(cash) or cash < 0:
        raise ValueError("现金须为有限非负数")
    budgets = dict(budgets or {})
    if set(budgets) - portfolio_config.STRATEGY_NAMES.keys() or any(type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1 for w in budgets.values()) or sum(budgets.values()) > 1:
        raise ValueError("策略预算比例无效")
    rows, codes = [], set()
    for item in positions:
        code = canonical_stock_code(item["code"])
        count, price = item["count"], money(item["price"])
        strategy = item.get("strategy", "unassigned")
        if strategy not in {*portfolio_config.STRATEGY_NAMES, "unassigned"}:
            raise ValueError("策略标记无效")
        if (strategy, code) in codes or not positive(count) or int(count) != count:
            raise ValueError("代码不得重复，份额须为正整数")
        codes.add((strategy, code))
        rows.append({"code": code, "count": int(count), "price": price, "strategy": strategy})
    if not rows:
        raise ValueError("初始化持仓不能为空")
    targets = {canonical_stock_code(c): float(w) for c, w in (targets or {}).items()}
    if any(not math.isfinite(w) or not 0 <= w <= 1 for w in targets.values()) or sum(targets.values()) > 1:
        raise ValueError("目标权重无效")
    targets.setdefault("CASH", max(0, 1 - sum(targets.values())))
    allocations = cash_allocation(cash_allocations or {}, cash)
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


def lookup_fund_names(_path, payload):
    codes = payload.get("codes")
    if not isinstance(codes, list) or not 1 <= len(codes) <= 100:
        raise ValueError("请提供一至一百个基金代码")
    return {"names": fund_names.lookup({fund_code(code) for code in codes})}


def lookup_fund_nav(_path, payload):
    """按申购适用日期查询单位净值。"""
    from data_provider.fund_nav import latest_fund_nav

    code, day = fund_code(payload.get("code")), validate_day(payload.get("date", ""))
    mark = latest_fund_nav(code, day)
    if mark is None or mark[0] != day:
        raise ValueError("该买入日的单位净值暂不可用，请确认日期或稍后重试")
    return {"date": day, "nav": mark[1]}


def confirm_fund_subscriptions(path, day):
    """只用买入日的公布净值结算，查询期间释放台账锁。"""
    from data_provider.fund_nav import latest_fund_nav

    state = load(path)
    pending = snapshot(state, day)["fund_pending"]
    marks = {}
    for code, buy_day in sorted({(row["code"], row["nav_date"]) for row in pending.values()}):
        try:
            mark = latest_fund_nav(code, buy_day)
        except Exception:
            logger.exception("场外基金 %s %s 申购净值获取失败", code, buy_day)
            continue
        if mark and mark[0] == buy_day:
            marks[code, buy_day] = mark[1]
    if not marks:
        return []

    with _locked(path):
        state = load(path)
        if state["events"][-1]["date"] > day:
            return []
        view = snapshot(state, day)
        confirmed = []
        for pending_id, row in view["fund_pending"].items():
            nav = marks.get((row["code"], row["nav_date"]))
            if nav is None:
                continue
            net = net_subscription(row["amount"], row["subscription_fee"])
            qty = (net / Decimal(str(nav))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            if not 0 < qty <= 1e9:
                logger.warning("待确认申购 %s 的计算份额超出记账范围", pending_id)
                continue
            payload = {**row, "side": "fund_confirm", "date": day, "pending_id": pending_id,
                       "qty": float(qty), "nav": nav}
            state["events"].append(make_event(payload, str(uuid4()), day, view))
            confirmed.append(pending_id)
        if confirmed:
            _save(path, state)
    return confirmed


def refresh_fund_navs(path, through):
    """查询账户内持仓的公布净值，再按日期合并到估值记录。"""
    from data_provider.fund_nav import latest_fund_nav

    result = {"updated": [], "unchanged": [], "failed": []}
    confirmed = confirm_fund_subscriptions(path, through)
    if confirmed:
        result["confirmed"] = confirmed
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


def publish_rotation(day, rows, prices, diag, *, path=None, dry_run=False, error=None):
    """发布投资账户的跨资产动量建议。"""
    return account_advice.publish_rotation(
        day, rows, prices, diag, path=path or ACCOUNT_PATH, dry_run=dry_run, error=error,
    )


def publish_quality(day, *, path=None, dry_run=False, retry=False):
    """发布投资账户的质量动量建议。"""
    return account_advice.publish_quality(day, path=path or ACCOUNT_PATH, dry_run=dry_run, retry=retry)


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
        if payload.get("side") == "fund_confirm":
            raise ValueError("待确认申购由系统按公布净值结算")
        day = validate_day(payload.get("date", ""))
        last_day = state["events"][-1]["date"] if state["events"] else state["initial"]["date"]
        if day < last_day:
            raise ValueError("记账日期不能早于期初或上笔流水")
        view = snapshot(state, day)
        event = make_event(payload, request_id, day, view, config=state["_config"])
        account_advice.on_event(state, view, event, _today())
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
        view = account_valuation.valuation_view(state, today, price_day)
        if view["equity"] is None:
            raise ValueError("部分持仓待估值，请先补齐估值数据再进行现金再平衡")
        strategies = {key: account_advice.strategy_facts(state, view, key) for key in portfolio_config.STRATEGY_NAMES}
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
    if events[index]["side"] == "fund_pending":
        if any(e.get("pending_id") == events[index]["id"] for e in events[index + 1:]):
            raise ValueError("申购已确认，请在确认流水中更正份额")
        if any(e.get("voided_pending_id") == events[index]["id"] for e in events[index + 1:]):
            raise ValueError("申购已作废，流水不可更正")
    if events[index]["side"] == "fund_void" or payload.get("side") == "fund_void":
        raise ValueError("作废流水不可更正")
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
        state["events"] = replay_edit(state, index, payload, min_day, max_day)
        state.setdefault("event_edits", []).append({"id": request_id, "event_id": old["id"],
                                                   "request": payload, "before": old,
                                                   "after": state["events"][index]})
        account_advice.invalidate_after_edit(state, _today().isoformat())
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
        view = account_valuation.valuation_view(state, today, price_day)
        views = strategy_views(state, view, today=today)
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
        record = account_valuation.close_record(
            state, asof, close_prices, revision=revision(state, asof),
        )
        state["records"] = sorted([r for r in state["records"] if r["date"] != asof] + [record],
                                  key=lambda r: r["date"])
        if not dry_run:
            _save(path, state)
        return state


def refresh_close_prices(path, today, price_day, close_prices):
    """盘前采集与手动刷新共同补齐同日收盘价；取数期间允许记账。"""
    if price_day is None:
        raise ValueError("交易日历不可用，无法确认估值日")
    state = load(path)
    codes = account_valuation.valuation_codes(state, today)
    known = account_valuation.closing_prices(state, price_day)
    needed = codes - known.keys()
    if needed:
        fetched = close_prices(sorted(needed), price_day)
        with _locked(path):
            state = load(path)
            known, changed = account_valuation.merge_closing_prices(state, price_day, needed, fetched)
            if changed:
                _save(path, state)
    return sorted(codes - known.keys())


def subscription_fees(state):
    """各场外基金最近登记的申购费率（后一笔覆盖前一笔），供表单预填与定投补记。"""
    return {event["code"]: event["subscription_fee"] for event in state["events"]
            if event["side"] in {"fund_buy", "fund_pending"} and "subscription_fee" in event}


def account_data(path):
    state = load(path)
    day = _today()
    today = day.isoformat()
    price_day = valuation_day(day)
    view = account_valuation.valuation_view(state, day, price_day)
    records = sorted([r for r in state["records"] if r["date"] <= today], key=lambda r: r["date"])
    valid = [r for r in records if positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    valid_dates = {r["date"] for r in valid}
    latest = valid[-1] if valid else None
    warnings = list(records[-1]["issues"]) if records else []
    if records and (not latest or latest["date"] != records[-1]["date"]):
        warnings.append("最新成绩待重新估值，曲线保留最近有效数据。")
    grouped_values = {"etf": [], "stock": [], "other": []}
    for row in view["positions"]:
        row["kind"] = holding_kind(row["code"])
        grouped_values[row["kind"]].append(row["market_value"])
    funds = view["fund_positions"]
    if funds:
        grouped_values["fund"] = [row["market_value"] for row in funds.values()]
    holdings_by_kind = {kind: round(math.fsum(values), 2) if None not in values else None
                        for kind, values in grouped_values.items() if kind != "other" or values}
    net_contributed = round(state["initial"]["equity"] + math.fsum(
        event["amount"] if event["side"] == "deposit" else -event["amount"]
        for event in state["events"] if event["date"] <= today and event["side"] in {"deposit", "withdraw"}), 2)
    fund_codes = {event["code"] for event in state["events"] if event["side"] in FUND_TRADES}
    fund_codes.update(code for asset in state["_config"]["core"] for code in asset.get("fund_codes", []))
    names = fund_names.names_for(fund_codes)
    for row in [*funds.values(), *view["fund_pending"].values()]:
        if row["code"] in names:
            row["name"] = names[row["code"]]
    strategies = strategy_views(state, view, today=day, names=names)
    core = next(strategy for strategy in strategies if strategy["id"] == "core")
    core["pending_fund_value"] = view["pending_fund_value"]
    core["allocation_budget"] = core["budget"]
    core["allocation_assets"] = core["assets"]
    fund_subscription_fees = subscription_fees(state)
    return {"id": "etf", "name": "投资账户", "can_record": True, **view,
            "holdings_by_kind": holdings_by_kind, "net_contributed": net_contributed,
            "fund_subscription_fee": state["_config"].get("fund_subscription_fee"),
            "fund_subscription_fees": fund_subscription_fees,
            "trade_fees": state["_config"].get("trade_fees", {}),
            "trade_fee_code_prefixes": {"stock": A_STOCK_PREFIXES, "etf": ETF_HOLDING_PREFIXES},
            "fund_names": {code: names.get(code) for code in sorted(fund_codes)},
            "performance": latest, "trades": [{**{k: v for k, v in e.items() if k != "request"},
                         **({"name": names.get(e["code"], f"场外基金 {e['code']}") if e["side"] in FUND_TRADES
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
            state = read_account(path, config)
        except Exception:
            logger.warning("投资账户读取失败，跳过持仓对照", exc_info=True)
            return config, None
    try:
        view = snapshot(state, _today().isoformat())
        if view["fund_positions"] or view["fund_pending"] or any(
            portfolio_config.grouped(row) for row in config["core"]
        ):
            return config, None
        core = account_advice.strategy_facts(state, view, "core")
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
