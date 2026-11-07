"""名称来自单一展示缓存；改名不改变账户事实和收益。"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from data_provider import security_names as names
from src import etf_account as wallet


def test_cache_normalizes_updates_and_does_not_replace_with_empty_name():
    assert names.security_name('sh510300') == '510300'
    names.remember({'sh510300': '沪深300ETF', '588000': '科创50ETF'})
    assert names.security_name('510300') == '沪深300ETF'
    names.remember({'510300': '', '588000': '588000'})
    assert names.security_name('588000') == '科创50ETF'
    names.remember({'510300': '新的证券简称'})
    assert names.security_name('510300') == '新的证券简称'
    assert names.security_name('CASH') == '现金'


def test_concurrent_updates_preserve_other_codes():
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(names.remember, ({code: f'名称{code}'} for code in ('510300','588000','513100','513500'))))
    assert len(names.names()) == 4


def test_corrupt_cache_does_not_break_price_fetch_or_name_lookup():
    names.CACHE_PATH.write_bytes(b'not sqlite')
    names.remember({'588000': '名称'})
    assert names.security_name('588000') == '588000'


def test_existing_quote_request_caches_name_without_extra_request():
    from data_provider.fetchers.base import BaseFetcher
    calls = []
    def quote(code):
        calls.append(code)
        return SimpleNamespace(name='科创50ETF', price=1.228)
    adapter = SimpleNamespace(get_realtime_quote=quote)
    prices = BaseFetcher.get_realtime_prices(adapter, ['588000'])
    assert calls == ['588000'] and prices == {'588000': 1.228}
    assert names.security_name('588000') == '科创50ETF'
    assert names.names_for(['sh588000', '510300']) == {'sh588000': '科创50ETF'}


def test_account_code_only_renames_without_rewriting_ledger_or_nav(tmp_path):
    path=tmp_path/'etf_account.json'
    wallet.initialize(path, asof='2025-01-03', cash=1000,
                      positions=[dict(code='588000',count=100,price=1,strategy='core')],
                      budgets={'core':1},cash_allocations={'core':1000})
    before = path.read_bytes()
    view = wallet.account_data(path)
    assert view['positions'][0]['name'] == '588000'
    assert 'name' not in wallet.load(path)['initial']['positions'][0]
    names.remember({'588000':'科创50ETF'})
    renamed = wallet.account_data(path)
    assert renamed['positions'][0]['name'] == '科创50ETF'
    for key in ('cash','equity','pnl','revision','history'):
        assert renamed[key] == view[key]
    assert path.read_bytes() == before
    wallet.record_event(path,dict(request_id='code-only-buy',revision=renamed['revision'],date='2025-01-06',
                                 side='buy',strategy='core',code='588000',qty=100,price=1,fee=0))
    assert 'name' not in wallet.load(path)['events'][-1]
    assert wallet.account_data(path)['trades'][0]['name'] == '科创50ETF'


def test_strategy_names_follow_cache_changes_without_reimport():
    from src.etf.config import example_baseline
    CORE_BASELINE = example_baseline()
    allocation=next(a for a in CORE_BASELINE if a.code=='513500')
    assert allocation.name=='513500'
    names.remember({'513500':'标普500ETF'})
    assert allocation.name=='标普500ETF'


def test_fund_directory_is_separate_from_listed_names_and_preserves_ledger(tmp_path, monkeypatch):
    import pytest
    from data_provider import fund_names

    names.remember({"000001": "平安银行"})
    response = SimpleNamespace(
        text='var r = [["000001","HXCZHH","华夏成长混合"],["096001","DC","大成标普500等权重指数(QDII)A人民币"]];',
        raise_for_status=lambda: None,
    )
    calls = []
    def fetch(url, *, timeout):
        calls.append(url)
        assert timeout == (3, 5)
        return response
    monkeypatch.setattr(fund_names.requests, "get", fetch)
    monkeypatch.setattr(fund_names, "_last_attempt", float("-inf"))

    path = tmp_path / "etf_account.json"
    wallet.initialize(path, asof="2025-01-03", cash=1000,
                      positions=[dict(code="000001", count=100, price=1, strategy="core")],
                      budgets={"core": 1}, cash_allocations={"core": 1000})
    def record(**fields):
        wallet.record_event(path, {"revision": wallet.revision(wallet.load(path)),
                                   "date": "2025-01-06", **fields})
    record(request_id="existing-fund", side="fund_buy", code="000001", qty=10, amount=10)
    record(request_id="pending-fund", side="fund_pending", code="096001", amount=50, nav_date="2025-01-06",
           subscription_fee={"kind": "none", "value": 0})
    before = path.read_bytes()
    account = wallet.account_data(path)
    assert account["fund_names"] == {"000001": None, "096001": None}
    assert not calls
    with pytest.raises(ValueError, match="六位场外基金代码"):
        wallet.lookup_fund_names(path, {"codes": ["000001", "sh000001"]})
    result = wallet.lookup_fund_names(path, {"codes": ["000001", "096001", "000000"]})
    assert result == {"names": {"000001": "华夏成长混合", "096001": "大成标普500等权重指数(QDII)A人民币"}}
    assert wallet.lookup_fund_names(path, {"codes": ["000000"]}) == {"names": {}}
    assert calls == [fund_names.DIRECTORY_URL]

    updated = wallet.account_data(path)
    assert updated["positions"][0]["name"] == "平安银行"
    assert updated["fund_positions"]["000001"]["name"] == "华夏成长混合"
    assert updated["fund_pending"]["pending-fund"]["name"] == updated["trades"][0]["name"] == result["names"]["096001"]
    core = next(row for row in updated["strategies"] if row["id"] == "core")
    assert next(group for group in core["allocation_groups"] if group["id"] == "outside-fund:000001")["name"] == "华夏成长混合"
    for key in ("cash", "equity", "revision", "history"):
        assert updated[key] == account[key]
    assert path.read_bytes() == before
