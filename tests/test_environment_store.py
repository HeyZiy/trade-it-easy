# -*- coding: utf-8 -*-
"""environment 快照仓库单测：落盘/读取回路、schema 漂移显式失败、过期判定、fail-soft。"""
import json
from datetime import date, timedelta
from pathlib import Path

from src.market_state.environment import (
    EnvironmentView,
    GATE_STATE_LABELS,
    GateRead,
    GateVerdictView,
    gate_verdict_issue,
    load_environment,
    load_gate_verdict,
    save_environment,
)
from src.market_state.market_gate import GATE_STATE_PATHS, GateDiagnosis


def _diag(data_date="2026-09-27", gate_state="trending_up"):
    return GateDiagnosis(gate_state=gate_state, ma5=10.0, ma10=9.9, ma20=9.8, close=10.1,
                           bias_ma20=3.06, alignment="MA5>MA10>MA20（多头排列）",
                           path="②", note="", data_date=data_date)


def test_roundtrip(tmp_path: Path):
    f = tmp_path / "environment.json"
    save_environment(_diag(), path=f)

    view = load_environment(path=f)
    assert isinstance(view, EnvironmentView)
    assert view.data_date == "2026-09-27"
    assert view.gate_state.state == "trending_up"
    assert view.gate_state.available is True
    assert view.gate_state.bias_ma20 == 3.06


def test_gate_state_unavailable(tmp_path: Path):
    """指数数据不足：available=False，标签值仍透传（语义归消费方 policy）。"""
    f = tmp_path / "environment.json"
    save_environment(_diag(gate_state="chaos").__class__(gate_state="chaos", note="数据不足"), path=f)
    view = load_environment(path=f)
    assert view.gate_state.available is False
    assert view.gate_state.state == "chaos"


def test_stale_by_data_date(tmp_path: Path):
    f = tmp_path / "environment.json"
    old = (date.today() - timedelta(days=5)).isoformat()
    save_environment(_diag(data_date=old), path=f)
    view = load_environment(path=f)
    assert view.gate_state.is_stale() is True
    assert view.gate_state.age_days == 5

    fresh = date.today().isoformat()
    save_environment(_diag(data_date=fresh), path=f)
    assert load_environment(path=f).gate_state.is_stale() is False


def test_missing_file_returns_none(tmp_path: Path):
    assert load_environment(path=tmp_path / "nope.json") is None


def test_corrupt_file_returns_none(tmp_path: Path):
    f = tmp_path / "environment.json"
    f.write_text("{not json", encoding="utf-8")
    assert load_environment(path=f) is None


def test_schema_drift_fails_explicit(tmp_path: Path):
    """schema 漂移在此显式失败（缺 gate_state 键 → gate_state 视图为空对象而非静默半读）。"""
    f = tmp_path / "environment.json"
    f.write_text(json.dumps({"data_date": "2026-09-27"}), encoding="utf-8")
    view = load_environment(path=f)
    assert isinstance(view.gate_state, GateVerdictView)
    assert view.gate_state.state is None
    assert view.gate_state.available is False


def test_view_projects_full_diagnosis(tmp_path: Path):
    """GateVerdictView 投影完整 GateDiagnosis：均线值/命中路径可回放（报告明细用）。"""
    f = tmp_path / "environment.json"
    save_environment(_diag(), path=f)
    v = load_environment(path=f).gate_state
    assert (v.ma5, v.ma10, v.ma20, v.close) == (10.0, 9.9, 9.8, 10.1)
    assert v.path == "②"
    assert "MA5>MA10>MA20" in v.describe()
    assert "数据日期 2026-09-27" in v.describe()


def test_view_describe_unavailable(tmp_path: Path):
    f = tmp_path / "environment.json"
    save_environment(_diag(gate_state="chaos").__class__(gate_state="chaos", note="数据不足"),
                     path=f)
    v = load_environment(path=f).gate_state
    assert "均线数据不足" in v.describe()


# ── gate_verdict_issue：消费方新鲜度谓词单点 ──

def test_issue_fresh_verdict_returns_none(tmp_path: Path):
    f = tmp_path / "environment.json"
    save_environment(_diag(data_date=date.today().isoformat()), path=f)
    assert gate_verdict_issue(load_environment(path=f).gate_state) is None


def test_issue_missing_snapshot():
    assert "缺失" in gate_verdict_issue(None)


def test_issue_unavailable_verdict():
    v = GateVerdictView(state="chaos", available=False, data_date="2026-09-27",
                        alignment="", bias_ma20=None, note="数据不足")
    issue = gate_verdict_issue(v)
    assert issue is not None and "不可用" in issue


def test_issue_stale_verdict(tmp_path: Path):
    f = tmp_path / "environment.json"
    old = (date.today() - timedelta(days=5)).isoformat()
    save_environment(_diag(data_date=old), path=f)
    issue = gate_verdict_issue(load_environment(path=f).gate_state)
    assert issue is not None and "5 天未更新" in issue


# ── GateRead / load_gate_verdict：消费方读取单点 ──

def test_load_gate_verdict_fresh(tmp_path: Path):
    f = tmp_path / "environment.json"
    save_environment(_diag(data_date=date.today().isoformat()), path=f)
    gate = load_gate_verdict(path=f)
    assert isinstance(gate, GateRead)
    assert gate.issue is None
    assert gate.effective_state("chaos") == "trending_up"


def test_load_gate_verdict_missing_file(tmp_path: Path):
    gate = load_gate_verdict(path=tmp_path / "nope.json")
    assert gate.view is None
    assert "缺失" in gate.issue
    assert gate.effective_state("chaos") == "chaos"


def test_load_gate_verdict_stale_falls_back(tmp_path: Path):
    f = tmp_path / "environment.json"
    old = (date.today() - timedelta(days=5)).isoformat()
    save_environment(_diag(data_date=old), path=f)
    gate = load_gate_verdict(path=f)
    assert gate.view is not None and gate.view.state == "trending_up"
    assert gate.issue is not None
    assert gate.effective_state("chaos") == "chaos"


# ── 状态展示名自描述（GATE_STATE_LABELS / view.state_label） ──

def test_state_labels_and_paths_keysets_agree():
    """展示名与命中路径是 5 态的两张衍生表，键集合漏态必须红。"""
    assert set(GATE_STATE_LABELS) == set(GATE_STATE_PATHS)


def test_state_label_known_and_unknown():
    v = GateVerdictView(state="trending_up", available=True, data_date="2026-09-27",
                        alignment="", bias_ma20=None, note="")
    assert v.state_label.startswith("📈")
    bad = GateVerdictView(state="whatever", available=True, data_date="2026-09-27",
                          alignment="", bias_ma20=None, note="")
    assert bad.state_label == "❓ 状态不明"
