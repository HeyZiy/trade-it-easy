# -*- coding: utf-8 -*-
"""
环境快照 — 落盘与类型化视图（Environment Store）。

data/environment.json 的 schema 单点：环境判定任务（env_report.py）每交易日以同一份
指数数据、同一判定时点产出 gate_state verdict，本模块负责读写；消费方经
load_gate_verdict 读 GateRead（view+issue 成对），不再各自拼装判定。
当前无运行中的消费方（唯一消费方趋势双任务已退役，2026-10）。

分层约定：
- 本仓库只存**标签与判定事实**（gate 5 态标签），不存 policy——
  哪些标签允许开仓/清仓是策略层的事。
"""
import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

ENVIRONMENT_FILE = Path(__file__).parent.parent.parent / "data" / "environment.json"

SNAPSHOT_MAX_AGE_DAYS = 3   # 视图层默认过期阈值（自然日，覆盖周末顺延）；开仓语义归消费方 policy

# gate 5 态的展示名（emoji+人话，视图自描述产出；消费方报告头条用）。
# 与 market_gate.GATE_STATE_PATHS 键集合相等的断言见 tests。
GATE_STATE_LABELS = {
    "trending_up":   "📈 均线多头（过去式）— MA5>MA10>MA20",
    "weak_up":       "🌤️ 弱上行 — 站上 MA20 但非标准多头排列",
    "sideways":      "➡️ 震荡横盘 — 紧贴 MA20 震荡",
    "trending_down": "📉 均线空头（过去式）— MA5<MA10<MA20",
    "chaos":         "🌪️ 混沌 — 方向不明",
}


@dataclass(frozen=True)
class GateVerdictView:
    """gate_state verdict 的类型化视图（market_gate.GateDiagnosis 的落盘投影）。"""
    state: Optional[str]             # 5 态标签；available=False 时也为标签值（如 chaos），语义归消费方
    available: bool                  # 指数数据是否足以判定（False = 数据不足/取数失败）
    data_date: Optional[str]         # 判定所用数据的最新交易日
    alignment: str
    bias_ma20: Optional[float]
    note: str
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    close: Optional[float] = None
    path: str = ""                   # 命中的判定路径文案

    @property
    def age_days(self) -> Optional[int]:
        if not self.data_date:
            return None
        try:
            return (date.today() - date.fromisoformat(str(self.data_date))).days
        except ValueError:
            return None

    def is_stale(self, max_age: int = SNAPSHOT_MAX_AGE_DAYS) -> bool:
        return self.age_days is None or self.age_days > max_age

    @property
    def state_label(self) -> str:
        """状态展示名（视图自描述；未知标签回落兜底文案，不静默吞）。"""
        return GATE_STATE_LABELS.get(self.state, "❓ 状态不明")

    def describe(self) -> str:
        """人类可读的诊断行（与 market_gate.GateDiagnosis.describe 同构）。"""
        date_part = f"｜数据日期 {self.data_date}" if self.data_date else ""
        if not self.available:
            return f"状态={self.state}｜均线数据不足，无法给出排列与偏离（{self.note}）{date_part}"
        return (
            f"状态={self.state}｜{self.alignment}｜"
            f"收盘{self.close} 偏离MA20 {self.bias_ma20:+.2f}%｜命中：{self.path}{date_part}"
        )


def gate_verdict_issue(view: Optional[GateVerdictView]) -> Optional[str]:
    """消费方新鲜度谓词单点：verdict 可用且不过期返回 None，否则返回不可用原因。

    回答"这个 verdict 能不能消费"；读到不可用 verdict 后 fail-closed 还是
    fail-soft 是各消费方的 policy，不在本谓词。
    """
    if view is None:
        return "环境快照缺失（env_report 未运行或写入失败）"
    if not view.available:
        return f"快照判定不可用（{view.note or '指数数据不足'}）"
    if view.is_stale():
        return f"快照已 {view.age_days} 天未更新（阈值 {SNAPSHOT_MAX_AGE_DAYS} 天）"
    return None


@dataclass(frozen=True)
class EnvironmentView:
    """data/environment.json 的类型化视图（schema 漂移在此显式失败）。"""
    data_date: Optional[str]
    gate_state: Optional[GateVerdictView]


class GateRead(NamedTuple):
    """gate verdict 的一次读取结果：view 与可消费性结论成对返回。

    防止调用方各自拼装 load_environment + gate_verdict_issue 时漏判 issue；
    读到不可用 verdict 后的保护性 fallback 是消费方 policy，经 effective_state 注入。
    """
    view: Optional[GateVerdictView]
    issue: Optional[str]

    def effective_state(self, fallback: str) -> str:
        """消费用 gate 标签：verdict 可用返回 view.state，否则返回调用方的保护性 fallback。"""
        return self.view.state if self.issue is None else fallback


def save_environment(gate_diag, path: Path = ENVIRONMENT_FILE) -> None:
    """环境判定任务产出落盘（fail-soft，写入失败仅告警——消费方按过期/缺失路径处理）。

    gate_diag: market_gate.GateDiagnosis（asdict 序列化，schema 由其 owner 定义）
    """
    try:
        payload = {
            "data_date": gate_diag.data_date or None,
            "gate_state": asdict(gate_diag),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"environment 快照写入失败: {e}")


def load_environment(path: Path = ENVIRONMENT_FILE) -> Optional[EnvironmentView]:
    """读当前环境快照；文件缺失 / 解析失败返回 None（fail-soft，调用方自判）。"""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        reg = data.get("gate_state") or {}
        return EnvironmentView(
            data_date=data.get("data_date"),
            gate_state=GateVerdictView(
                state=reg.get("gate_state"),
                available=bool(reg.get("ma20") is not None and reg.get("close") is not None),
                data_date=reg.get("data_date") or None,
                alignment=str(reg.get("alignment") or ""),
                bias_ma20=reg.get("bias_ma20"),
                note=str(reg.get("note") or ""),
                ma5=reg.get("ma5"),
                ma10=reg.get("ma10"),
                ma20=reg.get("ma20"),
                close=reg.get("close"),
                path=str(reg.get("path") or ""),
            ),
        )
    except Exception as e:
        logger.warning(f"environment 快照读取失败: {e}")
        return None


def load_gate_verdict(path: Path = ENVIRONMENT_FILE) -> GateRead:
    """读环境快照中的 gate verdict（消费方读取单点）：view 与 issue 成对返回。

    fail-closed / fail-soft、日志文案、chaos 等保护性 fallback 均归消费方入口。
    """
    env = load_environment(path)
    view = env.gate_state if env else None
    return GateRead(view, gate_verdict_issue(view))
