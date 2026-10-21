"""模拟账户收盘成绩：独立于交易执行，缺价不生成净值，报告只读已保存成绩。"""

from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile

from .ledger import DEFAULT_LEDGER_PATH, NOMINAL_EQUITY, derive, load_trades

logger = logging.getLogger(__name__)


def performance_path(ledger_path):
    return Path(ledger_path).with_suffix(".performance.json")


def _load(ledger_path):
    path = performance_path(ledger_path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_performance(ledger_path):
    """只读已保存的成绩状态；无记录返回空字典（采集方判断缺行用）。"""
    return _load(ledger_path)


def _fingerprint(trades, asof):
    payload = [asdict(t) for t in trades if t.date <= asof]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _save(ledger_path, state):
    path = performance_path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_close(ledger_path, asof, benchmark, *, close_prices, index_prices,
                  trading_dates, dry_run=False):
    """保存指定交易日收盘；数据供料函数均接受明确日期，不允许最新价代替历史价。

    benchmark: {name, weights}（含 CASH、期初权重买入后持有）或 {name, index}。
    起点固定为首笔成交日，基准从该日前一交易日收盘起算。独立台账独立记录。
    """
    date.fromisoformat(asof)
    trades = load_trades(ledger_path)
    started = [t.date for t in trades if t.date <= asof]
    if not started:
        return {}
    start = min(started)
    state = _load(ledger_path)
    if state and state["start_date"] != start:
        raise ValueError("模拟起点已变化，须重新建立收盘成绩记录")
    if not state:
        prior = trading_dates(date.fromisoformat(start) - timedelta(days=40),
                              date.fromisoformat(start) - timedelta(days=1))
        if not prior:
            raise ValueError("交易日历不可用，无法确定基准起点")
        state = {"start_date": start, "baseline_date": max(prior).isoformat(),
                 "benchmark": benchmark, "baseline_prices": {}, "records": []}
    if date.fromisoformat(asof) not in trading_dates(date.fromisoformat(asof), date.fromisoformat(asof)):
        raise ValueError("收盘日期不是已确认交易日")

    spec = state["benchmark"]  # 首次采集后冻结，配置变更不重写历史对照。
    unpriced = derive(trades, asof)
    codes = sorted(unpriced.held_counts())
    issues = []
    try:
        prices = close_prices(codes, asof) if codes else {}
    except Exception:
        logger.exception("收盘持仓估值失败")
        prices = {}
    missing = [c for c in codes if not _positive(prices.get(c))]
    if missing:
        issues.append("估值不完整，缺少收盘价：" + "、".join(missing))
    snapshot = derive(trades, asof, {c: prices[c] for c in codes if c not in missing})
    valid = not missing and _positive(snapshot.total_assets) and math.isfinite(snapshot.cash)
    if not valid and not missing:
        issues.append("总权益无效，暂停展示收益")

    benchmark_nav = None
    try:
        if "index" in spec:
            targets = [spec["index"]]
            base = state["baseline_prices"]
            if not all(_positive(base.get(c)) for c in targets):
                base = index_prices(targets, state["baseline_date"])
            current = index_prices(targets, asof)
        else:
            weights = spec["weights"]
            if (any(not math.isfinite(w) or w < 0 for w in weights.values())
                    or not math.isclose(sum(weights.values()), 1.0)):
                raise ValueError("参考组合权重无效")
            targets = [c for c, w in weights.items() if c != "CASH" and w > 0]
            base = state["baseline_prices"]
            if not all(_positive(base.get(c)) for c in targets):
                base = close_prices(targets, state["baseline_date"])
            current = close_prices(targets, asof)
        if all(_positive(base.get(c)) for c in targets):
            state["baseline_prices"] = {c: float(base[c]) for c in targets}
            if all(_positive(current.get(c)) for c in targets):
                benchmark_nav = (current[targets[0]] / base[targets[0]] if "index" in spec
                                 else weights.get("CASH", 0.0) + sum(
                                     weights[c] * current[c] / base[c] for c in targets))
    except Exception:
        logger.exception("同期基准取数失败")
    if not _positive(benchmark_nav):
        benchmark_nav = None
    if benchmark_nav is None:
        issues.append("同期基准数据不完整，基准和超额收益暂缺")
    days = trading_dates(date.fromisoformat(start), date.fromisoformat(asof))
    row = {"date": asof, "nav": snapshot.total_assets / NOMINAL_EQUITY if valid else None,
           "equity": snapshot.total_assets if valid else None,
           "cash": snapshot.cash, "invested": snapshot.invested if valid else None,
           "holding_count": len(codes), "trading_days": len(days) if days else None,
           "benchmark_nav": benchmark_nav, "issues": issues,
           "fingerprint": _fingerprint(trades, asof)}
    state["records"] = sorted([r for r in state["records"] if r["date"] != asof] + [row],
                              key=lambda r: r["date"])
    if not dry_run:
        _save(ledger_path, state)
    return state


def render_summary(ledger_path=DEFAULT_LEDGER_PATH, *, today=None, label="组合", state=None):
    """日报只读摘要：不拉行情、不写记录；不完整日期保留并明确标注最近完整日期。"""
    today = today or date.today()
    lines = ["## 模拟成绩", "", f"**{label}**", ""]
    try:
        trades = load_trades(ledger_path)
        started = [t.date for t in trades if t.date <= today.isoformat()]
        if not started:
            return "\n".join(lines + ["尚未开始模拟记账，运行天数和收益暂缺。"])
        state = _load(ledger_path) if state is None else state
        start = min(started)
        lines.append(f"开始日期 {start}｜已运行 {(today - date.fromisoformat(start)).days + 1} 个自然日")
        lines.append("")
        if state and state["start_date"] != start:
            return "\n".join(lines + ["⚠️ 模拟起点已变化，历史成绩待重新核对。"])
        rows = [r for r in state.get("records", []) if r["date"] <= today.isoformat()]
        valid = [r for r in rows if _positive(r["nav"])
                 and r["fingerprint"] == _fingerprint(trades, r["date"])]
        if not valid:
            lines.append("收盘成绩尚未生成或估值不完整，累计收益、基准收益和仓位暂缺。")
        else:
            latest = valid[-1]
            nav, bnav = latest["nav"], latest["benchmark_nav"]
            bench = f"{(bnav - 1) * 100:+.2f}%" if _positive(bnav) else "—"
            excess = f"{(nav - bnav) * 100:+.2f} 个百分点" if _positive(bnav) else "—"
            lines += [f"**收盘截至 {latest['date']}**｜累计 {latest['trading_days'] or '—'} 个交易日", "",
                      f"**累计收益 {(nav - 1) * 100:+.2f}%**｜同期基准 {bench}｜超额收益 {excess}",
                      "",
                      f"仓位 {latest['invested'] / latest['equity'] * 100:.1f}%｜"
                      f"现金 {latest['cash'] / latest['equity'] * 100:.1f}%｜持仓 {latest['holding_count']} 只", "",
                      f"基准：{state['benchmark']['name']}；起点 {state['baseline_date']} 收盘。"]
            if rows and latest["date"] != rows[-1]["date"]:
                lines.append(f"⚠️ {rows[-1]['date']} 的成绩不完整，以上保留最近完整估值。")
        if rows:
            lines.extend(f"⚠️ {rows[-1]['date']}：{issue}" for issue in rows[-1]["issues"])
        lines.append("")
        if rows:
            if rows[-1]["fingerprint"] != _fingerprint(trades, rows[-1]["date"]):
                lines.append("⚠️ 台账补记后历史估值已失效，须重新采集对应日期。")
        lines.append("名义模拟口径；未计手续费、分红和份额变更，不代表实盘收益。")
    except Exception:
        logger.exception("模拟成绩读取失败")
        lines.append("⚠️ 模拟成绩暂不可用，本次信号与执行结果见下文。")
    return "\n".join(lines)


def execution_summary(before, after, *, dry_run=False, planned=0, blocked=0):
    """仅汇总实际新增流水，部分失败不计为成交；试运行单列计划数量。"""
    if dry_run:
        return f"**本次执行**：试运行，未记账；计划 {planned} 笔，受阻 {blocked} 笔。"
    recorded = after[len(before):]
    buys = sum(t.side == "buy" for t in recorded)
    sells = sum(t.side == "sell" for t in recorded)
    return f"**本次执行**：已记账 {len(recorded)} 笔（买入 {buys}，卖出 {sells}）；未完成 {max(blocked, planned - len(recorded))} 笔。"
