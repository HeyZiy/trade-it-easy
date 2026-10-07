# -*- coding: utf-8 -*-
"""
===================================
日志告警处理器 - WARNING/ERROR 日志推送通知
===================================

职责：
1. 将 WARNING 及以上的日志记录推送到已配置的通知渠道
2. 去重 + 限流，防止错误风暴刷屏
3. 后台线程异步发送，不阻塞主流程
4. 防递归，避免通知服务自身的日志触发连锁告警
"""

import atexit
import logging
import queue
import threading
import time

# 第三方库 logger 前缀，这些库的 WARNING 多为连接细节噪音，不触发告警
IGNORED_LOGGER_PREFIXES = (
    "urllib3",
    "google",
    "httpx",
    "requests",
    "charset_normalizer",
)

# 取数遥测类模式：失败由调用方按设计降级（多源合并/回退收盘，diag 与日报计数），
# 只进日志文件不进告警——否则休市日/限流日的正常降级会刷屏，淹没真故障
SUPPRESSED_PATTERNS = (
    "[熔断]",       # 数据源熔断器记录
    "[API错误]",    # 取数重试与最终失败遥测
    "[实时合并]",   # 实时行情合并无可用数据源（回退最新收盘）
    "[实时行情]",   # 单源快照为空
)


class LogAlertHandler(logging.Handler):
    """将 WARNING 及以上日志推送到通知渠道。

    特性：
    1. 去重：同一 (级别, 消息) 在去重窗口内只发送一次
    2. 限流：滑动窗口内最多发送 max_per_window 条
    3. 防递归：发送期间标记 busy，通知服务自身产生的日志不会再次触发发送
    4. 异步：队列 + 守护线程，进程退出时尽力 flush
    """

    def __init__(
        self,
        level: int = logging.WARNING,
        dedupe_window_s: float = 300,
        max_per_window: int = 10,
        window_s: float = 60,
        queue_timeout_s: float = 0.5,
    ):
        super().__init__(level=level)
        self._dedupe_window = dedupe_window_s
        self._max_per_window = max_per_window
        self._window_s = window_s
        self._recent: dict = {}
        self._window_start = time.monotonic()
        self._window_count = 0
        self._lock = threading.Lock()
        self._queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._busy = False
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="log-alert"
        )
        self._worker.start()
        self._notifier = None
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        """入队 WARNING+ 记录（去重 + 限流）。"""
        try:
            if record.name.startswith(IGNORED_LOGGER_PREFIXES):
                return
            if any(p in record.getMessage() for p in SUPPRESSED_PATTERNS):
                return
            msg = self.format(record)
            key = (record.levelno, msg)
            now = time.monotonic()

            with self._lock:
                # 去重：同一消息在窗口内只发一次
                if key in self._recent and now - self._recent[key] < self._dedupe_window:
                    return
                self._recent[key] = now
                # 清理过期的去重条目，避免内存膨胀
                if len(self._recent) > 512:
                    self._recent = {
                        k: t for k, t in self._recent.items()
                        if now - t < self._dedupe_window
                    }
                # 限流：滑动窗口计数
                if now - self._window_start > self._window_s:
                    self._window_start = now
                    self._window_count = 0
                if self._window_count >= self._max_per_window:
                    return
                self._window_count += 1

            self._queue.put((record.levelname, msg))
        except Exception:
            self.handleError(record)

    def _run(self) -> None:
        """后台工作线程：从队列取消息并发送。"""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            levelname, msg = item
            self._send(levelname, msg)

    def _send(self, levelname: str, msg: str) -> None:
        """推送一条告警；busy 期间直接丢弃（防递归）。"""
        if self._busy:
            return
        self._busy = True
        try:
            if self._notifier is None:
                from src.notify.service import NotificationService

                self._notifier = NotificationService()
            notifier = self._notifier
            if not notifier.is_available():
                return
            emoji = (
                "\U0001F6A8" if levelname in ("ERROR", "CRITICAL") else "\u26A0\uFE0F"
            )
            notifier.send(f"{emoji} **[{levelname}] 日志告警**\n\n{msg}")
        except Exception as e:
            # 告警发送失败不递归触发告警，仅落到标准错误输出
            try:
                import sys

                print(f"[log-alert] 告警发送失败: {e}", file=sys.stderr)
            except Exception:
                pass
        finally:
            self._busy = False

    def close(self) -> None:
        """停止工作线程前尽力 flush 队列（最多等待 queue_timeout 若干轮）。"""
        if self._stop.is_set():
            return
        # 等待队列清空（最多约 5 秒）
        deadline = time.monotonic() + 5
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        super().close()
