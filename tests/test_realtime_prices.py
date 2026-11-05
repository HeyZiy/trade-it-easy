"""批量价格接口按代码回退，已取得的价格直接交付给业务。"""
from types import SimpleNamespace

from data_provider import realtime_prices
from data_provider.manager import DataFetcherManager


class Source:
    priority = 0

    def __init__(self, key, result):
        self.REALTIME_VARIANTS = {key: {'variant': key}}
        self.result = result
        self.calls = []

    def get_realtime_prices(self, codes, *, variant):
        self.calls.append((variant, codes))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def manager(monkeypatch, fetchers, priority, *, enabled=True):
    monkeypatch.setattr('src.config.get_config', lambda: SimpleNamespace(
        enable_realtime_quote=enabled, realtime_source_priority=priority))
    return DataFetcherManager(fetchers=fetchers)


def test_fallback_receives_only_missing_codes_and_cannot_override(monkeypatch):
    first = Source('first', {'510300': 4, '600519': float('inf'), '159915': -2})
    second = Source('second', {'510300': 8, '600519': 1500, '159915': True})
    third = Source('third', {'159915': 2.4, '000001': 10})
    fm = manager(monkeypatch, [third, second, first], 'first,second,third')
    prices = realtime_prices(['sh510300', '510300.SH', '600519', '159915'], manager=fm)
    assert prices == {'sh510300': 4., '510300.SH': 4., '600519': 1500., '159915': 2.4}
    assert first.calls == [('first', ['510300', '600519', '159915'])]
    assert second.calls == [('second', ['600519', '159915'])]
    assert third.calls == [('third', ['159915'])]


def test_complete_prices_do_not_contact_later_source(monkeypatch):
    first = Source('first', {'510300': 4})
    second = Source('second', {'510300': 8})
    fm = manager(monkeypatch, [first, second], 'first,second')
    assert realtime_prices(['510300'], manager=fm) == {'510300': 4.}
    assert not second.calls


def test_failed_source_continues_and_omits_unpriced_symbols(monkeypatch, caplog):
    first = Source('first', RuntimeError('offline'))
    second = Source('second', {'510300': 4, '159915': None})
    fm = manager(monkeypatch, [first, second], 'typo,first,second,second')
    assert realtime_prices(['510300', '159915'], manager=fm) == {'510300': 4.}
    assert second.calls == [('second', ['510300', '159915'])]
    assert '未声明的源名' in caplog.text
    assert '缺少有效价格' in caplog.text


def test_disabled_or_empty_request_does_not_query_sources(monkeypatch):
    first = Source('first', {'510300': 4})
    fm = manager(monkeypatch, [first], 'first', enabled=False)
    assert realtime_prices(['510300'], manager=fm) == {}
    assert realtime_prices([]) == {}
    assert not first.calls
