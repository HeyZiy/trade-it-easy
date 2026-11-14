# -*- coding: utf-8 -*-
"""场外基金定投：把基金 App 的周期扣款自动补记进投资账户。

机制：
- 真实扣款永远发生在基金 App；本模块只在其后补记账，不下单、不扣款。
- 时间线：T 日 App 扣款 → T+1 08:30 盘前任务先确认更早的在途申购、刷新净值，
  再把 T 日（及回看窗口内漏掉的）扣款补记为 fund_pending（nav_date=T）→
  当天可在 dashboard「申购作废」（fund_void 补偿流水）→ T+2 08:30 由
  confirm_fund_subscriptions 按 T 日公布净值确认份额。
- 幂等：request_id = "dca-{plan_id}-{due}"，同一次扣款只补记一次；重跑、
  补跑都安全。先扫描账本跳过已存在的 request_id，不依赖重试内容比对。
- 回看窗口：每次运行回看 CATCHUP_WINDOW_DAYS 个自然日内的到期日（不含当天，
  当天扣款由下次运行补记）；错过多天也能逐日补上，nav_date 各归各位。
- 优先使用账户统一申购费用；未设置时沿用该基金最近登记的费用，两者都缺失则跳过并告警。
  多资产配置现金不足时该笔记账失败并告警，不影响其他计划；
  交易日历不可用时不猜测扣款日，整体跳过。
- 待确认申购超过 AGING_TRADING_DAYS 个交易日未确认份额 → 告警（多为净值未公布）。

计划存放在账本同目录的 fund_plans.json，是配置而非账本事实：可随时修改、
停用、删除；已补记的流水不受影响，记错了走「申购作废」补偿流水。计划结构
由 save_plan 校验生成，手工编辑需保持字段完整。
"""

import json
import logging
import re
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from src.account_replay import fund_code, positive
from src.etf_account import (
    load as load_account,
    record_event,
    revision,
    snapshot,
    subscription_fees,
)
from src.json_store import locked as _locked, save as _json_save
from src.trading_calendar import get_trading_dates, trading_days_lag
from src.trading_calendar import shanghai_today as _today

logger = logging.getLogger(__name__)

PLANS_FILENAME = "fund_plans.json"
DCA_REQUEST_PREFIX = "dca-"
FREQUENCIES = ("monthly", "weekly")
CATCHUP_WINDOW_DAYS = 14   # 补记回看自然日数：覆盖春节长假与漏跑
POSTPONE_MAX_DAYS = 10     # 扣款日休市时顺延查找下一交易日的最大跨度
AGING_TRADING_DAYS = 5     # 待确认超过该交易日数未确认份额即告警

_PLAN_ID_RE = re.compile(r"[0-9a-f]{32}")


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
        raise ValueError("扣款周期只支持 monthly / weekly")
    anchor = payload.get("anchor")
    if type(anchor) is not int or not 1 <= anchor <= (31 if frequency == "monthly" else 5):
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
    return {"id": plan_id, "code": code, "amount": float(amount), "frequency": frequency,
            "anchor": anchor, "enabled": enabled, "note": note}


def load_plans(account_path):
    """全部定投计划；从未创建过计划（文件不存在）返回空配置。"""
    return _read_plans(_plans_path(account_path))


def save_plan(account_path, payload):
    """新增或更新一条定投计划。计划是配置：改动不影响已补记的流水。"""
    plan = _validated_plan(payload)
    path = _plans_path(account_path)
    with _locked(path):
        state = _read_plans(path)
        plans = state["plans"]
        if plan["id"]:
            index = next((i for i, row in enumerate(plans) if row.get("id") == plan["id"]), None)
            if index is None:
                raise ValueError("定投计划不存在，可能已被删除，请刷新")
            plan["created_on"] = plans[index].get("created_on") or _today().isoformat()
            plans[index] = plan
        else:
            plan["id"] = uuid4().hex
            plan["created_on"] = _today().isoformat()
            plans.append(plan)
        _json_save(path, state)
    logger.info("[定投] 计划已保存：%s %s 每期 %.2f 元", plan["code"], plan["frequency"], plan["amount"])
    return plan


