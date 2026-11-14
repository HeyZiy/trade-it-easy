"""投资账户的文件读写和版本校验。"""

import hashlib
import json
from pathlib import Path

from src import portfolio_config
from src.json_store import save as _json_save

FILENAME = "etf_account.json"
ACCOUNT_PATH = Path("data") / FILENAME


def save(path, state):
    if "_config_revision" in state:
        if portfolio_config.current_revision(portfolio_config.path_for(path)) != state["_config_revision"]:
            raise ValueError("配置已更新，请刷新后重试")
        state = {k: v for k, v in state.items() if k not in {"targets", "budgets", "_config", "_config_revision"}}
    _json_save(path, state)


def load(path=ACCOUNT_PATH):
    if not Path(path).exists():
        raise FileNotFoundError("投资账户尚未初始化")
    config = portfolio_config.load(portfolio_config.path_for(path))
    return read_account(path, config)


def read_account(path, config):
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    state.update(targets={r["code"]: r["weight"] for r in config["core"]},
                 budgets=config["budgets"], _config=config,
                 _config_revision=portfolio_config.revision(config))
    return state


def revision(state, asof=None):
    facts = {"initial": state["initial"], "events": [e for e in state["events"]
             if asof is None or (e["date"] <= asof and e["side"] not in {"assign", "cash_assign", "cash_transfer"})]}
    if asof is None:
        facts["budgets"] = state.get("budgets", {})
        facts["targets"] = state.get("targets", {})
        if "_config_revision" in state:
            facts["configuration"] = state["_config_revision"]
        for key in ("quality_signal", "quality_state"):
            if key in state:
                facts[key] = state[key]
        if "rotation_signal" in state:
            facts["rotation_signal"] = state["rotation_signal"]
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
