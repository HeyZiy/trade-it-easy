"""投资账户策略建议的生成、发布与成交后更新。"""

from datetime import date
import logging
from pathlib import Path

from data_provider.security_names import security_name
from src import account_benchmarks, account_valuation, portfolio_config
from src.account_replay import FUND_TRADES, display_event, snapshot
from src.account_store import load, revision, save
from src.advice_identity import short_digest
from src.json_store import locked

logger = logging.getLogger(__name__)


def suggestions(state, view, price_day, deviations, strategy):
    from src.etf.rebalancer import ETFRebalancer

    key = strategy["id"]
    config = state["_config"]
    grouped_codes = {row["code"] for row in config[key] if portfolio_config.grouped(row)}
    engine = ETFRebalancer(asset for asset in portfolio_config.allocations(config, key)
                           if asset.code not in grouped_codes)
    orders, _ = engine.orders_for_deviations(
        strategy["target_weights"], strategy["positions"], strategy["budget"], deviations, prices=view["prices"])
    rev, result = revision(state), []
    namespace = rev if key == "core" else f"{rev}:{key}"
    for order in orders:
        deviation = deviations[order.code]
        price = view["prices"][order.code]
        identity = short_digest(f"{namespace}:{price_day}:{order.code}:{order.action}:{order.qty}:{price}")
        if identity in state["dismissed"]:
            continue
        result.append({"id": identity, "code": order.code, "name": order.name,
                       "side": order.action, "qty": order.qty, "price": price,
                       "amount": round(order.qty * price, 2),
                       "gap_amount": order.amount,
                       "current_pct": order.current_pct, "target_pct": order.target_pct,
                       "current_value": deviation.current_value, "target_value": deviation.target_value,
                       "price_date": price_day, "price_issue": None, "reason": order.reason})
    cash = strategy["cash"]
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


def allocation_groups(assets, strategy, prices, names):
    ungrouped_listed = {row["code"]: row for row in strategy["positions"]}
    ungrouped_funds = dict(strategy["fund_positions"])
    for order in strategy["fund_pending"].values():
        code = order["code"]
        row = ungrouped_funds.get(code, {"code": code, "name": names.get(code), "market_value": 0})
        ungrouped_funds[code] = {**row, "pending_amount": round(row.get("pending_amount", 0) + order["amount"], 2)}
    groups = []
    for asset in assets:
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
            position = ungrouped_funds.pop(member, {"code": member, "market_value": 0})
            products.append({**position, "name": names.get(member) or position.get("name"), "kind": "fund"})
        first_product = products[0]
        values = [row["market_value"] + row.get("pending_amount", 0)
                  if row["market_value"] is not None else None for row in products]
        groups.append({
            "id": code,
            "name": asset.get("label") or first_product.get("name") or f"场外基金 {first_product['code']}",
            "name_fund_code": first_product["code"] if not asset.get("label") and first_product["kind"] == "fund" else None,
            "weight": asset["weight"],
            "market_value": None if None in values else round(sum(values), 2),
            "products": products,
            "reference_price_missing": not portfolio_config.grouped(asset) and code not in prices,
        })
    groups.extend({"id": row["code"], "name": row["name"], "weight": 0, "market_value": row["market_value"],
                   "products": [{**row, "kind": "listed"}], "outside": True} for row in ungrouped_listed.values())
    groups.extend({"id": f"outside-fund:{code}", "name": row.get("name") or f"场外基金 {code}",
                   "name_fund_code": code, "weight": 0,
                   "market_value": (row["market_value"] + row.get("pending_amount", 0)
                                    if row["market_value"] is not None else None),
                   "products": [{**row, "kind": "fund"}], "outside": True}
                  for code, row in ungrouped_funds.items())
    return groups


