# -*- coding: utf-8 -*-
"""登记基金 App 定投扣款。收盘任务登记当期，历史漏记由用户逐期核对。

自动和手动登记共用计划 ID 与扣款日生成的流水标识，防止同一期重复扣减账面现金。
"""

import json
import logging
import re
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from src.account_replay import display_event, fund_code, fund_strategy, positive
from src.account_store import deleted_subscription_events
from src.etf_account import (
    load as load_account,
    record_event,
    revision,
    snapshot,
    subscription_fees,
)
from src.json_store import locked as _locked, save as _json_save
from src.trading_accounts import UNKNOWN
from src.trading_calendar import get_trading_dates, trading_days_lag
from src.trading_calendar import shanghai_today as _today

logger = logging.getLogger(__name__)

PLANS_FILENAME = "fund_plans.json"
DCA_REQUEST_PREFIX = "dca-"
FREQUENCIES = ("monthly", "weekly", "daily")
HISTORY_DAYS = 60
PLAN_TERMS = ("code", "strategy", "amount", "frequency", "anchor", "trading_account")
POSTPONE_MAX_DAYS = 10     # 扣款日休市时顺延查找下一交易日的最大跨度
AGING_TRADING_DAYS = 5     # 待确认超过该交易日数未确认份额即告警
NEXT_DUE_WINDOW_DAYS = 31 + POSTPONE_MAX_DAYS

_PLAN_ID_RE = re.compile(r"[0-9a-f]{32}")
_DCA_EVENT_RE = re.compile(rf"{DCA_REQUEST_PREFIX}({_PLAN_ID_RE.pattern})-(\d{{4}}-\d{{2}}-\d{{2}})(?:-r([1-9]\d*))?")


def _plans_path(account_path):
    """定投计划与账本同目录存放，便于随账本一起备份/迁移。"""
    return Path(account_path).parent / PLANS_FILENAME


def _read_plans(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"plans": []}
    if not isinstance(state, dict) or not isinstance(state.get("plans"), list):
        raise ValueError("定投计划文件损坏，请修复或删除 fund_plans.json")
    return state


def _validated_plan(payload):
    if not isinstance(payload, dict):
        raise ValueError("定投计划参数缺失")
    code = fund_code(str(payload.get("code") or "").strip())
    amount = payload.get("amount")
    if not positive(amount) or round(amount, 2) != amount:
        raise ValueError("每期金额须为正数，最多两位小数")
    frequency = payload.get("frequency")
    if frequency not in FREQUENCIES:
        raise ValueError("扣款周期只支持每日、每周或每月")
    anchor = payload.get("anchor")
    if frequency == "daily":
        anchor = None
    elif type(anchor) is not int or not 1 <= anchor <= (31 if frequency == "monthly" else 5):
        raise ValueError("月定投扣款日须为 1-31，周定投须为周一到周五")
    note = str(payload.get("note") or "").strip()
    if len(note) > 50:
        raise ValueError("备注最多 50 字")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("启用状态须为 true 或 false")
    plan_id = str(payload.get("id") or "").strip()
    if plan_id and not _PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("定投计划标识无效")
    return {"id": plan_id, "code": code, "strategy": fund_strategy(payload),
            "amount": float(amount), "frequency": frequency,
            "anchor": anchor, "enabled": enabled, "note": note}


def load_plans(account_path):
    """全部定投计划；从未创建过计划（文件不存在）返回空配置。"""
    return _read_plans(_plans_path(account_path))


