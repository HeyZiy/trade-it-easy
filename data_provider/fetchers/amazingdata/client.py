"""AmazingData 进程级会话；只负责认证和 SDK 对象，不依赖数据能力或 Fetcher。"""
import contextlib
import io
import logging
import os
import threading
import time
from typing import Optional

from dotenv import load_dotenv
from data_provider.types import DataFetchError
from ._utils import _PROJECT_ROOT

load_dotenv(_PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)
_LOGIN_RETRIES = max(1, int(os.getenv("AMAZINGDATA_LOGIN_RETRIES", "3")))
_LOGIN_RETRY_DELAY = float(os.getenv("AMAZINGDATA_LOGIN_RETRY_DELAY", "3"))


def _credentials():
    fields = ("USERNAME", "PASSWORD", "HOST", "PORT")
    # Any TGW configuration selects that whole group; never mix two accounts.
    prefix = "TGW" if any(os.getenv(f"TGW_{f}", "").strip() for f in fields) else "AD"
    return {f.lower(): os.getenv(f"{prefix}_{f}", "").strip() for f in fields}


def is_configured() -> bool:
    return all(_credentials().values())


class AmazingDataClient:
    """构造时不导入 SDK、不联网；同一进程的调用共用默认 client。"""

    def __init__(self):
        self._login_lock = threading.Lock()
        self._login_attempted = False
        self._login_success = False
        self._base_data = None
        self._info_data = None
        self._market_data = None
        self._calendar = None

    def ensure_login(self) -> None:
        """确保已登录 tgw 并拿到 MarketData 实例。"""
        credentials = _credentials()
        if not all(credentials.values()):
            raise DataFetchError("AmazingData 缺少完整凭证：TGW_USERNAME/TGW_PASSWORD/TGW_HOST/TGW_PORT（或完整 AD_*）")
        with self._login_lock:
            if self._login_success and self._market_data is not None:
                return
            if self._login_attempted and not self._login_success:
                raise DataFetchError("AmazingData 登录已尝试失败，跳过该数据源")

            self._login_attempted = True
            last_err: Optional[BaseException] = None
            for attempt in range(1, _LOGIN_RETRIES + 1):
                try:
                    # 登录会打印大量 logon json，临时静默
                    with contextlib.redirect_stdout(io.StringIO()):
                        import AmazingData as ad

                        ad.login(
                            username=credentials["username"],
                            password=credentials["password"],
                            host=credentials["host"],
                            port=int(credentials["port"]),
                        )
                        base = ad.BaseData()
                        cal = base.get_calendar()
                        market = ad.MarketData(cal)
                    self._base_data = base
                    self._calendar = cal
                    self._market_data = market
                    self._login_success = True
                    logger.info(f"AmazingData 登录成功，交易日历 {len(cal)} 天")
                    return
                except SystemExit:
                    # ad.login 失败时会 sys.exit(0)，视为一次可重试的登录失败
                    last_err = DataFetchError("AmazingData 登录失败（账号/密码/网络）")
                    logger.warning(f"AmazingData 登录失败（第 {attempt}/{_LOGIN_RETRIES} 次，SystemExit）")
                except Exception as e:
                    last_err = e
                    logger.warning(f"AmazingData 登录异常（第 {attempt}/{_LOGIN_RETRIES} 次）：{e}")
                if attempt < _LOGIN_RETRIES:
                    time.sleep(_LOGIN_RETRY_DELAY)

            self._login_success = False
            raise DataFetchError(
                f"AmazingData 登录连续 {_LOGIN_RETRIES} 次失败，放弃该数据源：{last_err}"
            ) from last_err


    def get_base_data(self):
        self.ensure_login()
        return self._base_data

    def get_info_data(self):
        self.ensure_login()
        with self._login_lock:
            if self._info_data is None:
                with contextlib.redirect_stdout(io.StringIO()):
                    import AmazingData as ad
                    self._info_data = ad.InfoData()
            return self._info_data

    def get_market_data(self):
        self.ensure_login()
        return self._market_data


client = AmazingDataClient()
