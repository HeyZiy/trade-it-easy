# -*- coding: utf-8 -*-
"""
===================================
个股日线取数策略（多源 failover + 新鲜度校验 + 列回补）
===================================
给定 Need(kind=stock_daily) 与 fetcher 集合：
1. 按能力声明筛候选
2. 按优先级依次取数，失败自动切换
3. 校验数据新鲜度：最新 bar 必须覆盖到最近交易日
4. A 股主源缺换手率时，从其余 A 股源回补该列

边界：不含单源知识、不持有实例 —— fetcher 集合由调用方（manager / 研究脚本）传入；
交易日历以 `latest_trading_day` 谓词注入（组装点 manager 供料），本模块零 src 依赖。

ETF/指数日线不走这里（见 bars.py）。

新鲜度契约（显式，勿静默）：
- end_date 给值 → 最新 bar 必须覆盖到收敛交易日（注入谓词把周末/节假日收敛到
  最近交易日，避免把整条源链误判过期）；日历调用异常 → warning 后放行；
- end_date=None → 「要尽可能新」的宽松口径，显式跳过检查（warning 一次）。
  盘前/盘中以昨收为最新 bar 的调用依赖此口径；要严格校验请显式传 end_date。
"""
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, List, Optional

import pandas as pd

from .routing import supporting
from .types import DataFetchError, Need, summarize_exception

logger = logging.getLogger(__name__)

# 主源未提供、需从备用源补齐的关键列（当前仅换手率）。
# 各 fetcher 出口已归一化为 STANDARD_COLUMNS，故只按标准列名对齐补齐，
# 不覆盖主源已有有效数据；补齐后仍整列缺失则告警，交由下游降级/跳过处理。
# 回退前先按各源的 SUPPORTS_COLUMNS 能力声明过滤，跳过日线接口确定没有该列的源。
BACKFILL_COLUMNS = ['turnover_rate']


def _backfill_missing_columns(
    primary_df: pd.DataFrame,
    fetchers: List[Any],
    stock_code: str,
    start_date: Optional[str],
    end_date: Optional[str],
    days: int,
    primary_name: str,
) -> pd.DataFrame:
    """主源缺失关键列时从其余数据源补齐（按标准化 'date' 列对齐），并告警仍缺失的列。"""
    if primary_df is None or primary_df.empty:
        return primary_df
    for col in BACKFILL_COLUMNS:
        if col in primary_df.columns and not primary_df[col].isna().all():
            continue  # 主源已有有效数据，无需补齐
        for fb in fetchers:
            if fb.name == primary_name:
                continue  # 不从主源自身补齐
            if fb.SUPPORTS_COLUMNS is not None and col not in fb.SUPPORTS_COLUMNS:
                logger.debug(
                    f"[列回退] {stock_code} 跳过 [{fb.name}]：其日线接口不提供 '{col}'"
                )
                continue  # 该源确定没有此列，不发无效请求
            try:
                sub = fb.get_daily_data(
                    stock_code=stock_code,
                    start_date=start_date,
                    end_date=end_date,
                    days=days,
                )
            except Exception as e:
                logger.debug(f"[列回退] {stock_code} 从 [{fb.name}] 补齐 '{col}' 失败: {e}")
                continue
            if sub is None or sub.empty or col not in sub.columns or sub[col].isna().all():
                continue
            filled = primary_df['date'].map(dict(zip(sub['date'], sub[col])))
            if col not in primary_df.columns:
                primary_df[col] = filled
            else:
                missing = primary_df[col].isna()
                primary_df.loc[missing, col] = filled[missing].values
            logger.info(
                f"[列回退] {stock_code} 从 [{fb.name}] 补齐缺失列 '{col}' "
                f"(主源 {primary_name} 未提供)"
            )
            break
        # 遍历所有备用源后仍缺失 → 告警，下游降级/跳过处理
        if col not in primary_df.columns or primary_df[col].isna().all():
            logger.warning(
                f"[数据缺失] {stock_code} 关键列 '{col}' 在所有数据源均缺失，"
                f"依赖该列的信号/剔除逻辑将跳过或降级处理"
            )
    return primary_df


