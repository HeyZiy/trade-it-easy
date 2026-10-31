"""ETF账户的唯一生效配置。手工编辑和网页保存使用同一校验规则。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from src.json_store import locked, save

FILENAME = "etf_config.json"
DEFAULT_PATH = Path("data") / FILENAME
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "etf_config.example.json"


def path_for(account):
    account = Path(account)
    # 生产是一份ETF账户；独立测试账户各自隔离配置。
    return account.with_name(FILENAME if account.name == "etf_account.json" else account.stem + ".config.json")


def revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate(config):
    if not isinstance(config, dict) or set(config) != {"schema_version", "budgets", "core"}:
        raise ValueError("配置须包含 schema_version、budgets、core")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("不支持的配置版本")
    budgets = config["budgets"]
    if not isinstance(budgets, dict) or set(budgets) - {"core", "rotation"}:
        raise ValueError("策略目标比例无效")
    valid_weight = lambda value: type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    if any(not valid_weight(v) for v in budgets.values()) or sum(budgets.values()) > 1 + 1e-10:
        raise ValueError("策略目标比例合计不能超过100%")
    rows = config["core"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("核心配置须有1至50项资产")
    codes = set()
    fields = {"code", "weight", "asset_type", "tracked_index", "valuation_kind", "dividend_style", "volatility_rank"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("资产配置字段不完整或包含未知字段")
        code = row["code"]
        if not isinstance(code, str) or not (code == "CASH" or re.fullmatch(r"[0-9]{6}", code)) or code in codes:
            raise ValueError("资产代码须为6位数字或CASH，且不能重复")
        codes.add(code)
        if not valid_weight(row["weight"]):
            raise ValueError("资产权重须在0至100%之间")
        if row["asset_type"] not in {"equity", "bond", "gold", "cash"}:
            raise ValueError("资产类别无效")
        if (code == "CASH") != (row["asset_type"] == "cash"):
            raise ValueError("现金资产须使用CASH代码")
        if row["valuation_kind"] not in {"none", "csindex", "overseas"}:
            raise ValueError("估值来源无效")
        index = row["tracked_index"]
        if row["valuation_kind"] == "csindex":
            if not isinstance(index, str) or not re.fullmatch(r"[0-9]{6}", index):
                raise ValueError("中证估值须填写6位跟踪指数代码")
        elif index is not None:
            raise ValueError("仅中证估值填写跟踪指数代码")
        if type(row["dividend_style"]) is not bool:
            raise ValueError("股息估值标记须为布尔值")
        if type(row["volatility_rank"]) is not int or not 0 <= row["volatility_rank"] <= 100:
            raise ValueError("减仓顺序须为0至100的整数")
        if code == "CASH" and (row["valuation_kind"] != "none" or row["dividend_style"]):
            raise ValueError("现金不设置证券估值")
    if "CASH" not in codes or not math.isclose(sum(r["weight"] for r in rows), 1, rel_tol=0, abs_tol=1e-8):
        raise ValueError("核心内部权重须合计100%，并包含一项CASH")
    return deepcopy(config)


def _read(path):
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取账户配置 {Path(path).name}：{error}") from error


def _archive(path, config):
    history = path.with_suffix(".history")
    history.mkdir(parents=True, exist_ok=True)
    version = history / (revision(config) + ".json")
    if not version.exists():
        save(version, config)


def load(path=None):
    path = Path(path or DEFAULT_PATH)
    with locked(path):
        config = _read(path)
        _archive(path, config)
        return config


def current_revision(path):
    return revision(_read(path))


def create(path, config):
    path = Path(path)
    config = validate(config)
    with locked(path):
        if path.exists():
            raise ValueError("账户配置已存在，禁止覆盖初始化")
        _archive(path, config)
        save(path, config)


def save_config(path, config, expected):
    path = Path(path)
    config = validate(config)
    with locked(path):
        old = _read(path)
        if revision(old) != expected:
            raise ValueError("配置已被其他页面或手工修改，请刷新后重试")
        _archive(path, old)
        _archive(path, config)
        if current_revision(path) != expected:
            raise ValueError("配置已变化，请刷新后重试")
        save(path, config)


def example():
    return _read(EXAMPLE)


def from_legacy(state):
    # 保留实际生效的完整精度，而不是用Markdown显示的四舍五入数字覆盖。
    metadata = {r["code"]: r for r in example()["core"]}
    rows = []
    for code, weight in state["targets"].items():
        row = deepcopy(metadata.get(code, {"code": code, "weight": 0, "asset_type": "equity",
            "tracked_index": None, "valuation_kind": "none", "dividend_style": False, "volatility_rank": 0}))
        row["weight"] = weight
        row["volatility_rank"] = 0  # 旧账户的baseline未设置优先级，迁移保持原排序。
        rows.append(row)
    return validate({"schema_version": 1, "budgets": state.get("budgets", {}), "core": rows})


def migrate(account):
    """显式、可重复迁移。先创建配置，再移除台账副本，异常中断后可以重试。"""
    account = Path(account)
    config_path = path_for(account)
    with locked(account):
        state = json.loads(account.read_text(encoding="utf-8"))
        if config_path.exists():
            config = load(config_path)
        else:
            config = from_legacy(state)
            create(config_path, config)
        if "targets" in state or "budgets" in state:
            # 已存在的新配置优先，绝不以旧台账覆盖网页修改。
            state.pop("targets", None)
            state.pop("budgets", None)
            save(account, state)
        return config


def allocations(config):
    from src.etf.config import AssetAllocation
    return [AssetAllocation(r["code"], r["asset_type"], r["weight"], r["volatility_rank"]) for r in config["core"]]


def data(account):
    path = path_for(account)
    config = load(path)
    versions = sorted(path.with_suffix(".history").glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    history = []
    for version in versions[:20]:
        value = _read(version)
        if version.stem != revision(value):
            continue
        history.append({"revision": version.stem, "saved_at": datetime.fromtimestamp(version.stat().st_mtime, timezone.utc).isoformat(), "config": value})
    return {"config": config, "revision": revision(config), "history": history}


def update(account, payload):
    with locked(account):
        save_config(path_for(account), payload["config"], payload.get("revision"))