def strategy_facts(state, view, key):
    """按策略标识提供账户事实，不计算建议；供展示、发布和成交处理共用。"""
    name = portfolio_config.OWNER_NAMES[key]
    positions = [r for r in view["strategy_positions"] if r["strategy"] == key]
    funds = view["strategy_fund_positions"].get(key, {})
    holdings = [*positions, *funds.values()]
    pending = view["strategy_pending_fund_value"].get(key, 0)
    invested = (round(sum(r["market_value"] for r in holdings) + pending, 2)
                if all(r["market_value"] is not None for r in holdings) else None)
    weight = state.get("budgets", {}).get(key)
    budget = round(view["equity"] * weight, 2) if weight is not None and view["equity"] is not None else None
    remaining = max(0, round(budget - invested, 2)) if budget is not None and invested is not None else None
    cash = view["cash_by_strategy"][key]
    assets = round(invested + cash, 2) if invested is not None else None
    status = ("目标比例待设置" if weight is None
              else "待估值" if remaining is None
              else "已分配" if holdings or pending or cash > 0 else "暂无持仓")
    thresholds = None
    allocation = key in portfolio_config.ALLOCATION_NAMES
    if allocation:
        from src.etf.config import MIN_TRADE_AMOUNT, REBALANCE_RELATIVE_THRESHOLD, REBALANCE_SINGLE_THRESHOLD
        thresholds = {"trade_amount": MIN_TRADE_AMOUNT, "single": REBALANCE_SINGLE_THRESHOLD,
                      "relative": REBALANCE_RELATIVE_THRESHOLD}
    return {"id": key, "name": name, "account_id": "etf", "positions": positions,
            "budget_pct": weight, "budget": budget, "invested": invested,
            "pnl": view["strategy_pnl"][key] if invested is not None else None,
            "target_weights": ({r["code"]: r["weight"] for r in state["_config"].get(key, ())}
                               if allocation else None),
            "rebalance_thresholds": thresholds,
            "remaining_budget": remaining, "buying_power": min(cash, remaining) if remaining is not None else None,
            "suggestions": [], "trades": [e for e in map(display_event, state["events"])
                                           if e.get("strategy", "core" if e["side"] in FUND_TRADES else None) == key
                                           or e.get("from_strategy") == key
                                           or (e["side"] == "cash_assign" and key in e["cash_allocations"])
                                           or (e["side"] == "cash_adjust" and key in e.get("cash_allocations", {}))],
            "history": [], "performance": None,
            "benchmark_name": account_benchmarks.benchmark_name(key) if key in portfolio_config.STRATEGY_NAMES else None,
            "residual_pct": max(0, 1 - sum(state["budgets"].values())) if key == portfolio_config.HOLDING_AREA else None,
            "cash": cash, "assets": assets, "actual_pct": assets / view["equity"] if view["equity"] and assets is not None else None,
            "deviation": round(assets - budget, 2) if budget is not None and assets is not None else None, "status": status}


def allocation_view(state, view, key, *, names=None):
    """配置组合的金额偏离及建议，供看板和收盘报告共用。"""
    from src.etf.rebalancer import allocation_deviation

    row = strategy_facts(state, view, key)
    row.update(fund_positions=view["strategy_fund_positions"].get(key, {}),
               fund_pending={order_id: order for order_id, order in view["fund_pending"].items()
                             if order["strategy"] == key},
               pending_fund_value=view["strategy_pending_fund_value"][key],
               allocation_budget=row["budget"], allocation_assets=row["assets"])
    assets = state["_config"].get(key, ())
    groups = allocation_groups(assets, row, view["prices"], names or {})
    deviations = {}
    for group in groups:
        deviation = allocation_deviation(group["market_value"], group["weight"], row["budget"])
        deviations[group["id"]] = deviation
        group.update(target_value=deviation.target_value, gap_amount=deviation.gap,
                     absolute_deviation=deviation.absolute, relative_deviation=deviation.relative,
                     absolute_review=deviation.absolute_review, relative_review=deviation.relative_review,
                     trade_eligible=deviation.trade_eligible)
    row["allocation_groups"] = groups
    if assets and row["status"] == "已分配" and row["budget"] > 0:
        row["suggestions"] = suggestions(state, view, view["valuation"]["date"], deviations, row)
    return row


def strategy_history(state, key, today):
    """策略收益曲线：账户成绩有效且策略指纹（含划转）一致时才展示当日净值。"""
    from src.account_store import revision
    records, valid = account_valuation.valid_records(state, today)
    valid_dates = {r["date"] for r in valid}
    benchmarks = None
    history = []
    for r in records:
        current = (r["date"] in valid_dates
                   and r.get("strategy_revision") == revision(state, r["date"], include_transfers=True))
        nav = (r.get("strategy_navs") or {}).get(key) if current else None
        if benchmarks is None and nav is not None:
            benchmarks = account_benchmarks.benchmark_navs(state, key, r["date"])
        history.append({"date": r["date"],
                        "nav": nav,
                        "benchmark_nav": (benchmarks or {}).get(r["date"]) if nav is not None else None})
    return history


def strategy_views(state, view, *, today, names=None):
    """汇总策略事实与建议；策略标识决定归属，展示顺序不参与规则判定。"""
    # 独立信号沿用其发布与成交时的账户事实；配置份额使用统一收盘估值。
    signal_view = snapshot(state, today.isoformat()) if any(
        state.get(f"{key}_signal") for key in ("rotation", "quality")
    ) else view
    result = []
    for key in portfolio_config.OWNER_NAMES:
        row = (allocation_view(state, view, key, names=names) if key in portfolio_config.ALLOCATION_NAMES
               else strategy_facts(state, view, key))
        if key == portfolio_config.HOLDING_AREA:
            row.update(holding_only=True, status=row["name"], fund_positions=view["strategy_fund_positions"].get(key, {}),
                       fund_pending={i: order for i, order in view["fund_pending"].items() if order["strategy"] == key},
                       pending_fund_value=view["strategy_pending_fund_value"][key])
            result.append(row)
            continue
        row["history"] = strategy_history(state, key, today.isoformat())
        if key == "rotation":
            from src.etf.rotation_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("rotation_signal"), strategy_facts(state, signal_view, key), today,
                account_revision=revision(state), dismissed=state["dismissed"])
            row["signal_date"] = state.get("rotation_signal", {}).get("date")
        if key == "quality":
            from src.quality_pool.account_advice import advice
            row["suggestions"], row["advice_status"] = advice(
                state.get("quality_signal"), strategy_facts(state, signal_view, key), today,
                dismissed=state["dismissed"])
            row["signal_date"] = state.get("quality_signal", {}).get("date")
        result.append(row)
    return result


