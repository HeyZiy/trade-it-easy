# -*- coding: utf-8 -*-
"""
===================================
市场环境开仓门控模块
===================================

职责：
1. fetch_index_df(): 【生产者 env_report 调用】取国证A指（399317）日线——AmazingData 单源（K 线 + 快照补
   当日 bar + 数据日期断言），与个股主源同源；无 akshare 回退
2. diagnose_gate(): 根据均线结构判断市场状态（5 级）+ 可解释诊断
3. gate_state_series(): 逐日五态回放（research 证据共用同一判定函数）
4. 判定文案（gate_log_line / gate_verdict_summary）——接受消费方算好的 can_open，只做渲染

环境层定位：本模块只产**标签与判定事实**，不持有策略 policy——
哪些状态允许开仓（CAN_OPEN_STATES）归各策略消费方自判。
当前无运行中的消费方（唯一消费方趋势双任务已退役）；需要市场门控的策略读环境快照 data/environment.json，不各自取数判定；
本模块的取数与判定仅由生产者 env_report 调用。
底层指数 = 国证A指（399317）。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

from data_provider.types import DataFetchError
from src.market_state.environment import GateVerdictView

logger = logging.getLogger(__name__)

# 状态判定的命中路径文案（与 diagnose_gate 的判定顺序一一对应，供报告诊断用）；
# 状态展示名（emoji+人话）是视图自描述，单点在 environment.GATE_STATE_LABELS。
# 命名纪律：五态全部是**过去式**——描述最近 20 日均线排列，不预测行情，
# 标签措辞避免任何未来式读法（"若真能预测下行，policy 就该是清仓，
# 而这套标签的实测前瞻收益是抛硬币"）。
GATE_STATE_PATHS = {
    "trending_down": "① 均线空头排列(MA5<MA10<MA20) + 收盘<MA10",
    "trending_up":   "② 均线多头排列(MA5>MA10>MA20) + 收盘>MA10",
    "sideways":      "③ 收盘紧贴MA20（偏离<1.5%）",
    "weak_up":       "④ 收盘>MA20（非标准多头排列）",
    "chaos":         "⑤ 收盘<MA20 的乱序区（或数据不足）",
}

SIDEWAYS_BIAS = 0.015  # sideways 判定阈值：收盘偏离 MA20 < 1.5%


def _n(value: Optional[float]) -> Optional[float]:
    """float 统一 2 位小数（报告口径，避免各处精度不一）。"""
    return None if value is None else round(float(value), 2)


def _alignment_text(ma5: float, ma10: float, ma20: float) -> str:
    """均线排列描述，如 'MA5>MA10>MA20（多头排列）'。"""
    order = sorted((("MA5", ma5), ("MA10", ma10), ("MA20", ma20)),
                   key=lambda kv: kv[1], reverse=True)
    seq = ">".join(name for name, _ in order)
    if seq == "MA5>MA10>MA20":
        label = "（多头排列）"
    elif seq == "MA20>MA10>MA5":
        label = "（空头排列）"
    else:
        label = "（交织，无明确排列）"
    return f"{seq}{label}"


@dataclass
class GateDiagnosis:
    """市场状态判定的诊断信息（回答"为什么判成这个状态"）。

    Attributes:
        gate_state: 判定结果
        ma5/ma10/ma20/close: 判定所依据的均线与收盘（均为 2 位小数）
        bias_ma20: 收盘偏离 MA20 的百分比（正=在 MA20 上方）
        alignment: 均线排列描述
        path: 命中的判定路径文案
        note: 补充说明（如数据不足）
        data_date: 判定所用数据的最新交易日（P0 验收：日报强制展示，杜绝 T-1 静默流入）
    """
    gate_state: str
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    close: Optional[float] = None
    bias_ma20: Optional[float] = None
    alignment: str = "数据不足，无法判定"
    path: str = ""
    note: str = ""
    data_date: str = ""

    @property
    def available(self) -> bool:
        return self.ma20 is not None and self.close is not None

    def describe(self) -> str:
        """人类可读的诊断行（供日志与报告共用）。"""
        date_part = f"｜数据日期 {self.data_date}" if self.data_date else ""
        if not self.available:
            return f"状态={self.gate_state}｜均线数据不足，无法给出排列与偏离（{self.note}）{date_part}"
        return (
            f"状态={self.gate_state}｜{self.alignment}｜"
            f"收盘{self.close} 偏离MA20 {self.bias_ma20:+.2f}%｜命中：{self.path}{date_part}"
        )


def _gate_state_label(ma5: float, ma10: float, ma20: float, close: float) -> str:
    """单根 bar 的五态判定（diagnose_gate 与 gate_state_series 的唯一判定路径）。

    判定优先级：trending_down > trending_up > sideways > weak_up > chaos
    """
    # ① trending_down — 均线空头（**过去式**：最近 20 日排列形状，不是行情预测），最高优先级
    if ma5 < ma10 < ma20 and close < ma10:
        return "trending_down"
    # ② trending_up — 均线多头
    if ma5 > ma10 > ma20 and close > ma10:
        return "trending_up"
    # ③ sideways — 紧贴 MA20 震荡
    if abs(close - ma20) / ma20 < SIDEWAYS_BIAS:
        return "sideways"
    # ④ weak_up — 在 MA20 上方，但不是标准多头排列
    if close > ma20:
        return "weak_up"
    # ⑤ chaos — 收盘 < MA20 的乱序区
    return "chaos"


def gate_state_series(index_df: pd.DataFrame) -> list:
    """逐日五态判定（矢量化均线 + 逐 bar 复用 _gate_state_label）。

    供 CycleStage 历史回放与 research 状态分解使用：与 diagnose_gate 同一判定函数。
    不足 20 根或 MA 含 NaN 的 bar 判 chaos。
    """
    df = index_df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    ma5 = close.rolling(5).mean().to_numpy()
    ma10 = close.rolling(10).mean().to_numpy()
    ma20 = close.rolling(20).mean().to_numpy()
    closes = close.to_numpy()

    gate_states: list = []
    for i in range(len(closes)):
        if i < 19 or any(np.isnan(x) for x in (ma5[i], ma10[i], ma20[i])):
            gate_states.append("chaos")
            continue
        gate_states.append(_gate_state_label(ma5[i], ma10[i], ma20[i], closes[i]))
    return gate_states


def diagnose_gate(index_df) -> GateDiagnosis:
    """根据指数均线结构判断市场状态，并给出可解释的诊断信息。

    判定路径与 gate_state_series 共用 _gate_state_label（单一事实来源）。

    Returns:
        GateDiagnosis：含 gate_state、MA5/MA10/MA20 排列、偏离 MA20 百分比、命中路径
        trending_up   — 均线多头排列 + 收盘在 MA10 上方
        trending_down — 均线空头排列 + 收盘在 MA10 下方
        sideways      — 收盘紧贴 MA20（偏离 < 1.5%）
        weak_up       — 收盘在 MA20 上方，但非明确多头
        chaos         — 收盘 < MA20 的乱序区，或数据不足
    """
    try:
        if index_df is None or len(index_df) < 20:
            return GateDiagnosis("chaos", path=GATE_STATE_PATHS["chaos"],
                                   note="指数日线缺失或不足20条")
        ma5 = index_df['close'].rolling(5).mean().iloc[-1]
        ma10 = index_df['close'].rolling(10).mean().iloc[-1]
        ma20 = index_df['close'].rolling(20).mean().iloc[-1]
        close = index_df['close'].iloc[-1]
        if any(pd.isna(x) for x in [ma5, ma10, ma20]):
            return GateDiagnosis("chaos", path=GATE_STATE_PATHS["chaos"],
                                   note="MA5/MA10/MA20 存在 NaN")

        ma5, ma10, ma20, close = float(ma5), float(ma10), float(ma20), float(close)
        bias_ma20 = (close - ma20) / ma20 * 100 if ma20 > 0 else 0.0
        data_date = ""
        try:
            data_date = str(pd.to_datetime(index_df['date'].iloc[-1]).date())
        except Exception:
            pass
        base = dict(
            ma5=_n(ma5), ma10=_n(ma10), ma20=_n(ma20), close=_n(close),
            bias_ma20=_n(bias_ma20), alignment=_alignment_text(ma5, ma10, ma20),
            data_date=data_date,
        )

        gate_state = _gate_state_label(ma5, ma10, ma20, close)

        # 命中路径文案与补充说明（仅诊断展示，不影响判定）
        if gate_state == "trending_down":
            return GateDiagnosis(gate_state, path=GATE_STATE_PATHS[gate_state],
                                   note=f"收盘{_n(close)} < MA10 {_n(ma10)}", **base)
        if gate_state == "trending_up":
            return GateDiagnosis(gate_state, path=GATE_STATE_PATHS[gate_state],
                                   note=f"收盘{_n(close)} > MA10 {_n(ma10)}", **base)
        if gate_state == "sideways":
            return GateDiagnosis(gate_state, path=GATE_STATE_PATHS[gate_state],
                                   note=f"偏离MA20 {_n(bias_ma20):+.2f}%", **base)
        if gate_state == "weak_up":
            return GateDiagnosis(gate_state, path=GATE_STATE_PATHS[gate_state],
                                   note="收盘在MA20上方，均线非标准多头", **base)
        return GateDiagnosis("chaos", path=GATE_STATE_PATHS["chaos"],
                               note="收盘<MA20 且未满足以上任一结构", **base)
    except Exception as e:
        logger.warning(f"市场状态判定失败，降级为 chaos：{e}")
    return GateDiagnosis("chaos", path=GATE_STATE_PATHS["chaos"], note="判定异常，降级处理")


def fetch_index_df() -> Optional[pd.DataFrame]:
    """取国证A指（399317）日线（市场状态判定的唯一数据源 = AmazingData，无回退源）。

    指数与个股主源统一走 AmazingData（服务器 cron 已配 TGW 凭证）——
    公开数据源（csindex/新浪日线盘后晚间更新、东财限流）无法保证收盘即含当日 bar。
    TGW 未配置（本地调试）或取数失败时返回 None → gate_state=chaos →
    当日不开仓：宁可不出信号，不拿过期数据做开仓判断。
    """
    try:
        from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher

        fetcher = AmazingDataFetcher()
    except Exception as e:
        logger.warning(f"指数数据不可用（TGW 未配置或初始化失败）: {e}")
        return None

    try:
        return _build_index_df(fetcher)
    except DataFetchError as e:
        logger.error(f"指数日线获取失败: {e}")
        return None
    except Exception as e:
        logger.error(f"指数日线组装异常: {e}")
        return None


def _expected_latest_trade_date(now=None) -> Optional[date]:
    """预期指数最新交易日：交易日取当天，节假日取此前最近一个交易日。

    15:10 买入分析 / 14:45 卖出任务都在交易日盘中后段运行，
    因此交易日的"预期最新交易日"就是当天。
    谓词单点在 trading_calendar.latest_trading_day_on_or_before，
    fallback=None：日历不可用返回 None → 调用方跳过数据日期断言。
    """
    from src.trading_calendar import latest_trading_day_on_or_before

    d = (now or datetime.now()).date()
    return latest_trading_day_on_or_before(d, fallback=None)


def _build_index_df(fetcher) -> Optional[pd.DataFrame]:
    """指数日线组装：K 线 + 快照补当日 bar + 数据日期断言。

    1. query_kline 拉指数日线；
    2. 若最后一根 bar 落后于预期交易日（当日 bar 未入库的已知缺口），
       用 query_snapshot 当日最后一笔快照（close/volume）补一根——
       盘中为最新近似（与尾盘 14:45 近似收盘口径一致），收盘后为官方值；
    3. 断言：补齐后仍落后 → 返回 None 并显式报错，杜绝 T-1 数据流入判定。
    """
    df = fetcher.get_index_daily("sz399317")
    if df is None or df.empty:
        return None

    expected = _expected_latest_trade_date()
    if expected is None:
        return df  # 日历不可用时跳过断言（fail-open），日期已在日志可见

    last_date = df["date"].iloc[-1].date()
    if last_date >= expected:
        return df

    # 当日 bar 缺失 → 用当日最后一笔快照补
    snap = fetcher.get_index_snapshot("sz399317", expected)
    if not snap or not snap.get("close"):
        logger.error(
            f"🔴 指数数据过期且无法补齐：K 线最新 {last_date} < 预期交易日 {expected}，"
            f"快照补齐失败 → 拒绝提供（gate_state 将降级 chaos，当日不开仓）"
        )
        return None

    close = float(snap["close"])
    bar = {
        "date": pd.Timestamp(expected),
        "open": float(snap.get("open") or close),
        "high": float(snap.get("high") or close),
        "low": float(snap.get("low") or close),
        "close": close,
        "volume": float(snap.get("volume") or 0),
        "amount": float(snap.get("amount") or 0),
    }
    df = pd.concat([df, pd.DataFrame([bar])], ignore_index=True)
    logger.info(
        f"当日 bar 缺失，已用 {snap.get('trade_time', expected)} 快照补齐 "
        f"{expected}（close={close}）"
    )

    if df["date"].iloc[-1].date() < expected:
        logger.error(
            f"🔴 指数数据过期：补齐后最新 {df['date'].iloc[-1].date()} < 预期 {expected} → 拒绝提供"
        )
        return None
    return df


def gate_log_line(can_open: bool, gate_state: str) -> str:
    """一行门控结论（消费方日志用；✅ info / ⛔ warning 的取舍在调用方）。

    can_open 是消费方以策略层 policy（buy_pipeline.CAN_OPEN_STATES）对快照标签
    算出的结论，本函数只做 ✅/⛔ 渲染。
    """
    return (f"✅ 市场状态 {gate_state}，允许开仓" if can_open
            else f"⛔ 市场状态 {gate_state}，不开新仓")


def gate_verdict_summary(view: "GateVerdictView", can_open: bool) -> str:
    """快照 verdict 的人类可读判定摘要（消费方日志/报告用）。

    5 态人话词汇归本模块（与状态定义同侧）；can_open 由消费方策略层 policy 算出，
    本函数只负责把「标签 + 结论 + 诊断明细」渲染成文案。
    """
    if view.state == "trending_down":
        # 均线空头时禁止开仓：空头结构下"高成交+高情绪"是下跌中继/放量出货的典型
        # 特征，不是反转信号。趋势策略坚持"底部偏右进场"，等收盘重回 MA20 再参与。
        action = "📉 均线空头排列，禁止开仓（等收盘重回MA20）"
    elif not can_open:
        # sideways（横盘）/ chaos（收盘<MA20 乱序）/ weak_up（非标准多头）：
        # 禁止开新仓；持仓卖出照常按正常版输出。
        action = f"🌪️ 状态{view.state}：不开新仓（等方向明确）"
    else:
        action = "✅ 结构确认，允许开仓"

    data_date = f"｜数据日期 {view.data_date}" if view.data_date else ""
    return (
        f"市场状态判定：{view.state} → {'✅ 允许开仓' if can_open else '❌ 禁止开仓'}{data_date}\n"
        f"{action}\n{view.describe()}"
    )
