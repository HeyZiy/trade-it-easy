# -*- coding: utf-8 -*-
"""
===================================
数据层横切件：请求节流 + TTL 快照缓存
===================================

职责：
1. Throttle — 网页类免费接口（akshare/efinance）的反爬节流，
   adaptive 模式带连续错误自适应退避（efinance 用）
2. TtlSnapshotCache — 全量拉取型实时行情接口的快照缓存，
   失败时由调用方存入空 DataFrame 作哨兵，TTL 内不再反复请求

注意：Tushare 的官方配额记账（80/min、500/day）语义不同，不走本模块。
"""

import logging
import random
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 错误计数自动重置窗口（秒）：超过该时长无新错误则清零
_ERROR_RESET_WINDOW = 60.0


class Throttle:
    """请求节流：最小间隔补足 + 随机 jitter；adaptive 模式额外做错误退避。

    用法：每次外呼前调用 wait()；调用方在失败/成功路径上分别调
    record_error()/record_success()（仅 adaptive 模式有意义）。
    """

    def __init__(self, sleep_min: float, sleep_max: float, adaptive: bool = False):
        self.sleep_min = sleep_min
        self.sleep_max = sleep_max
        self.adaptive = adaptive
        self._last_request_time: Optional[float] = None
        self._consecutive_errors: int = 0
        self._error_reset_time: Optional[float] = None

    def wait(self) -> None:
        now = time.time()

        if self.adaptive and self._error_reset_time is not None:
            if now - self._error_reset_time > _ERROR_RESET_WINDOW and self._consecutive_errors > 0:
                logger.debug(f"重置连续错误计数: {self._consecutive_errors} -> 0")
                self._consecutive_errors = 0
                self._error_reset_time = None

        # 基础间隔补足：距上次请求不足 sleep_min 时补齐差额
        if self._last_request_time is not None:
            elapsed = now - self._last_request_time
            if elapsed < self.sleep_min:
                additional_sleep = self.sleep_min - elapsed
                logger.debug(f"补充休眠 {additional_sleep:.2f} 秒")
                time.sleep(additional_sleep)

        # 自适应退避：每个连续错误加 0.5s，上限 3s
        if self.adaptive and self._consecutive_errors > 0:
            extra_sleep = min(self._consecutive_errors * 0.5, 3.0)
            logger.debug(f"自适应流控: 连续{self._consecutive_errors}次错误，额外休眠{extra_sleep:.2f}秒")
            time.sleep(extra_sleep)

        time.sleep(random.uniform(self.sleep_min, self.sleep_max))
        self._last_request_time = time.time()

    def record_error(self) -> None:
        """记录一次错误，用于自适应退避（非 adaptive 模式下仅计数不生效）"""
        self._consecutive_errors += 1
        self._error_reset_time = time.time()

    def record_success(self) -> None:
        """记录一次成功，连续错误计数 -1（下限 0）"""
        if self._consecutive_errors > 0:
            self._consecutive_errors -= 1


class TtlSnapshotCache:
    """全量快照接口的 TTL 缓存：get 命中返回快照，未命中返回 None。

    store 接受显式 timestamp，供刷新流程沿用"刷新前取的时间"作基准，
    把刷新耗时计入缓存年龄（与原实现一致）。
    """

    def __init__(self, ttl: float, label: str):
        self.ttl = ttl
        self.label = label
        self._data: Optional[pd.DataFrame] = None
        self._timestamp: float = 0.0

    def get(self, now: Optional[float] = None) -> Optional[pd.DataFrame]:
        now = now if now is not None else time.time()
        if self._data is not None and now - self._timestamp < self.ttl:
            cache_age = int(now - self._timestamp)
            logger.debug(f"[缓存命中] {self.label} - 缓存年龄 {cache_age}s/{self.ttl}s")
            return self._data
        return None

    def store(self, df: Optional[pd.DataFrame], timestamp: Optional[float] = None) -> None:
        self._data = df
        self._timestamp = timestamp if timestamp is not None else time.time()
        logger.info(f"[缓存更新] {self.label} 缓存已刷新，TTL={self.ttl}s")