def save_plan(account_path, payload):
    """从条款变更日开始用新条款推算计划期次。"""
    plan = _validated_plan(payload)
    if payload.get("trading_account"):
        settings = load_account(account_path)["_config"].get("trading_accounts", {})
        account = payload["trading_account"]
        if not isinstance(account, str) or settings.get("accounts", {}).get(account, {}).get("channel") != "fund":
            raise ValueError("请选择有效的场外扣款账户")
        plan["trading_account"] = account
    path = _plans_path(account_path)
    with _locked(path):
        state = _read_plans(path)
        plans = state["plans"]
        if plan["id"]:
            index = next((i for i, row in enumerate(plans) if row.get("id") == plan["id"]), None)
            if index is None:
                raise ValueError("定投计划不存在，可能已被删除，请刷新")
            previous = plans[index]
            if "trading_account" not in payload and "trading_account" in previous:
                plan["trading_account"] = previous["trading_account"]
            if "strategy" not in payload:
                plan["strategy"] = fund_strategy(previous)
            plan["created_on"] = previous.get("created_on") or _today().isoformat()
            if (any(plan.get(key) != previous.get(key) for key in PLAN_TERMS)
                    or (plan["enabled"] and not previous.get("enabled", True))):
                plan["effective_on"] = _today().isoformat()
            elif "effective_on" in previous:
                plan["effective_on"] = previous["effective_on"]
            plans[index] = plan
        else:
            plan["id"] = uuid4().hex
            plan["created_on"] = _today().isoformat()
            plans.append(plan)
        _json_save(path, state)
    logger.info("[定投] 计划已保存：%s %s 每期 %.2f 元", plan["code"], plan["frequency"], plan["amount"])
    return plan


def delete_plan(account_path, payload):
    """删除定投计划配置。"""
    plan_id = str((payload or {}).get("id") or "")
    if not plan_id:
        raise ValueError("缺少定投计划标识")
    path = _plans_path(account_path)
    with _locked(path):
        state = _read_plans(path)
        remaining = [row for row in state["plans"] if row.get("id") != plan_id]
        if len(remaining) < len(state["plans"]):
            state["plans"] = remaining
            state.get("failures", {}).pop(plan_id, None)
            _json_save(path, state)
            logger.info("[定投] 计划已删除：%s", plan_id)


def _postponed(day):
    """扣款日休市则顺延到下一交易日；日历不可用返回 None（fail-closed 不记账）。"""
    days = get_trading_dates(day, day + timedelta(days=POSTPONE_MAX_DAYS))
    return days[0] if days else None


