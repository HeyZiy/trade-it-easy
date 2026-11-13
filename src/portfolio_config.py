"""投资账户的唯一生效配置。手工编辑和网页保存使用同一校验规则。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from src.json_store import locked, save

STRATEGY_NAMES = {"core": "多资产配置", "rotation": "跨资产动量", "quality": "质量动量"}

FILENAME = "etf_config.json"
DEFAULT_PATH = Path("data") / FILENAME
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "etf_config.example.json"
FUND_PREFIX = "FUND:"


def fund_only(row):
    return row["code"].startswith(FUND_PREFIX)


def grouped(row):
    return fund_only(row) or bool(row.get("etf_codes") or row.get("fund_codes"))


def path_for(account):
    account = Path(account)
    # 生产是一份投资账户；独立测试账户各自隔离配置。
    return account.with_name(FILENAME if account.name == "etf_account.json" else account.stem + ".config.json")


def revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_subscription_fee(fee):
    if not isinstance(fee, dict) or set(fee) != {"kind", "value"}:
        raise ValueError("申购费用设置无效")
    kind, value = fee["kind"], fee["value"]
    if kind not in ("none", "rate", "fixed") or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("申购费用设置无效")
    if kind == "none" and value != 0:
        raise ValueError("免申购费的金额须为零")
    return {"kind": kind, "value": value}


def _validate_trade_fees(fees):
    stamp_duty = "stock_sell_stamp_duty_per_10000"
    if not isinstance(fees, dict) or fees.keys() - {"stock", "etf", stamp_duty}:
        raise ValueError("场内交易费率设置无效")
    for kind in ("stock", "etf"):
        if kind not in fees:
            continue
        rule = fees[kind]
        if not isinstance(rule, dict) or rule.keys() != {"commission_per_10000", "min_commission"}:
            raise ValueError("请同时设置佣金率和最低佣金")
        rate, minimum = rule["commission_per_10000"], rule["min_commission"]
        if type(rate) not in (int, float) or not 0 <= rate <= 10000:
            raise ValueError("佣金率须在万分之0至10000之间")
        if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 0 or round(minimum, 2) != minimum:
            raise ValueError("最低佣金须为非负金额，最多两位小数")
    if stamp_duty in fees:
        rate = fees[stamp_duty]
        if "stock" not in fees or type(rate) not in (int, float) or not 0 <= rate <= 10000:
            raise ValueError("请先设置股票佣金，印花税率须在万分之0至10000之间")


def validate(config):
    required = {"schema_version", "budgets", "core"}
    if not isinstance(config, dict) or not required <= config.keys() <= required | {"fund_subscription_fee", "trade_fees"}:
        raise ValueError("配置须包含 schema_version、budgets、core")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("不支持的配置版本")
    if "fund_subscription_fee" in config:
        validate_subscription_fee(config["fund_subscription_fee"])
    if "trade_fees" in config:
        _validate_trade_fees(config["trade_fees"])
    budgets = config["budgets"]
    if not isinstance(budgets, dict) or set(budgets) - STRATEGY_NAMES.keys():
        raise ValueError("策略目标比例无效")
    valid_weight = lambda value: type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    if any(not valid_weight(v) for v in budgets.values()) or sum(budgets.values()) > 1 + 1e-10:
        raise ValueError("策略目标比例合计不能超过100%")
    rows = config["core"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("多资产配置须有1至50项资产")
    used_listed, used_funds = set(), set()
    required_fields = {"code", "weight", "asset_type", "tracked_index", "valuation_kind", "dividend_style", "volatility_rank"}
    allowed_fields = required_fields | {"label", "etf_codes", "fund_codes"}
    for row in rows:
        if not isinstance(row, dict) or not required_fields <= row.keys() <= allowed_fields:
            raise ValueError("资产配置字段不完整或包含未知字段")
        code = row["code"]
        if not isinstance(code, str) or not (code == "CASH" or re.fullmatch(r"(?:FUND:)?[0-9]{6}", code)):
            raise ValueError("请输入六位产品代码")
        label = row.get("label", "")
        if not isinstance(label, str) or len(label) > 40 or ("label" in row and not label.strip()):
            raise ValueError("配置名称须为1至40个字符")
        listed, funds = row.get("etf_codes", []), row.get("fund_codes", [])
        for members in (listed, funds):
            if not isinstance(members, list) or len(members) > 20 or any(
                    not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}", c) for c in members):
                raise ValueError("每组最多填写20个同类成员，产品代码须为六位数字")
        if code == "CASH" and (listed or funds or label):
            raise ValueError("现金不能绑定产品")
        if fund_only(row) and (not funds or code != FUND_PREFIX + funds[0] or listed
                              or row["valuation_kind"] != "none" or row["dividend_style"]):
            raise ValueError("仅场外配置须按基金代码建组，估值使用基金公布的单位净值")
        for members, used in (([code, *listed], used_listed), (funds, used_funds)):
            if len(set(members)) != len(members) or used.intersection(members):
                raise ValueError("同一产品只能归入一个配置，且组内不能重复")
            used.update(members)
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
    if "CASH" not in used_listed or not math.isclose(sum(r["weight"] for r in rows), 1, rel_tol=0, abs_tol=1e-8):
        raise ValueError("多资产配置内部权重须合计100%，并包含一项CASH")
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
    return [AssetAllocation(
        code=r["code"], asset_type=r["asset_type"], neutral_weight=r["weight"],
        volatility_rank=r["volatility_rank"], tracked_index=r["tracked_index"],
        valuation_kind=r["valuation_kind"], dividend_style=r["dividend_style"],
    ) for r in config["core"] if not fund_only(r)]


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
