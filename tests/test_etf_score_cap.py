"""Held-cap exception must never become a higher entry threshold or delayed rank exit."""
import importlib.util
import math
from pathlib import Path
import sys
import types

import numpy as np
import pandas as pd
import pytest

from research.studies.etf_rotation.study_score_cap import target_for
from src.etf.momentum import momentum_score


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', types.ModuleType('jqdata'))
    path = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation/study_score_cap_jq.py'
    spec = importlib.util.spec_from_file_location('score_cap_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('held_score,new_score,old,expected', [
    (5., 2., True, 'old'), (5.1, 2., False, 'new'),
    (0., 2., True, 'new'), (-1., 2., True, 'new'),
    (float('nan'), 2., True, 'new'), (4., 4.5, True, 'new'),
    (5.1, 6., False, None),
])
def test_entry_cap_and_rank_exit_are_preserved(platform, held_score, new_score, old, expected):
    a,b = platform.POOL[:2]
    scores = {a: held_score, b: new_score}
    target = platform.choose_target(scores, a if old else None, hold_cap=True)
    assert target == {'old':a, 'new':b, None:None}[expected]
    day = pd.DataFrame([
        dict(code=a,score=held_score,eligible=int(math.isfinite(held_score) and 0<held_score<5),rank=2,paused=0,decision_price=1.),
        dict(code=b,score=new_score,eligible=int(0<new_score<5),rank=1,paused=0,decision_price=1.),
    ]).set_index('code')
    assert target_for(day,a if old else None,True) == target


@pytest.mark.parametrize('slope,last_ratio',[(.001,1.),(.01,1.),(-.002,1.),(.001,.94)])
def test_platform_score_matches_production_formula(platform,slope,last_ratio):
    prices=np.exp(np.arange(26)*slope)
    prices[-1]*=last_ratio
    assert platform.score_of(prices[:-1],prices[-1]) == pytest.approx(momentum_score(prices[:-1],prices[-1]),abs=1e-12)


def test_baseline_still_exits_at_five(platform):
    a,b=platform.POOL[:2]
    assert platform.choose_target({a:5.,b:2.},a,False)==b
