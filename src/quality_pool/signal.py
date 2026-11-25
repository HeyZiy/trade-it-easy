# -*- coding: utf-8 -*-
"""每天检查持有资格，有空位时排名补位。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

import pandas as pd

import src.trading_calendar as trading_calendar
from data_provider.security_names import names_for
from src.quality_pool import config, feeds, screener, state as pool_state

logger = logging.getLogger(__name__)


@dataclass
class SignalPlan:
    """一轮调仓名单的执行事实；持久化形状（to_dict/from_stored）归本类型所有。

    报告专用的 decisions 明细不进名单——生成侧渲染完即弃，执行侧不消费。
    """

    signal_day: str
    execute_day: str
    asof: str
    selected: list[str]
    deferred: list[str]
    industries: Dict[str, str]
    kept: list[str]
    exit_reasons: Dict[str, str]

    @property
    def entries(self) -> list[str]:
        kept = set(self.kept)
        return [code for code in self.selected if code not in kept]

    def merge_exit_queue(self, queue: dict) -> dict:
        """叠加本轮退出意图，撤销已恢复持有资格的退出。"""
        exits = dict(queue)
        exits.update(self.exit_reasons)
        return {c: r for c, r in exits.items() if c not in self.selected}

    def to_dict(self) -> dict:
        return {"signal_day": self.signal_day, "execute_day": self.execute_day,
                "asof": self.asof, "selected": self.selected,
                "deferred": self.deferred, "industries": self.industries,
                "kept": self.kept, "exit_reasons": self.exit_reasons}

    @classmethod
    def from_stored(cls, raw: Optional[dict], exec_date: str) -> "SignalPlan | None":
        """读回并校验持久化名单：版本/执行日/数据截止日任一不符返回 None（丢弃）。

        asof 校验要求名单的数据截止日 = 执行日的前一交易日（与生成侧口径一致）。
        """
        if raw is None:
            return None
        prev = trading_calendar.previous_trading_day(date.fromisoformat(exec_date))
        if (raw.get('strategy_version') != config.STRATEGY_VERSION
                or raw.get('execute_day') != exec_date
                or raw.get('asof') != prev.isoformat()):
            logger.warning("STALE_SIGNAL：计划版本/执行日/数据截止日不匹配，丢弃")
            return None
        return cls(signal_day=raw["signal_day"], execute_day=raw["execute_day"],
                   asof=raw["asof"], selected=list(raw.get("selected", [])),
                   deferred=list(raw.get("deferred", [])),
                   industries=dict(raw.get("industries", {})),
                   kept=list(raw.get("kept", [])),
                   exit_reasons=dict(raw.get("exit_reasons", {})))


def _fundamentals(codes: set[str], asof: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    ordered = sorted(codes)
    raw_close = feeds.raw_closes_at(ordered, asof)
    return feeds.fetch_fundamentals(ordered, asof, raw_closes=raw_close)


def run_signal(today: date, dry_run: bool = False,
               *, state: dict, held_codes: set[str], state_writer) -> str:
    """同日成功名单不因手动重试重建；失败在原日或下一交易日重算。"""
    day = today.isoformat()
    if state.get('last_signal_date') == day and not state.get('signal_retry'):
        return '- 今日名单已生成，不重复本轮。'

    asof = trading_calendar.previous_trading_day(today).isoformat()
    lines = [f"- 财务/估值/波动/强弱截至 T−1：{asof}"]
    state['signal_retry'] = True
    pool_state.put_plan(state, None)
    if not dry_run:
        state_writer(state)
    try:
        held = set(held_codes)
        universe = feeds.fetch_universe(asof)
        status = feeds.fetch_signal_status(sorted(universe | held), asof, day)
        if status.empty:
            raise RuntimeError('状态快照为空')
        audit = {c: {} for c in held}
        incumbents = held & universe
        passed = set(screener.build_pool(incumbents, _fundamentals(incumbents, asof),
                                         status, audit=audit))
    except Exception as exc:
        logger.exception("持仓资格检查失败")
        return "\n".join(lines + ["- 数据异常，暂停生成新名单。"])

    unknown = {c for c in held if audit[c]['pool_state'] == 'unknown'}
    exits = {c: config.EXIT_OUT_OF_POOL for c in held if audit[c]['pool_state'] == 'failed'}
    exits.update({c: reason for c, reason in state['exit_queue'].items() if c in unknown})
    # 无有效补位时：目标名单 = 留存名单（entry 分支会分别重建两者）。
    kept = sorted(held - exits.keys())
    selected = list(kept)
    ranked, scores, industries = [], {}, {}
    eligible = None
    entry_error = None
    if len(kept) < config.TOP_N:
        try:
            is_st = status.get('is_st', pd.Series(dtype='boolean'))
            non_st = is_st.index[is_st.eq(False).fillna(False)]
            candidates = (universe - held).intersection(non_st)
            fundamentals = feeds.fetch_fundamentals(sorted(candidates), asof,
                                                    min_profit_yoy=config.NP_YOY_MIN)
            quality_pool = passed | set(screener.build_pool(candidates, fundamentals, status))
            if quality_pool:
                closes = feeds.fetch_closes(sorted(quality_pool), asof, rows=config.SCORE_CLOSES)
                if (closes.empty or len(closes) != config.SCORE_CLOSES
                        or pd.Timestamp(closes.index[-1]).normalize() != pd.Timestamp(asof)):
                    raise RuntimeError('收盘矩阵未完整更新至上一交易日')
                ranked, scores = screener.rank_pool(sorted(quality_pool), closes)
            eligible = quality_pool
            if ranked:
                shortlist = ranked[:config.TOP_N]
                industries = feeds.fetch_industries(sorted(set(shortlist) | held), asof)
                selected, kept = screener.select_targets(
                    shortlist, held, passed, industries, unknown - exits.keys())
        except Exception as exc:
            logger.exception("新仓排序失败")
            entry_error = exc

    plan = SignalPlan(signal_day=day, execute_day=day, asof=asof, selected=selected,
                      deferred=sorted(unknown), industries=industries, kept=kept, exit_reasons=exits)
    pool_state.put_plan(state, plan.to_dict())
    state.update(last_signal_date=day, signal_retry=False, exit_queue=exits)
    if not dry_run:
        state_writer(state)

    rank = {c: i + 1 for i, c in enumerate(ranked)}
    decisions = []
    for code in sorted(held | set(selected)):
        # 优先级：退出 > 暂缓判断 > 留存 > 新开仓。
        if code in exits:
            action = 'exit'
        elif code in unknown:
            action = 'defer'
        else:
            action = 'keep' if code in held else 'entry'
        decisions.append(dict(code=code, action=action, rank=rank.get(code),
                              score=scores.get(code), exit_reason=exits.get(code, ''),
                              pool_reason=audit.get(code, {}).get('pool_reason', '')))
    lines.append(f"- 检查 {len(held)} 只 | 留存 {len(kept)} | 目标 {len(selected)} | "
                 f"计划退出 {len(exits)} | 暂缓判断 {len(unknown)}")
    if entry_error is not None:
        lines.append(f"- ⚠️ 补位数据异常，暂停新买入：{entry_error}")
    names = names_for(held | set(selected) | (eligible or set()))
    lines += render_decision_table(decisions, names)
    if eligible is not None:
        # 新候选与旧仓的财务字段均来自本轮已经完成的资格检查。
        metrics = fundamentals.loc[fundamentals.index.isin(eligible)].to_dict("index")
        metrics.update({code: audit[code] for code in passed})
        lines += render_pool_table(eligible, ranked, scores, names, held, set(selected), metrics)
    elif entry_error is None:
        lines.append("- 持仓名额已满，本轮仅检查持有资格，未重算全池排名。")
    return "\n".join(lines)


def _stock_label(code, names):
    name = " ".join(names.get(code, "").split()).replace("|", "&#124;")
    return f"{name}（{code}）" if name and name != code else code


def render_decision_table(decisions: list[dict], names: dict[str, str]) -> list[str]:
    """按排名列出本轮持仓与交易决定。"""
    # 展示按排名升序（未知排名的退出/暂缓置底）；执行计划仍按原 decisions 提交。
    ordered = sorted(decisions,
                     key=lambda d: (d["rank"] is None, d["rank"] or 0, d["code"]))
    if not ordered:
        return []
    actions = {"keep": "留存", "entry": "拟补位", "exit": "拟退出", "defer": "暂缓判断"}
    reasons = {
        "not_in_mainboard_universe": "不在主板范围", "roe_low": "ROE 未达标",
        "profit_growth_low": "净利同比未达标", "pe_high": "PE 达到上限",
        "pb_high": "PB 达到上限", "pe_nonpos": "PE 非正", "pb_nonpos": "PB 非正",
        "financial_missing": "财务数据缺失", "valuation_invalid": "估值数据缺失",
        "status_missing": "证券状态缺失", "st": "ST", "passed": "质量筛选通过",
        "out_of_pool": "退出质量池",
    }
    lines = ["", "### 本轮持仓与补位", "", "| 标的 | 决定 | 排名 | 依据 |", "| --- | --- | ---: | --- |"]
    for d in ordered:
        basis = d["pool_reason"] or d["exit_reason"]
        note = "、".join(reasons.get(k, k) for k in basis.split("|")) if basis else "进入补位名单"
        lines.append(f"| {_stock_label(d['code'], names)} | {actions[d['action']]} | {d['rank'] or '—'} | {note} |")
    return lines


POOL_HEADING = "### 质量池全表"


def render_pool_table(eligible, ranked, scores, names, held, selected, metrics):
    unranked = sorted(eligible.difference(ranked))
    lines = ["", POOL_HEADING, "",
             f"通过质量筛选 {len(eligible)} 只，参与动量排名 {len(ranked)} 只。"
             f"前 {config.TOP_N} 名可参与补位。"]
    if unranked:
        lines.append(f"另有 {len(unranked)} 只因波动率筛选或价格历史不足未参与排名。")
    if eligible:
        lines += ["", "| 排名 | 标的 | 动量收益 | 本轮状态 | ROE（TTM） | 净利同比 | PE（TTM） | PB |",
                  "| ---: | --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for i, code in enumerate([*ranked, *unranked], 1):
        position = str(i) if i <= len(ranked) else "—"
        score = f"{scores[code]:+.2%}" if code in scores else "—"
        if code in held:
            status = "留存"
        elif code in selected:
            status = "拟补位"
        elif code not in scores:
            status = "未参与排名"
        elif i <= config.TOP_N:
            status = "补位范围内·未入选"
        else:
            status = "观察"
        values = metrics[code]
        lines.append(f"| {position} | {_stock_label(code, names)} | {score} | {status} | "
                     f"{float(values['roe_ttm_pct']):.1f}% | {float(values['np_yoy_pct']):+.1f}% | "
                     f"{float(values['pe_ttm']):.2f} | {float(values['pb']):.2f} |")
    return lines
