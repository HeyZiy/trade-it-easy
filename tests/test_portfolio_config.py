from copy import deepcopy
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import etf_account as wallet, portfolio_config as config


@pytest.fixture
def account(tmp_path):
    path=tmp_path/'etf_account.json'
    wallet.initialize(path,asof='2025-01-03',cash=1000,
                      positions=[dict(code='512010',count=100,price=2,strategy='core')],
                      targets={'512010':.5},budgets={'core':.8,'rotation':.1},cash_allocations={'core':1000})
    return path


def test_only_config_file_stores_current_weights_and_budget(account):
    raw=json.loads(account.read_text(encoding='utf-8'))
    assert 'targets' not in raw and 'budgets' not in raw
    c=config.load(config.path_for(account))
    assert c['budgets']=={'core':.8,'rotation':.1}
    assert c['core'][0]['code']=='512010' and c['core'][0]['weight']==.5


def test_editing_targets_preserves_cash_shares_pnl_and_history(account):
    before=wallet.account_data(account);raw=account.read_bytes()
    c=config.data(account)
    changed=deepcopy(c['config'])
    changed['core'][0]['weight']=.3
    changed['core'][1]['weight']=.7
    changed['budgets']['core']=.7
    config.update(account,{'revision':c['revision'],'config':changed})
    after=wallet.account_data(account)
    for key in ('cash','equity','positions','pnl','history','cash_by_strategy'):
        assert after[key]==before[key]
    assert account.read_bytes()==raw
    assert after['strategies'][0]['budget']==840
    assert after['strategies'][0]['target_weights']['512010']==.3


@pytest.mark.parametrize('mutation',[
    lambda c:c['core'][0].update(weight=.2),
    lambda c:c['core'][0].update(code='123'),
    lambda c:c['core'][0].update(code='CASH'),
    lambda c:c['core'][0].update(weight=float('nan')),
    lambda c:c['core'][0].update(tracked_index='bad'),
    lambda c:c['budgets'].update(rotation=.8),
])
def test_invalid_configs_do_not_replace_valid_one(account,mutation):
    current=config.data(account);before=config.path_for(account).read_bytes()
    draft=deepcopy(current['config']);mutation(draft)
    with pytest.raises(ValueError):config.update(account,{'revision':current['revision'],'config':draft})
    assert config.path_for(account).read_bytes()==before


def test_stale_page_and_concurrent_saves_do_not_lose_edits(account):
    current=config.data(account)
    def update(core_weight):
        c=deepcopy(current['config']);c['budgets']['core']=core_weight
        try:config.update(account,{'revision':current['revision'],'config':c});return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update,[.6,.7]))==[False,True]


def test_manual_edit_is_validated_and_invalidates_old_page(account):
    current=config.data(account)
    changed=deepcopy(current['config']);changed['budgets']['core']=.6
    config.path_for(account).write_text(json.dumps(changed),encoding='utf-8')
    assert wallet.account_data(account)['strategies'][0]['budget']==720
    with pytest.raises(ValueError,match='修改'):
        config.update(account,{'revision':current['revision'],'config':current['config']})
    changed['core'][0]['weight']=2
    config.path_for(account).write_text(json.dumps(changed),encoding='utf-8')
    with pytest.raises(ValueError):wallet.account_data(account)


def test_history_can_restore_and_removed_asset_remains_held(account):
    current=config.data(account)
    changed=deepcopy(current['config'])
    changed['core']=[r for r in changed['core'] if r['code']=='CASH']
    changed['core'][0]['weight']=1
    config.update(account,{'revision':current['revision'],'config':changed})
    view=wallet.account_data(account)['strategies'][0]
    assert view['positions'][0]['code']=='512010' and '512010' not in view['target_weights']
    history=config.data(account)
    old=next(h for h in history['history'] if h['revision']==current['revision'])
    config.update(account,{'revision':history['revision'],'config':old['config']})
    assert config.load(config.path_for(account))==current['config']


def test_metadata_changes_take_effect_without_restart(account,monkeypatch):
    from src.etf.config import asset_settings
    monkeypatch.setattr(config,'DEFAULT_PATH',config.path_for(account))
    assert asset_settings('512010')['tracked_index']=='000913'
    current=config.data(account);changed=deepcopy(current['config'])
    changed['core'][0]['tracked_index']='000922'
    config.update(account,{'revision':current['revision'],'config':changed})
    assert asset_settings('512010')['tracked_index']=='000922'


def test_legacy_migration_is_exact_and_never_overwrites_new_config(tmp_path):
    path=tmp_path/'etf_account.json'
    legacy={'initial':{'date':'2025-01-03','cash':1000,'equity':1200,
                      'positions':[{'code':'512010','name':'旧名称','count':100,'price':2,'strategy':'core'}]},
            'events':[],'records':[],'dismissed':[],'targets':{'512010':.123456789,'CASH':.876543211},'budgets':{'core':.83,'rotation':.07}}
    path.write_text(json.dumps(legacy),encoding='utf-8')
    c=config.migrate(path)
    assert c['core'][0]['weight']==.123456789
    assert 'targets' not in json.loads(path.read_text())
    before=wallet.account_data(path)
    raw=path.read_bytes();config.migrate(path)
    assert path.read_bytes()==raw
    assert wallet.account_data(path)['equity']==before['equity']==1200
