# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 配置管理模块
===================================

职责：
1. 使用单例模式管理全局配置
2. 从 .env 文件加载敏感配置
3. 提供类型安全的配置访问接口
"""

import logging
import os
import re
from dataclasses import dataclass, field
from dotenv import load_dotenv
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# 实时源优先级默认串（排序 policy 唯一显式列表；源名须与各 fetcher 的
# REALTIME_VARIANTS 声明匹配，未声明名会在 merge_realtime_quotes 启动汇总 warning）
DEFAULT_REALTIME_PRIORITY = "tencent,akshare_sina,efinance,akshare_em"


@dataclass
class ConfigIssue:
    """Structured configuration validation issue with a severity level.

    Attributes:
        severity: One of "error", "warning", or "info".
        message:  Human-readable description of the issue.
        field:    The environment variable / config field name most relevant to
                  this issue (empty string when not applicable).
    """

    severity: Literal["error", "warning", "info"]
    message: str
    field: str = ""

    def __str__(self) -> str:  # noqa: D105
        return self.message


def setup_env(override: bool = False):
    """
    Initialize environment variables from .env file.

    Args:
        override: If True, overwrite existing environment variables with values
                  from .env file. Set to True when reloading config after updates.
                  Default is False to preserve behavior on initial load where
                  system environment variables take precedence.
    """
    # src/config.py -> src/ -> root
    env_file = os.getenv("ENV_FILE")
    if env_file:
        env_path = Path(env_file)
    else:
        env_path = Path(__file__).parent.parent / '.env'
    load_dotenv(dotenv_path=env_path, override=override)


@dataclass
class Config:
    """
    系统配置类 - 单例模式

    仅保留项目实际消费的字段。
    """

    # === 数据源 API Token ===
    mx_apikey: Optional[str] = None

    # === 分析筛选 ===
    bias_threshold: float = 5.0

    # === 实时行情配置 ===
    enable_realtime_quote: bool = True
    realtime_source_priority: str = DEFAULT_REALTIME_PRIORITY

    # === 邮件通知 ===
    email_sender: Optional[str] = None
    email_sender_name: str = "daily_stock_analysis股票分析助手"
    email_password: Optional[str] = None
    email_receivers: List[str] = field(default_factory=list)
    stock_email_groups: List[Tuple[List[str], List[str]]] = field(default_factory=list)

    # === 飞书 Webhook ===
    feishu_webhook_url: Optional[str] = None
    feishu_max_bytes: int = 20000

    # 单例实例存储
    _instance: Optional['Config'] = None
    
    @classmethod
    def get_instance(cls) -> 'Config':
        """
        获取配置单例实例
        
        单例模式确保：
        1. 全局只有一个配置实例
        2. 配置只从环境变量加载一次
        3. 所有模块共享相同配置
        """
        if cls._instance is None:
            cls._instance = cls._load_from_env()
        return cls._instance
    
    @classmethod
    def _load_from_env(cls) -> 'Config':
        """从环境变量 + .env 加载配置（仅保留项目实际消费的字段）。"""
        setup_env()
        return cls(
            mx_apikey=(os.getenv('MX_APIKEY') or os.getenv('MX_API_KEY') or '').strip() or None,
            bias_threshold=max(1.0, float(os.getenv('BIAS_THRESHOLD', '5.0'))),
            enable_realtime_quote=os.getenv('ENABLE_REALTIME_QUOTE', 'true').lower() == 'true',
            realtime_source_priority=(os.getenv('REALTIME_SOURCE_PRIORITY')
                                      or DEFAULT_REALTIME_PRIORITY),
            email_sender=os.getenv('EMAIL_SENDER'),
            email_sender_name=os.getenv('EMAIL_SENDER_NAME', 'daily_stock_analysis股票分析助手'),
            email_password=os.getenv('EMAIL_PASSWORD'),
            email_receivers=[r.strip() for r in os.getenv('EMAIL_RECEIVERS', '').split(',') if r.strip()],
            stock_email_groups=cls._parse_stock_email_groups(),
            feishu_webhook_url=os.getenv('FEISHU_WEBHOOK_URL'),
            feishu_max_bytes=int(os.getenv('FEISHU_MAX_BYTES', '20000')),
        )
    @classmethod
    def _parse_stock_email_groups(cls) -> List[Tuple[List[str], List[str]]]:
        """
        Parse STOCK_GROUP_N and EMAIL_GROUP_N from environment.
        Returns [(stocks, emails), ...] ordered by group index.
        """
        groups: dict = {}
        stock_re = re.compile(r'^STOCK_GROUP_(\d+)$', re.IGNORECASE)
        email_re = re.compile(r'^EMAIL_GROUP_(\d+)$', re.IGNORECASE)
        for key in os.environ:
            m = stock_re.match(key)
            if m:
                idx = int(m.group(1))
                val = os.environ[key].strip()
                groups.setdefault(idx, {})['stocks'] = [c.strip() for c in val.split(',') if c.strip()]
            m = email_re.match(key)
            if m:
                idx = int(m.group(1))
                val = os.environ[key].strip()
                groups.setdefault(idx, {})['emails'] = [e.strip() for e in val.split(',') if e.strip()]
        result = []
        for idx in sorted(groups.keys()):
            g = groups[idx]
            if 'stocks' in g and 'emails' in g and g['stocks'] and g['emails']:
                result.append((g['stocks'], g['emails']))
        return result

# === 便捷的配置访问函数 ===
def get_config() -> Config:
    """获取全局配置实例的快捷方式"""
    return Config.get_instance()