def fetch_stock_daily(
    need: Need,
    fetchers: List[Any],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: int = 30,
    *,
    latest_trading_day: Optional[Callable[[date], date]] = None,
) -> pd.DataFrame:
    """按需求取个股日线（多源 failover）。

    Args:
        need: kind=stock_daily 的需求（market 由 codes.classify_market 判定）
        fetchers: 已实例化的数据源列表（按优先级）
        start_date / end_date / days: 取数窗口
        latest_trading_day: 交易日历谓词（date → ≤date 的最近交易日），组装点注入；
            None = 无日历可用，新鲜度检查整体跳过（warning 一次）

    Returns:
        标准化日线 DataFrame（命中哪个数据源见日志）

    Raises:
        DataFetchError: 所有候选数据源都失败或被判定为数据过期时抛出
    """
    stock_code = need.code
    candidates = supporting(fetchers, need)

    errors = []
    total_fetchers = len(candidates)
    request_start = time.time()

    # 新鲜度口径一次性声明（勿在逐源循环里静默降级）：
    # - end_date 未指定 → 宽松口径（见模块 docstring），warning 一次；
    # - 无日历 → 检查不可用，warning 一次（组装点应注入，见 manager）。
    check_freshness = latest_trading_day is not None and end_date is not None
    if end_date is None:
        logger.warning("[新鲜度] end_date 未指定：宽松口径，跳过新鲜度检查"
                       "（要严格校验请显式传 end_date）")
    elif latest_trading_day is None:
        logger.warning("[新鲜度] 未注入交易日历：新鲜度检查不可用")

    for attempt, fetcher in enumerate(candidates, start=1):
        try:
            logger.info(f"[数据源尝试 {attempt}/{total_fetchers}] [{fetcher.name}] 获取 {stock_code}...")
            df = fetcher.get_daily_data(
                stock_code=stock_code,
                start_date=start_date,
                end_date=end_date,
                days=days
            )

            if df is not None and not df.empty:
                # 新鲜度检查：最新 bar 日期 ≥ end_date 收敛到的最近交易日。
                # 收敛由注入谓词完成（end_date 常为周末/节假日，直接比对会误判整条链过期）。
                if check_freshness:
                    try:
                        df_latest = pd.to_datetime(df['date'].max()).date()
                        target_date = pd.to_datetime(end_date).date() if isinstance(end_date, str) else end_date
                        target_date = latest_trading_day(target_date)
                        if df_latest < target_date:
                            raise DataFetchError(
                                f"数据过期(最新:{df_latest}, 需要:{target_date})"
                            )
                    except DataFetchError:
                        raise
                    except Exception as e:
                        # 日历异常：可见但不阻断（放行本源数据，与宽松口径一致）
                        logger.warning(f"[{fetcher.name}] 数据新鲜度检查异常，放行: {e}")

                elapsed = time.time() - request_start
                logger.info(
                    f"[数据源完成] {stock_code} 使用 [{fetcher.name}] 获取成功: "
                    f"rows={len(df)}, elapsed={elapsed:.2f}s"
                )
                # 主源缺失关键列时从备用源补齐；补齐后仍缺失则告警（交由下游降级/跳过）
                # 回退列（换手率）只被 A 股规则消费，且回退源全是 A 股源，非 A 股不补
                if need.market == "cn":
                    df = _backfill_missing_columns(
                        df, fetchers, stock_code, start_date, end_date, days,
                        primary_name=fetcher.name,
                    )
                return df

        except Exception as e:
            error_type, error_reason = summarize_exception(e)
            error_msg = f"[{fetcher.name}] ({error_type}) {error_reason}"
            logger.warning(
                f"[数据源失败 {attempt}/{total_fetchers}] [{fetcher.name}] {stock_code}: "
                f"error_type={error_type}, reason={error_reason}"
            )
            errors.append(error_msg)
            if attempt < total_fetchers:
                next_fetcher = candidates[attempt]
                logger.info(f"[数据源切换] {stock_code}: [{fetcher.name}] -> [{next_fetcher.name}]")
            # 继续尝试下一个数据源
            continue

    # 所有数据源都失败
    error_summary = f"所有数据源获取 {stock_code} 失败:\n" + "\n".join(errors)
    elapsed = time.time() - request_start
    logger.error(f"[数据源终止] {stock_code} 获取失败: elapsed={elapsed:.2f}s\n{error_summary}")
    raise DataFetchError(error_summary)