def publish_rotation(day, rows, prices, diag, *, path, dry_run=False, error=None):
    """锁内发布轮动信号并返回本次建议；账户写入校验与其他记账共用。"""
    from src.etf import rotation_advice

    path = Path(path)
    if not path.exists():
        return "投资账户尚未初始化"
    with locked(path):
        state = load(path)
        signal = rotation_advice.make_signal(day, rows, prices, diag, error,
                                             previous=state.get("rotation_signal"))
        if signal is None:
            return "已有更新日期的账户建议，本次未覆盖"
        state["rotation_signal"] = signal
        strategy = strategy_facts(state, snapshot(state, day), "rotation")
        suggestions, status = rotation_advice.advice(
            signal, strategy, date.fromisoformat(day),
            account_revision=revision(state), dismissed=state["dismissed"])
        if not dry_run:
            save(path, state)
    return rotation_advice.render(suggestions, status)


def publish_quality(day, *, path, dry_run=False):
    """质量池使用实际持仓独立生成名单；取数期间释放锁，提交时检查账户版本。"""
    from copy import deepcopy
    from src.quality_pool import account_advice, assembly, signal, state as pool_state
    from src.etf.rotation_advice import render

    path = Path(path)
    if not path.exists():
        return "投资账户尚未初始化"
    with locked(path):
        account = load(path)
        expected = revision(account)
    previous = account.get("quality_signal")
    if previous and previous["date"] > day:
        return "已有更新日期的质量池建议，本次未覆盖"
    strategy = strategy_facts(account, snapshot(account, day), "quality")
    if not strategy["budget"]:
        return "请先设置质量动量目标比例"
    if account_advice.is_current(previous, day):
        rows, status = account_advice.advice(previous, strategy, date.fromisoformat(day),
                                             dismissed=account["dismissed"])
        return f"## 投资账户建议\n\n{render(rows, status)}"
    state = pool_state.restore(deepcopy(account.get("quality_state") or pool_state.empty_state()))

    def commit(value, published=None):
        nonlocal expected
        if dry_run:
            return
        with locked(path):
            current = load(path)
            if revision(current) != expected:
                raise ValueError("质量动量运行期间账户已更新，请重新运行")
            current["quality_state"] = deepcopy(value)
            current["quality_signal"] = published or {"date": day, "issues": "质量动量名单更新中，暂停建议"}
            save(path, current)
            expected = revision(current)

    signal_report = None
    try:
        signal_report = signal.run_signal(date.fromisoformat(day), dry_run=dry_run,
                          state=state, held_codes={p["code"] for p in strategy["positions"]},
                          state_writer=commit)
        if state.get("signal_retry"):
            raise ValueError("质量动量数据不完整，本轮暂停，等待重试")
        plan = signal.SignalPlan.from_stored(state.get("pending_plan"), day)
        execution_snapshot = assembly.build_snapshot(day, plan.entries if plan else [], account_facts=strategy)
        published = account_advice.make_signal(day, state, execution_snapshot, plan=plan)
    except Exception as error:
        logger.exception("质量动量实际账户建议更新失败")
        published = {"date": day, "issues": str(error)}
    # 若用户在取数期间记账，提交失败，保留用户数据并要求重新生成建议。
    commit(state, published)
    rows, status = account_advice.advice(published, strategy, date.fromisoformat(day),
                                         dismissed=account["dismissed"])
    advice_report = render(rows, status)
    if signal_report:
        return f"## 投资账户建议\n\n{advice_report}\n\n## 持有资格与候选\n\n{signal_report}"
    return advice_report


def on_event(state, view, event, today):
    """在记账前用当前账户事实更新建议的成交进度。"""
    side = event["side"]
    if event.get("strategy") == "rotation":
        from src.etf.rotation_advice import on_event
        signal = on_event(state.get("rotation_signal"), event,
                          strategy_facts(state, view, "rotation"), today,
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


def invalidate_after_edit(state, day):
    """历史流水更正后，清除基于旧流水的建议和待执行计划。"""
    state["dismissed"] = []
    # 已有操作计划基于旧账，不再沿用；下一次策略运行重新生成。
    for key in ("rotation_signal", "quality_signal"):
        if key in state:
            state[key] = {"date": day, "issues": "历史流水已更正，等待重新生成建议"}
    if state.get("quality_state"):
        state["quality_state"].update(pending_plan=None, signal_retry=True)