def _created_on(plan):
    try:
        return date.fromisoformat(plan["created_on"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("定投计划缺少有效的 created_on，请删除后重建该计划") from None


def _base_dates(plan, window_start, window_end):
    """窗口前后（多看一段）的原始扣款日：月末收缩到当月最后一天，周扣款按锚点星期。"""
    if plan["frequency"] == "monthly":
        first = window_start - timedelta(days=31)  # 多看一个月，覆盖跨月顺延
        year, month = first.year, first.month
        while (year, month) <= (window_end.year, window_end.month):
            yield date(year, month, min(plan["anchor"], monthrange(year, month)[1]))
            month += 1
            if month == 13:
                year, month = year + 1, 1
    else:
        monday = window_start - timedelta(days=window_start.weekday() + 7)
        while monday <= window_end:
            yield monday + timedelta(days=plan["anchor"] - 1)
            monday += timedelta(days=7)


def _due_dates(plan, window_start, window_end):
    """每日计划按交易日，周/月计划遇休市顺延；仅计算生效后的到期日。"""
    effective = _created_on(plan)
    if "effective_on" in plan:
        effective = date.fromisoformat(plan["effective_on"])
    if plan["frequency"] == "daily":
        start = max(window_start, effective)
        return get_trading_dates(start, window_end) if start <= window_end else []
    dues = []
    for base in _base_dates(plan, window_start, window_end):
        if base < effective:
            continue
        due = _postponed(base)
        if due is not None and window_start <= due <= window_end:
            dues.append(due)
    return sorted(set(dues))


def _request_id(plan, due):
    return f"{DCA_REQUEST_PREFIX}{plan['id']}-{due.isoformat()}"


def _restoration_terms(event):
    return {"code": event["code"], "strategy": fund_strategy(event), "amount": event["amount"],
            "trading_account": event.get("request", {}).get("trading_account")}


def _next_due(plan, today, known_ids):
    if not plan.get("enabled", True):
        return None
    dues = _due_dates(plan, today, today + timedelta(days=NEXT_DUE_WINDOW_DAYS))
    return next((due.isoformat() for due in dues if _request_id(plan, due) not in known_ids), None)


def _warn_aging_pendings(state, run_day):
    """在途申购迟迟未确认份额（净值未公布等）时逐日告警。"""
    for pending in snapshot(state, run_day.isoformat())["fund_pending"].values():
        lag = trading_days_lag(date.fromisoformat(pending["nav_date"]), run_day)
        if lag is not None and lag > AGING_TRADING_DAYS:
            logger.warning("[定投] %s %s 扣款已 %d 个交易日未确认份额，请检查该日净值是否公布",
                           pending["code"], pending["nav_date"], lag)


def _save_failures(account_path, failures):
    path = _plans_path(account_path)
    with _locked(path):
        state = _read_plans(path)
        stored = state.setdefault("failures", {})
        active_ids = {plan["id"] for plan in state["plans"]}
        changed = False
        for plan_id in active_ids.intersection(failures):
            failure = failures[plan_id]
            previous = stored.get(plan_id)
            newer = previous is None or (failure["due_date"], failure["date"]) >= (previous["due_date"], previous["date"])
            if newer and failure != previous:
                stored[plan_id] = failure
                changed = True
        if changed:
            _json_save(path, state)


def _subscription_payload(plan, due, run_day, account_revision):
    payload = {
        "request_id": _request_id(plan, due),
        "revision": account_revision,
        "side": "fund_pending",
        "strategy": fund_strategy(plan),
        "code": plan["code"],
        "amount": plan["amount"],
        "date": run_day.isoformat(),
        "nav_date": due.isoformat(),
    }
    if plan.get("trading_account"):
        payload["trading_account"] = plan["trading_account"]
    return payload


def _manual_subscription_key(row):
    return (row.get("nav_date") or row["date"], row["code"], row.get("strategy", "core"), row["amount"])


def _unlinked_subscriptions(events, voided_ids):
    matches = {}
    for event in events:
        if (event["side"] in {"fund_pending", "fund_buy"}
                and event["id"] not in voided_ids and not _DCA_EVENT_RE.fullmatch(event["id"])):
            key = _manual_subscription_key(display_event(event))
            matches.setdefault(key, set()).add(event.get("trading_account", UNKNOWN))
    return matches


def _has_matching_manual(unlinked, plan, due, settings):
    key = (due, plan["code"], fund_strategy(plan), plan["amount"])
    accounts = unlinked.get(key, ())
    account = plan.get("trading_account")
    if not account and settings:
        account = settings["products"].get(f"fund:{plan['code']}", settings["defaults"]["fund"])
    return account in accounts or UNKNOWN in accounts


def record_due_subscriptions(account_path, *, run_day=None):
    """登记当天到期的定投申购。"""
    plans = load_plans(account_path)["plans"]
    if not plans:
        return []
    run_day = run_day or _today()
    state = load_account(account_path)
    events = state["events"]
    known_ids = {event["id"] for event in events} | deleted_subscription_events(state).keys()
    unlinked = _unlinked_subscriptions(events, {event["voided_pending_id"] for event in events
                                                if event["side"] == "fund_void"})
    recorded, failures = [], {}
    for plan in plans:
        if not plan.get("enabled", True) or run_day not in _due_dates(plan, run_day, run_day):
            continue
        if _request_id(plan, run_day) in known_ids:
            continue
        payload = _subscription_payload(plan, run_day, run_day, revision(state))
        if _has_matching_manual(unlinked, plan, run_day.isoformat(), state["_config"].get("trading_accounts")):
            continue
        try:
            if "fund_subscription_fee" not in state["_config"]:
                fee = subscription_fees(state).get(plan["code"])
                if fee is None:
                    raise ValueError("未登记申购费率，请设置账户统一申购费或登记该基金的费用")
                payload["subscription_fee"] = fee
            event = record_event(account_path, payload)
        except ValueError as error:
            failures[plan["id"]] = {"status": "failed", "due_date": run_day.isoformat(),
                                    "date": run_day.isoformat(), "reason": str(error)}
            logger.warning("[定投] %s %s 扣款 %.2f 元记账失败：%s",
                           plan["code"], run_day, plan["amount"], error)
            continue
        state = load_account(account_path)  # 记账成功后重读，保证下一笔 revision 有效
        recorded.append(event)
        logger.info("[定投] 已登记 %s %s 扣款 %.2f 元", plan["code"], run_day, plan["amount"])
    if failures:
        _save_failures(account_path, failures)
    _warn_aging_pendings(state, run_day)
    return recorded


def record_installment(account_path, payload):
    """按用户确认的扣款金额登记指定期次。"""
    today = _today()
    date_text = payload.get("due_date")
    try:
        due = date.fromisoformat(date_text)
    except (TypeError, ValueError):
        raise ValueError("扣款日无效") from None
    if due.isoformat() != date_text:
        raise ValueError("扣款日无效")
    plan_id = payload.get("id")
    manual_id = payload.get("request_id")
    if not isinstance(manual_id, str) or not 8 <= len(manual_id) <= 100:
        raise ValueError("缺少有效的补记标识")
    plans_path = _plans_path(account_path)
    with _locked(plans_path):
        state = load_account(account_path)
        plan = next((row for row in _read_plans(plans_path)["plans"] if row["id"] == plan_id), None)
        if plan is None:
            raise ValueError("定投计划已更新，请刷新后核对")
        base_id = _request_id(plan, due)
        deleted = deleted_subscription_events(state)
        voided = {row["voided_pending_id"] for row in state["events"] if row["side"] == "fund_void"}
        attempts = {}
        for row in (*state["events"], *deleted.values()):
            match = _DCA_EVENT_RE.fullmatch(row["id"])
            if match and match.group(1, 2) == (plan_id, date_text):
                attempts[int(match[3] or 0)] = row
        attempt = max(attempts, default=-1)
        existing = attempts.get(attempt)
        if existing and existing["id"] not in deleted and existing["id"] not in voided:
            if existing.get("request", {}).get("dca_manual") == manual_id:
                return existing
            raise ValueError("该期已有流水，请刷新定投记录核对")
        if existing and any(row.get("request", {}).get("dca_manual") == manual_id for row in attempts.values()):
            raise ValueError("该次补记已撤销或删除，请重新发起补记")
        terms = {**plan, **_restoration_terms(existing)} if existing else plan
        start = max(today - timedelta(days=HISTORY_DAYS - 1), date.fromisoformat(state["initial"]["date"]))
        if (not start <= due <= today
                or (existing is None and (not plan.get("enabled", True) or due not in _due_dates(plan, due, due)))
                or (payload.get("code"), payload.get("strategy"), payload.get("plan_amount"), payload.get("plan_account"))
                != (terms["code"], fund_strategy(terms), terms["amount"], terms.get("trading_account"))):
            raise ValueError("该期不在当前计划内，请刷新后核对")
        entry = _subscription_payload(terms, due, today, payload.get("revision"))
        if existing:
            entry["request_id"] = f"{base_id}-r{attempt + 1}"
        entry.update(amount=payload.get("amount"), dca_manual=manual_id)
        if not terms.get("trading_account") and payload.get("trading_account"):
            entry["trading_account"] = payload["trading_account"]
        if payload.get("subscription_fee") is not None:
            entry["subscription_fee"] = payload["subscription_fee"]
        return record_event(account_path, entry)


def dca_status(account_path):
    """从交易日历推算期次，从账本读取登记来源和确认状态。"""
    try:
        state = load_plans(account_path)
        plans = state["plans"]
        latest = dict(state.get("failures", {}))
    except Exception:
        logger.warning("定投计划读取失败，页面隐藏计划", exc_info=True)
        plans = []
        latest = {}
    today = _today()
    today_iso = today.isoformat()
    recorded_today, by_strategy = [], {}
    account = load_account(account_path)
    events = account["events"]
    start = max(today - timedelta(days=HISTORY_DAYS - 1), date.fromisoformat(account["initial"]["date"]))
    deleted = deleted_subscription_events(account)
    known_ids = {event["id"] for event in events} | deleted.keys()
    confirmed = {event["pending_id"] for event in events if event["side"] == "fund_confirm"}
    voided = {event["voided_pending_id"] for event in events if event["side"] == "fund_void"}
    unlinked = _unlinked_subscriptions(events, voided)
    installments, sources = {}, {}
    attempts = []
    for event in (*events, *deleted.values()):
        match = _DCA_EVENT_RE.fullmatch(event["id"])
        if event["side"] == "fund_pending" and match:
            attempts.append((match[1], match[2], int(match[3] or 0), event))
    for plan_id, due_date, _, event in sorted(attempts, key=lambda row: row[:3]):
        nav_date = event["nav_date"]
        source = ("manual" if event.get("request", {}).get("dca_manual")
                  else "automatic" if event["date"] == due_date else "automatic_backfill")
        is_deleted = event["id"] in deleted
        if not is_deleted:
            sources[event["id"]] = source
        if start.isoformat() <= due_date <= today_iso:
            status = ("deleted" if is_deleted else "voided" if event["id"] in voided
                      else "confirmed" if event["id"] in confirmed else "pending")
            installments.setdefault(plan_id, {})[due_date] = {
                "due_date": due_date, "status": status, "source": source,
                "date": event["date"], "amount": event["amount"], "code": event["code"],
            }
            if status in {"deleted", "voided"}:
                terms = _restoration_terms(event)
                installments[plan_id][due_date].update(
                    restoration_terms=terms,
                    possible_manual=_has_matching_manual(unlinked, terms, due_date,
                                                         account["_config"].get("trading_accounts")),
                )
        previous = latest.get(plan_id)
        if previous is None or due_date >= previous["due_date"]:
            latest[plan_id] = {"status": "deleted" if is_deleted else "recorded",
                               "due_date": due_date, "date": event["date"]}
        if not is_deleted and event["date"] == today_iso and source != "manual" and event["id"] not in voided:
            recorded_today.append(nav_date)
            by_strategy.setdefault(event.get("strategy", "core"), []).append(nav_date)
    views = []
    for plan in plans:
        rows = installments.get(plan["id"], {})
        try:
            next_due = _next_due(plan, today, known_ids)
            if plan.get("enabled", True) and start <= today:
                for due in _due_dates(plan, start, today):
                    day = due.isoformat()
                    if day in rows:
                        continue
                    failure = latest.get(plan["id"], {})
                    rows[day] = {
                        "due_date": day, "status": "missing", "amount": plan["amount"], "code": plan["code"],
                        "possible_manual": _has_matching_manual(unlinked, plan, day, account["_config"].get("trading_accounts")),
                        "reason": failure.get("reason") if failure.get("due_date") == day else None,
                    }
        except (KeyError, TypeError, ValueError):
            logger.warning("[定投] 无法计算计划 %s 的下次扣款日", plan.get("id"), exc_info=True)
            next_due = None
        views.append({**plan, "next_due": next_due, "latest_result": latest.get(plan["id"]),
                      "installments": [rows[day] for day in sorted(rows, reverse=True)]})
    return {"plans": views, "recorded_today": recorded_today, "recorded_today_by_strategy": by_strategy,
            "sources": sources, "history_days": HISTORY_DAYS}
