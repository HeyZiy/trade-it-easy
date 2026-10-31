"""核心实际账户：期初快照、人工流水、收盘估值和调仓建议。"""

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

FILENAME = "core_account.json"
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
    return json.loads(Path(path).read_text(encoding="utf-8"))


def revision(state, asof=None):
    facts = {"initial": state["initial"], "events": [e for e in state["events"]
             if asof is None or e["date"] <= asof]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initialize(path, *, asof, cash, positions, targets=None):
    """期初价格用于建立收益起点，不伪造成交成本或历史交易。"""
    _day(asof)
    if type(cash) not in (int, float) or not math.isfinite(cash) or cash < 0:
        raise ValueError("现金须为有限非负数")
    rows, codes = [], set()
    for item in positions:
        code = canonical_stock_code(item["code"])
        count, price = item["count"], _money(item["price"])
        if code in codes or not _positive(count) or int(count) != count:
            raise ValueError("代码不得重复，份额须为正整数")
        codes.add(code)
        rows.append({"code": code, "name": item["name"], "count": int(count), "price": price})
    if not rows:
        raise ValueError("初始化持仓不能为空")
    targets = {canonical_stock_code(c): float(w) for c, w in (targets or {}).items()}
    if any(not math.isfinite(w) or not 0 <= w <= 1 for w in targets.values()) or sum(targets.values()) > 1:
        raise ValueError("目标权重无效")
    equity = round(cash + sum(r["count"] * r["price"] for r in rows), 2)
    state = {"initial": {"date": asof, "cash": cash, "positions": rows, "equity": equity},
             "targets": targets, "events": [], "records": [], "dismissed": []}
    mark = snapshot(state, asof)
    state["records"] = [{**mark, "date": asof, "nav": 1.0, "benchmark_nav": None,
                         "issues": [], "source": "用户确认的期初估值",
                         "revision": revision(state, asof)}]
    with _locked(path):
        if Path(path).exists():
            raise ValueError("核心实际账户已存在，禁止覆盖初始化")
        _save(path, state)
    return state


def snapshot(state, asof, prices=None):
    """重放已确认流水；资金流按当时可用估值增减净值份额，不计为盈利。"""
    initial = state["initial"]
    cash, units = initial["cash"], initial["equity"]
    held = {r["code"]: {**r, "avail_count": r["count"]} for r in initial["positions"]}
    marks = {r["code"]: r["price"] for r in initial["positions"]}
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
        elif side == "dividend":
            cash += event["amount"]
        else:
            code, qty, price = event["code"], event["qty"], event["price"]
            row = held.setdefault(code, {"code": code, "name": event["name"], "count": 0, "avail_count": 0})
            row["name"] = event["name"]
            marks[code] = price
            if side == "buy":
                row["count"] += qty
                if event["date"] < asof:
                    row["avail_count"] += qty
                cash -= round(qty * price + event["fee"], 2)
            else:
                row["count"] -= qty
                row["avail_count"] = max(0, row["avail_count"] - qty)
                cash += round(qty * price - event["fee"], 2)
    while record:
        marks.update({r["code"]: r["close"] for r in record["positions"] if _positive(r.get("close"))})
        record = next(records, None)
    marks.update(prices or {})
    rows = [{"code": code, "name": row["name"], "count": row["count"],
             "avail_count": row["avail_count"], "cost_price": None, "account": "core",
             "close": marks.get(code), "current_price": marks.get(code),
             "market_value": round(row["count"] * marks[code], 2)}
            for code, row in held.items() if row["count"] > 0]
    invested = round(sum(r["market_value"] for r in rows), 2)
    return {"positions": rows, "cash": round(cash, 2), "invested": invested,
            "equity": round(cash + invested, 2), "units": units, "receivables": 0,
            "prices": marks}


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
    return [AssetAllocation(c, names.get(c, c), AssetType.GOLD if c == "159934" else AssetType.EQUITY, w, 0)
            for c, w in state["targets"].items()]


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
        if side not in {"buy", "sell", "dividend", "deposit", "withdraw"}:
            raise ValueError("不支持的记账类型")
        event = {"id": request_id, "date": day, "side": side, "request": payload}
        view = snapshot(state, day)
        if side in {"buy", "sell"}:
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
                held = next((r for r in view["positions"] if r["code"] == code), None)
                if not held or qty > held["count"]:
                    raise ValueError("卖出份额超过实际持仓")
            if side == "buy" and round(qty * price + fee, 2) > view["cash"]:
                raise ValueError("实际现金不足以记录这笔买入")
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > 80:
                raise ValueError("请填写有效的标的名称")
            event.update(code=code, name=name, qty=int(qty), price=price, fee=float(fee))
        else:
            amount = round(_money(payload.get("amount")), 2)
            if amount <= 0 or (side == "withdraw" and amount > view["cash"]):
                raise ValueError("金额无效或超过实际现金")
            if side == "withdraw" and amount >= view["equity"]:
                raise ValueError("暂不支持全额清户，请保留正的账户资产")
            event["amount"] = amount
        state["events"].append(event)
        _save(path, state)
        return event


def dismiss(path, payload):
    with _locked(path):
        state = load(path)
        if payload.get("revision") != revision(state):
            raise ValueError("账户已更新，请刷新后重新核对")
        available = {r["id"] for r in suggestions(state, snapshot(state, date.today().isoformat()))}
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
    today = date.today().isoformat()
    view = snapshot(state, today)
    records = sorted([r for r in state["records"] if r["date"] <= today], key=lambda r: r["date"])
    valid = [r for r in records if _positive(r.get("nav")) and not r["issues"]
             and (r["source"] == "用户确认的期初估值" or r["revision"] == revision(state, r["date"]))]
    valid_dates = {r["date"] for r in valid}
    latest = valid[-1] if valid else None
    warnings = list(records[-1]["issues"]) if records else []
    if records and (not latest or latest["date"] != records[-1]["date"]):
        warnings.append("最新成绩待重新估值，曲线保留最近有效数据。")
    return {"id": "core", "name": "核心配置", "kind": "actual", **view,
            "performance": latest, "trades": [{k: v for k, v in e.items() if k != "request"}
                                              for e in reversed(state["events"])][:20],
            "start_date": state["initial"]["date"], "initial_equity": state["initial"]["equity"],
            "revision": revision(state), "suggestions": suggestions(state, view),
            "valuation_date": latest["date"] if latest else state["initial"]["date"],
            "has_events": bool(state["events"]), "benchmark_name": "暂未设置", "warnings": warnings,
            "history": [{"date": r["date"], "nav": r["nav"] if r["date"] in valid_dates else None,
                         "benchmark_nav": None} for r in records]}


def strategy_snapshot(path=ACCOUNT_PATH):
    from src.trade_ledger.ledger import LedgerSnapshot

    view = snapshot(load(path), date.today().isoformat())
    return LedgerSnapshot(positions=view["positions"], entry_map={}, cash=view["cash"],
                          invested=view["invested"], total_assets=view["equity"])


def render_summary(path=ACCOUNT_PATH):
    view = account_data(path)
    return (f"## 核心配置账户\n\n总资产 {view['equity']:,.2f} 元｜现金 {view['cash']:,.2f} 元｜"
            f"持仓 {len(view['positions'])} 只\n\n策略只提供建议，实际成交由人工确认记账。")
