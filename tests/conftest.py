# -*- coding: utf-8 -*-
"""pytest 根路径注入：保证 `python -m pytest` 从仓库任意目录运行都能 import src.*。"""
from datetime import timedelta
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolate_core_account(tmp_path, tmp_path_factory, monkeypatch):
    """测试不得探测或修改开发机/生产机上的ETF账户台账。"""
    monkeypatch.setattr("src.etf_account.ACCOUNT_PATH", tmp_path / "unused-core-account.json")
    monkeypatch.setattr("data_provider.security_names.CACHE_PATH", tmp_path / "security_names.sqlite3")

    def previous_weekday(day, **_):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day

    monkeypatch.setattr("src.account_valuation.latest_trading_day_on_or_before", previous_weekday)

    from src import portfolio_config
    config_path = tmp_path_factory.mktemp("settings") / "etf_config.json"
    monkeypatch.setattr(portfolio_config, "DEFAULT_PATH", config_path)
    config_path.write_text(portfolio_config.EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
