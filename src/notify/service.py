# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 通知层
===================================

职责：
1. 汇总分析结果生成日报
2. 支持 Markdown 格式输出
3. 多渠道推送（自动识别）：
   - 企业微信 Webhook
   - 飞书 Webhook
   - Telegram Bot
   - 邮件 SMTP
   - Pushover（手机/桌面推送）
"""
import logging
import requests
from datetime import datetime
from enum import Enum
from typing import List, Optional

from src.config import get_config
from src.notify.email import EmailSender

logger = logging.getLogger(__name__)


class NotificationChannel(Enum):
    """通知渠道类型"""
    WECHAT = "wechat"      # 企业微信
    FEISHU = "feishu"      # 飞书
    TELEGRAM = "telegram"  # Telegram
    EMAIL = "email"        # 邮件
    PUSHOVER = "pushover"  # Pushover（手机/桌面推送）
    PUSHPLUS = "pushplus"  # PushPlus（国内推送服务）
    SERVERCHAN3 = "serverchan3"  # Server酱3（手机APP推送服务）
    CUSTOM = "custom"      # 自定义 Webhook
    DISCORD = "discord"    # Discord 机器人 (Bot)
    ASTRBOT = "astrbot"
    UNKNOWN = "unknown"    # 未知


class ChannelDetector:
    """
    渠道检测器 - 简化版
    
    根据配置直接判断渠道类型（无需 URL 解析）
    """
    
    @staticmethod
    def get_channel_name(channel: NotificationChannel) -> str:
        """获取渠道中文名称"""
        names = {
            NotificationChannel.WECHAT: "企业微信",
            NotificationChannel.FEISHU: "飞书",
            NotificationChannel.TELEGRAM: "Telegram",
            NotificationChannel.EMAIL: "邮件",
            NotificationChannel.PUSHOVER: "Pushover",
            NotificationChannel.PUSHPLUS: "PushPlus",
            NotificationChannel.SERVERCHAN3: "Server酱3",
            NotificationChannel.CUSTOM: "自定义Webhook",
            NotificationChannel.DISCORD: "Discord机器人",
            NotificationChannel.ASTRBOT: "ASTRBOT机器人",
            NotificationChannel.UNKNOWN: "未知渠道",
        }
        return names.get(channel, "未知渠道")


class NotificationService(EmailSender):
    """
    通知服务
    
    职责：
    1. 生成 Markdown 格式的分析日报
    2. 向所有已配置的渠道推送消息（多渠道并发）
    3. 支持本地保存日报
    
    支持的渠道：
    - 企业微信 Webhook
    - 飞书 Webhook
    - Telegram Bot
    - 邮件 SMTP
    - Pushover（手机/桌面推送）
    
    注意：所有已配置的渠道都会收到推送
    """
    
    def __init__(self):
        """
        初始化通知服务
        
        只初始化邮件发送功能
        """
        config = get_config()

        # 仅分析结果摘要（Issue #262）：true 时只推送汇总，不含个股详情


        # 只初始化邮件渠道
        EmailSender.__init__(self, config)

        # 检测所有已配置的渠道
        self._available_channels = self._detect_all_channels()
        if not self._available_channels:
            logger.error("未配置有效的通知渠道，将不发送推送通知")
        else:
            channel_names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
            logger.info(f"已配置 {len(channel_names)} 个通知渠道：{', '.join(channel_names)}")

    def _detect_all_channels(self) -> List[NotificationChannel]:
        """
        检测所有已配置的渠道

        Returns:
            已配置的渠道列表
        """
        channels = []

        # 邮件
        if self._is_email_configured():
            channels.append(NotificationChannel.EMAIL)

        # 飞书群机器人 Webhook（FEISHU_WEBHOOK_URL）
        try:
            cfg = get_config()
            if getattr(cfg, 'feishu_webhook_url', None):
                channels.append(NotificationChannel.FEISHU)
        except Exception:
            pass

        return channels

    def is_available(self) -> bool:
        """检查通知服务是否可用（至少有一个渠道）"""
        return len(self._available_channels) > 0
    


    def send(
        self,
        content: str,
        email_stock_codes: Optional[List[str]] = None,
        email_send_to_all: bool = False
    ) -> bool:
        """
        统一发送接口 - 只向邮件渠道发送

        Args:
            content: 消息内容（Markdown 格式）
            email_stock_codes: 股票代码列表（可选，用于邮件渠道路由到对应分组邮箱，Issue #268）
            email_send_to_all: 邮件是否发往所有配置邮箱（用于大盘复盘等无股票归属的内容）

        Returns:
            是否发送成功
        """
        if not self._available_channels:
            logger.warning("通知服务不可用，跳过推送")
            return False

        success_count = 0
        fail_count = 0

        for channel in self._available_channels:
            channel_name = ChannelDetector.get_channel_name(channel)
            try:
                if channel == NotificationChannel.EMAIL:
                    receivers = None
                    if email_send_to_all and self._stock_email_groups:
                        receivers = self.get_all_email_receivers()
                    elif email_stock_codes and self._stock_email_groups:
                        receivers = self.get_receivers_for_stocks(email_stock_codes)
                    result = self.send_to_email(content, receivers=receivers)
                elif channel == NotificationChannel.FEISHU:
                    result = self._send_feishu(content)
                else:
                    logger.warning(f"不支持的通知渠道: {channel}")
                    result = False

                if result:
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.error(f"{channel_name} 发送失败: {e}")
                fail_count += 1

        logger.info(f"通知发送完成：成功 {success_count} 个，失败 {fail_count} 个")
        return success_count > 0
   
    def _send_feishu(self, content: str) -> bool:
        """
        通过飞书群机器人 Webhook 推送 Markdown 报告。

        飞书自定义机器人 Webhook 仅原生支持 text/post 等消息类型（不直接渲染 Markdown），
        故先用 format_feishu_markdown 转换为飞书友好的纯文本，再按字节分片发送以避开长度限制。
        注意：机器人安全设置请选「无需」或「自定义关键词」，勿选「签名校验」（本方法不计算签名）。
        """
        from src.notify.formatters import format_feishu_markdown

        try:
            cfg = get_config()
            url = getattr(cfg, 'feishu_webhook_url', None)
            if not url:
                logger.error("飞书 Webhook 未配置（FEISHU_WEBHOOK_URL 为空），无法推送")

            text = format_feishu_markdown(content)
            max_bytes = int(getattr(cfg, 'feishu_max_bytes', 20000) or 20000)
            raw = text.encode('utf-8')
            chunks = [
                raw[i:i + max_bytes].decode('utf-8', errors='ignore')
                for i in range(0, len(raw), max_bytes)
            ] or [text]

            ok = True
            for idx, chunk in enumerate(chunks, 1):
                payload = {"msg_type": "text", "content": {"text": chunk}}
                try:
                    resp = requests.post(url, json=payload, timeout=15)
                    data = resp.json()
                except Exception as e:
                    logger.error(f"飞书推送请求失败({idx}/{len(chunks)}): {e}")
                    ok = False
                    continue
                if data.get("code", 0) != 0:
                    logger.error(f"飞书推送失败({idx}/{len(chunks)}): {data}")
                    ok = False
                else:
                    logger.info(f"飞书推送成功({idx}/{len(chunks)})")
            return ok
        except Exception as e:
            logger.error(f"飞书推送异常: {e}")
            return False

