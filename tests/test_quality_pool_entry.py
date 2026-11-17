"""质量动量入口的交易日保护。"""

import sys

import pytest

import quality_pool as entry


@pytest.mark.parametrize(("trading", "args", "calls"), [
    (False, [], []),
    (False, ["--force"], [True]),
    (True, ["--force"], [False]),
    (True, ["--dry-run"], [True]),
])
def test_main_runs_only_when_allowed(monkeypatch, trading, args, calls):
    seen = []
    monkeypatch.setattr(entry, "is_trading_day", lambda _: trading)
    monkeypatch.setattr(entry, "setup_logging", lambda **_: None)
    monkeypatch.setattr(entry, "run", lambda today, *, dry_run, no_notify: seen.append(dry_run))
    monkeypatch.setattr(sys, "argv", ["quality_pool.py", *args])
    assert entry.main() == 0
    assert seen == calls
