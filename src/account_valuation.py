"""投资账户的收盘成绩与仪表盘估值。"""

from datetime import date, time, timedelta

from src import portfolio_config
from src.account_replay import positive, snapshot
from src.trade_ledger.closing_prices import holding_closes
from src.trading_calendar import SHANGHAI, latest_trading_day_on_or_before

# 同期基准：沪深300。起点为期初估值日（非交易日则取之前最近交易日）的指数收盘，
# 与账户 nav=1 的期初估值锚定同一时点；首次取数成功后冻结，不随配置变更重写历史。
BENCHMARK = {"name": "沪深300", "index": "000300"}
# 策略基准只给质量动量配（A股股票池，研究口径中证800）；
# 跨资产动量与多资产配置含债/金/海外/现金，对单一股票指数不公平，暂不配。
STRATEGY_BENCHMARKS = {"quality": {"name": "中证800", "index": "000906"}}


def strategy_benchmark_navs(state, asof, navs, index_prices):
    """策略基准同期净值；起点锚定该策略首个有效净值日并冻结。

    策略 nav 从首次入金为 1，基准须同日起算才是同窗口对比；
    锚定日非交易日时收敛到之前最近交易日收盘。取数失败留空，不影响策略净值。
    """
    result = {}
    for key, spec in STRATEGY_BENCHMARKS.items():
        if not positive(navs.get(key)):
            continue
        frozen = (state.get("strategy_benchmarks") or {}).get(key)
        if frozen is None:
            if index_prices is None:
                continue
            day = latest_trading_day_on_or_before(date.fromisoformat(asof), fallback=None)
            if day is None:
                continue
            baseline_date = day.isoformat()
            base = index_prices([spec["index"]], baseline_date).get(spec["index"])
            if not positive(base):
                continue
            frozen = {**spec, "baseline_date": baseline_date, "baseline_price": float(base)}
            state.setdefault("strategy_benchmarks", {})[key] = frozen
            result[key] = 1.0
        elif asof <= frozen["baseline_date"]:
            result[key] = 1.0
        elif index_prices is not None:
            current = index_prices([frozen["index"]], asof).get(frozen["index"])
            if positive(current):
                result[key] = float(current) / frozen["baseline_price"]
    return result


def valid_records(state, today):
    """收盘记录按日期截断与有效性过滤（账户成绩唯一口径）。"""
    from src.account_store import revision
    records = sorted((r for r in state["records"] if r["date"] <= today), key=lambda r: r["date"])
    valid = [r for r in records if positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    return records, valid


def benchmark_nav(state, asof, index_prices):
    """基准同期净值；指数取数失败返回 None，不影响账户净值有效性。"""
    if index_prices is None:
        return None
    spec = state.get("benchmark")
    if spec is None:
        day = latest_trading_day_on_or_before(
            date.fromisoformat(state["initial"]["date"]), fallback=None)
        if day is None:
            return None
        baseline_date = day.isoformat()
        base = index_prices([BENCHMARK["index"]], baseline_date).get(BENCHMARK["index"])
        if not positive(base):
            return None
        spec = {**BENCHMARK, "baseline_date": baseline_date, "baseline_price": float(base)}
        state["benchmark"] = spec
    if asof <= spec["baseline_date"]:
        return 1.0
    current = index_prices([spec["index"]], asof).get(spec["index"])
    return float(current) / spec["baseline_price"] if positive(current) else None


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


def close_record(state, asof, close_prices, *, revision, strategy_revision=None, index_prices=None):
    """生成采集日的收盘成绩，缺收盘价或基金净值时标记待估值。"""
    current = snapshot(state, asof)
    codes = [row["code"] for row in current["positions"]]
    prices, missing = holding_closes(codes, asof, close_prices)
    issues = ["缺少当日收盘价：" + "、".join(missing)] if missing else []
    view = snapshot(state, asof, prices)
    missing_funds = [code for code, row in view["fund_positions"].items() if row["nav"] is None]
    if missing_funds:
        issues.append("缺少场外基金净值：" + "、".join(missing_funds))
    navs = view["strategy_navs"] if not issues else dict.fromkeys(portfolio_config.STRATEGY_NAMES)
    return {**view, "date": asof, "nav": None if issues else view["equity"] / view["units"],
            "benchmark_nav": benchmark_nav(state, asof, index_prices),
            "strategy_navs": navs,
            "strategy_benchmark_navs": strategy_benchmark_navs(state, asof, navs, index_prices),
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
    view["valuation"] = {"date": price_day, "missing": sorted(missing)}
    if missing_funds:
        view["valuation"]["missing_funds"] = sorted(missing_funds)
    return view
