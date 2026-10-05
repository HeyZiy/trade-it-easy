# -*- coding: utf-8 -*-
"""
===================================
任务收尾 I/O（报告落盘 + 通知推送）
===================================

各入口脚本（etf_observe / industry_momentum / quality_pool）
共用的收尾两个动作——历史上四处逐字复制同一函数体，只差文件名前缀。

- save_report：Markdown 报告落盘 reports/{prefix}_{YYYYMMDD}.md，返回路径。
- notify：经 src/notify 多渠道推送；未配置渠道只 warning 不抛异常。
  一次运行内多次推送时传入同一个 notifier 实例复用渠道（不传则就地新建）。

归属：无交易语义的任务基础设施 → src/ 根级别共享工具。
"""

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def save_report(report: str, prefix: str, reports_dir: str = "reports") -> str:
    """保存报告到 reports/ 并返回路径。"""
    os.makedirs(reports_dir, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    report_path = os.path.join(reports_dir, f"{prefix}_{today_str}.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"报告已保存: {report_path}")
    return report_path


def notify(report: str, notifier=None) -> bool:
    """发送通知；渠道未配置或发送失败只记 warning，返回 False，不抛异常。

    Args:
        report: 通知正文
        notifier: 复用中的 NotificationService 实例；None 则就地新建
    """
    try:
        if notifier is None:
            from src.notify.service import NotificationService
            notifier = NotificationService()
        if not notifier.is_available():
            logger.error("通知服务未配置")
            return False
        success = notifier.send(report)
        if success:
            logger.info("通知发送成功")
        else:
            logger.error("通知发送失败")
        return success
    except Exception as e:
        logger.error(f"通知发送失败: {e}")
        return False
