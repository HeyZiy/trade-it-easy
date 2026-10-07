# -*- coding: utf-8 -*-
"""重试细节留在日志，真实的策略行情降级仍进入告警队列。"""

import logging

from src.notify.log_alert import LogAlertHandler


def test_source_failure_is_suppressed_but_strategy_impact_is_queued(monkeypatch):
    # 只验证入队决策，禁止测试调用真实通知渠道。
    monkeypatch.setattr(LogAlertHandler, "_run", lambda self: None)
    handler = LogAlertHandler()
    try:
        handler.emit(logging.LogRecord(
            "data_provider.fetchers._snapshot_quote", logging.WARNING,
            __file__, 1, "[API错误] ETF实时行情(efinance) 最终失败", (), None,
        ))
        assert handler._queue.empty()
        handler.emit(logging.LogRecord(
            "src.etf.industry_momentum", logging.WARNING, __file__, 1,
            "[行情降级] 行业动量截面 47/47 只 ETF 已回退到最新收盘价（非实时）",
            (), None,
        ))
        level, message = handler._queue.get_nowait()
        assert level == "WARNING" and "47/47" in message
        assert handler._queue.empty()
    finally:
        handler.close()
