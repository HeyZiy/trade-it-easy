"""账户与策略基准，按估值交易日固定权重合成。"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
import math

from src.account_replay import positive
from src.account_store import revision
from src.account_valuation import valid_records
from src.trading_calendar import TRADING_DAY_LOOKBACK_DAYS, get_trading_dates

CSI_300 = "H00300"
CSI_BOND = "H11001"
DEFINITIONS = {
    "etf": ("沪深300全收益60% + 中证全债40%", {CSI_300: .60, CSI_BOND: .40}),
    "core": ("沪深300全收益30% + 标普500 ETF净值35% + 黄金ETF净值5% + 货币30%",
             {CSI_300: .30, "513500": .35, "159934": .05, "001821": .30}),
    "rotation": ("沪深300全收益", {CSI_300: 1.0}),
    "quality": ("中证800全收益", {"H00906": 1.0}),
}
METHOD = "cn-close-daily-weights-v1"


def benchmark_name(key):
    return DEFINITIONS[key][0]


def benchmark_navs(state, key, anchor):
    saved = state.get("benchmark_series", {}).get(key, {})
    if (saved.get("anchor") == anchor and saved.get("method") == METHOD
            and saved.get("weights") == DEFINITIONS[key][1]):
        return saved["navs"]
    return {}


@dataclass
class PendingBenchmark:
    key: str
    anchor: str
    known: dict[str, float]
    missing: tuple[str, ...]
    start: str


def pending_benchmarks(state, through):
    """各组合从自身首个有效净值日开始，续算从第一个缺口之前的基准净值起步。"""
    _, valid = valid_records(state, through)
    dates = {key: [] for key in DEFINITIONS}
    for row in valid:
        day = row["date"]
        dates["etf"].append(day)
        if row.get("strategy_revision") == revision(state, day, include_transfers=True):
            for key, nav in row.get("strategy_navs", {}).items():
                if key in dates and positive(nav):
                    dates[key].append(day)

    plans = []
    for key, days in dates.items():
        if not days:
            continue
        anchor = days[0]
        known = benchmark_navs(state, key, anchor)
        missing = tuple(day for day in days if day not in known)
        if missing:
            start = max((day for day in known if day < missing[0]), default=anchor)
            plans.append(PendingBenchmark(key, anchor, known, missing, start))
    return plans


def collect_benchmarks(plans, load_returns):
    if not plans:
        return {}
    first = min(date.fromisoformat(plan.start) for plan in plans)
    through = max(date.fromisoformat(plan.missing[-1]) for plan in plans)
    sessions = [day.isoformat() for day in get_trading_dates(
        first - timedelta(days=TRADING_DAY_LOOKBACK_DAYS), through)]
    # 至少包含起点对应的上一收盘，作为第一段收益的分母。
    start_index = bisect_right(sessions, first.isoformat()) - 1
    sessions = sessions[start_index:] if start_index >= 0 else []
    symbols = {symbol for plan in plans if plan.missing[-1] > plan.start for symbol in DEFINITIONS[plan.key][1]}
    returns = load_returns(sorted(symbols), sessions) if symbols and len(sessions) > 1 else {}

    results = {}
    for plan in plans:
        weights = DEFINITIONS[plan.key][1]
        navs = {**plan.known, plan.anchor: 1.0}
        level = navs[plan.start]
        missing = set(plan.missing)
        for day in sessions[bisect_right(sessions, plan.start):]:
            if day > plan.missing[-1]:
                break
            if day in navs:
                level = navs[day]
                continue
            daily = [returns.get(symbol, {}).get(day) for symbol in weights]
            if level is None or not all(value is not None and math.isfinite(value) and value > -1 for value in daily):
                level = None
                continue
            level *= 1 + math.fsum(weight * value for weight, value in zip(weights.values(), daily))
            if day in missing:
                navs[day] = level
        results[plan.key] = {"method": METHOD, "weights": weights, "anchor": plan.anchor, "navs": navs}
    return results