def delete_plan(account_path, payload):
    """删除定投计划。只删配置；已补记的流水保留，错误记账走「申购作废」。"""
    plan_id = str((payload or {}).get("id") or "")
    if not plan_id:
        raise ValueError("缺少定投计划标识")
    path = _plans_path(account_path)
    with _locked(path):
        state = _read_plans(path)
        remaining = [row for row in state["plans"] if row.get("id") != plan_id]
        if len(remaining) < len(state["plans"]):
            state["plans"] = remaining
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
    """[window_start, window_end] 内的到期扣款日：剔除建计划前、休市顺延、去重升序。"""
    created = _created_on(plan)
    dues = []
    for base in _base_dates(plan, window_start, window_end):
        if base < created:
            continue
        due = _postponed(base)
        if due is not None and window_start <= due <= window_end:
            dues.append(due)
    return sorted(set(dues))


def _warn_aging_pendings(state, run_day):
    """在途申购迟迟未确认份额（净值未公布等）时逐日告警。"""
    for pending in snapshot(state, run_day.isoformat())["fund_pending"].values():
        lag = trading_days_lag(date.fromisoformat(pending["nav_date"]), run_day)
        if lag is not None and lag > AGING_TRADING_DAYS:
            logger.warning("[定投] %s %s 扣款已 %d 个交易日未确认份额，请检查该日净值是否公布",
                           pending["code"], pending["nav_date"], lag)


def record_due_subscriptions(account_path):
    """盘前任务入口：把回看窗口内漏记的扣款日补记为待确认申购。

    无计划（文件不存在或为空）时静默返回 []，不触碰账本与交易日历。
    """
    plans = load_plans(account_path)["plans"]
    if not plans:
        return []
    run_day = _today()
    window_end = run_day - timedelta(days=1)  # 当天扣款由下次运行补记
    window_start = run_day - timedelta(days=CATCHUP_WINDOW_DAYS)
    state = load_account(account_path)
    known_ids = {event["id"] for event in state["events"]}
    due_plans = sorted(
        ((due, plan) for plan in plans if plan.get("enabled", True)
         for due in _due_dates(plan, window_start, window_end)),
        key=lambda pair: pair[0])
    recorded = []
    for due, plan in due_plans:
        request_id = f"{DCA_REQUEST_PREFIX}{plan['id']}-{due.isoformat()}"
        if request_id in known_ids:
            continue  # 已补记过：幂等跳过
        payload = {
            "request_id": request_id,
            "revision": revision(state),
            "side": "fund_pending",
            "code": plan["code"],
            "amount": plan["amount"],
            "date": run_day.isoformat(),
            "nav_date": due.isoformat(),
        }
        if "fund_subscription_fee" not in state["_config"]:
            fee = subscription_fees(state).get(plan["code"])
            if fee is None:
                logger.warning("[定投] %s 未登记申购费率，跳过 %s 扣款记账；请设置账户统一申购费或登记该基金的费用",
                               plan["code"], due)
                continue
            payload["subscription_fee"] = fee
        try:
            event = record_event(account_path, payload)
        except ValueError as error:
            logger.warning("[定投] %s %s 扣款 %.2f 元补记失败：%s",
                           plan["code"], due, plan["amount"], error)
            continue
        state = load_account(account_path)  # 记账成功后重读，保证下一笔 revision 有效
        recorded.append(event)
        logger.info("[定投] 已补记 %s %s 扣款 %.2f 元（净值日 %s）",
                    plan["code"], due, plan["amount"], due)
    _warn_aging_pendings(state, run_day)
    return recorded


def dca_status(account_path):
    """dashboard 账户页读取入口：定投计划与今日自动补记的净值日；失败不阻塞账户页。"""
    try:
        plans = load_plans(account_path)["plans"]
    except Exception:
        logger.warning("定投计划读取失败，页面隐藏计划", exc_info=True)
        plans = []
    today = _today().isoformat()
    recorded_today = [event["nav_date"] for event in load_account(account_path)["events"]
                      if event["date"] == today and event["id"].startswith(DCA_REQUEST_PREFIX)]
    return {"plans": plans, "recorded_today": recorded_today}
