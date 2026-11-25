"""投资账户的收盘成绩与仪表盘估值。"""

from datetime import time, timedelta

from src import portfolio_config
from src.account_replay import positive, snapshot
from data_provider.closing_prices import holding_closes
from src.trading_calendar import SHANGHAI, latest_trading_day_on_or_before

def valid_records(state, today):
    """收盘记录按日期截断与有效性过滤（账户成绩唯一口径）。"""
    from src.account_store import revision
    records = sorted((r for r in state["records"] if r["date"] <= today), key=lambda r: r["date"])
    valid = [r for r in records if positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    return records, valid


VALUATION_CUTOFF = time(18, 30)


def valuation_day(today, *, now=None):
    """场内估值日：北京时间交易日 18:30 起取当日，其余时间取前一交易日。

    now 缺省按盘前口径（前一交易日），供只有日期的调用方与测试使用；
    生产调用方传入当前北京时间，晚间看板与采集切换到当日收盘。
    """
    if now is not None and now.astimezone(SHANGHAI).time() >= VALUATION_CUTOFF:
        day = latest_trading_day_on_or_before(today, fallback=None)
    else:
        day = latest_trading_day_on_or_before(today - timedelta(days=1), fallback=None)
    return day.isoformat() if day else None


def close_record(state, asof, close_prices, *, revision, strategy_revision=None):
    """生成采集日的收盘成绩，缺收盘价或基金净值时标记待估值。"""
    current = snapshot(state, asof)
    codes = [row["code"] for row in current["positions"]]
    prices, missing = holding_closes(codes, asof, close_prices)
    issues = ["缺少当日收盘价：" + "、".join(missing)] if missing else []
    view = snapshot(state, asof, prices)
    for key in ("trading_accounts", "custody_cash", "custody_positions"):
        view.pop(key)
    missing_funds = [code for code, row in view["fund_positions"].items() if row["nav"] is None]
    if missing_funds:
        issues.append("缺少场外基金净值：" + "、".join(missing_funds))
    navs = view["strategy_navs"] if not issues else dict.fromkeys(portfolio_config.STRATEGY_NAMES)
    return {**view, "date": asof, "nav": None if issues else view["equity"] / view["units"],
            "strategy_navs": navs,
            "strategy_revision": strategy_revision,
            "issues": issues, "revision": revision,
            "closing_prices": {code: float(price) for code, price in prices.items()},
            "source": "收盘行情"}


def valuation_codes(state, today):
    """返回仪表盘估值所需的场内持仓及配置参考标的。"""
    codes = {row["code"] for row in snapshot(state, today.isoformat())["positions"]}
    for asset in portfolio_config.allocation_rows(state["_config"]):
        if asset["code"] != "CASH" and not portfolio_config.fund_only(asset):
            codes.update((asset["code"], *asset.get("etf_codes", ())))
    return codes


def closing_prices(state, day):
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
    return {code: price for code, price in prices.items() if positive(price)}


def merge_closing_prices(state, price_day, needed, fetched):
    """向最新状态补齐同日缺价，返回已知价格与是否需要保存。"""
    known = closing_prices(state, price_day)
    # 同一估值日只补缺价，防止覆盖并行任务刚补到的价格。
    additions = {code: fetched[code] for code in needed - known.keys()
                 if positive(fetched.get(code))}
    saved = state.get("valuation_closes", {})
    if additions and saved.get("date", "") <= price_day:
        state["valuation_closes"] = {"date": price_day, "prices": {**known, **additions}}
        return state["valuation_closes"]["prices"], True
    return known, False


def valuation_view(state, today, price_day):
    """按估值日展示当前持仓，缺价的资产及相关汇总标记待估值。"""
    prices = closing_prices(state, price_day)
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
    for key, positions in view["strategy_fund_positions"].items():
        for row in positions.values():
            row.pop("book_price")
            if row["nav"] is None:
                row["market_value"] = None
                view["strategy_pnl"][key] = None
    if missing or missing_funds:
        view.update(invested=None, equity=None, pnl=None)
    view["prices"] = prices
    # 按策略和产品分摊已展示市值，最后一笔吸收分币舍入。
    listed = {(row["strategy"], row["code"]): row for row in view["strategy_positions"]}
    cumulative = {}
    for row in view["custody_positions"]:
        key = row["strategy"], row["code"]
        fund = row["kind"] == "fund"
        holding = view["strategy_fund_positions"][key[0]][key[1]] if fund else listed[key]
        group = row["kind"], *key
        prior_qty, prior_value = cumulative.get(group, (0, 0))
        qty = round(prior_qty + row["qty"], 6)
        value = holding["market_value"]
        if value is None:
            row["market_value"] = None
        else:
            subtotal = round(value * qty / (holding["qty"] if fund else holding["count"]), 2)
            row["market_value"] = round(subtotal - prior_value, 2)
            cumulative[group] = qty, subtotal
    view["valuation"] = {"date": price_day, "missing": sorted(missing)}
    if missing_funds:
        view["valuation"]["missing_funds"] = sorted(missing_funds)
    return view
