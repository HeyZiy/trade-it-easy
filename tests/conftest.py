# -*- coding: utf-8 -*-
"""pytest 根路径注入：保证 `python -m pytest` 从仓库任意目录运行都能 import src.*。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolate_actual_account(tmp_path, monkeypatch):
    """测试不得探测或修改开发机/生产机上的实际账户。"""
    monkeypatch.setattr("src.real_account.ACCOUNT_PATH", tmp_path / "unused-core-account.json")
