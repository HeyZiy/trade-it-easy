"""导出投资账户的生效配置和持仓快照。"""

from io import BytesIO
import math

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from data_provider.security_names import security_name
from src.account_advice import allocation_groups, strategy_facts
from src.account_store import load
from src.etf_account import valuation_day, valued_holdings
from src.json_store import locked
from src.portfolio_config import ALLOCATION_NAMES, OWNER_NAMES, fund_only
from src.trading_calendar import SHANGHAI


STRATEGIES = OWNER_NAMES
ASSET_NAMES = {"equity": "权益", "bond": "债券", "commodity": "商品", "cash": "现金类",
               "mixed": "混合", "unknown": "待分类", "gold": "黄金"}
HOLDING_NAMES = {"etf": "ETF", "stock": "股票", "other": "其他场内证券", "fund": "场外基金"}
AMOUNT = "#,##0.00"
PERCENT = "0.00%"
SHARES = "#,##0.######"
PRICE = "0.00######"
HOLDING_COLUMNS = ["代码", "名称", "产品类型", "资产类别", "数量或份额",
                   "收盘价或单位净值 / 元", "价格日期", "市值 / 元", "占账户总资产", "估值状态"]


def _table(book, title, headers, rows, formats=None):
    sheet = book.create_sheet(title)
    sheet.append(headers)
    for row, values in enumerate(rows, 2):
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row, column)
            if isinstance(value, str):
                cell.value = ILLEGAL_CHARACTERS_RE.sub("", value)
                cell.data_type = "s"  # 文本列保留前导零，名称按字面值写入。
            else:
                cell.value = value
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="235743")
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        width = max(sum(1 if char.isascii() else 2 for char in str(cell.value or "")) for cell in column)
        sheet.column_dimensions[letter].width = min(58, max(12, width + 3))
        if formats and (number_format := formats.get(column[0].value)):
            for cell in column[1:]:
                cell.number_format = number_format
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    return sheet


def _sum_values(values):
    values = list(values)
    return round(math.fsum(values), 2) if None not in values else None


def _share(value, equity):
    return value / equity if equity and value is not None else None


def _holding_row(row, product_type, category, equity, price_day):
    fund = product_type == "fund"
    price = row.get("nav" if fund else "close")
    value = row["market_value"]
    return [row["code"], row.get("name") or row["code"], HOLDING_NAMES[product_type],
            ASSET_NAMES[category], row["qty" if fund else "count"], price,
            (row["date"] if fund else price_day) if price is not None else None,
            value, _share(value, equity), "已估值" if value is not None else "待估值"]


def _holdings(book, view, price_day):
    equity = view["equity"]
    listed, funds = view["positions"], view["fund_positions"]
    formats = {"数量或份额": SHARES, "收盘价或单位净值 / 元": PRICE, "市值 / 元": AMOUNT,
               "占账户总资产": PERCENT}
    account_rows = [_holding_row(row, row["kind"], row["asset_class"], equity, price_day) for row in listed]
    account_rows.extend(_holding_row(row, "fund", row["asset_class"], equity, price_day)
                        for row in funds.values())
    _table(book, "账户持仓", HOLDING_COLUMNS, sorted(account_rows, key=lambda row: row[0]), formats)

    listed_by_code = {row["code"]: row for row in listed}
    strategy_rows = []
    for row in view["strategy_positions"]:
        asset = listed_by_code[row["code"]]
        strategy_rows.append([STRATEGIES[row["strategy"]],
                              *_holding_row(row, asset["kind"], asset["asset_class"], equity, price_day)])
    for strategy, positions in view["strategy_fund_positions"].items():
        for code, row in positions.items():
            strategy_rows.append([STRATEGIES[strategy],
                                  *_holding_row(row, "fund", funds[code]["asset_class"], equity, price_day)])
    _table(book, "策略持仓", ["所属策略", *HOLDING_COLUMNS],
           sorted(strategy_rows, key=lambda row: (row[0], row[1])), formats)


def _configuration(book, state, view, names):
    config = state["_config"]
    budgets = config["budgets"]
    equity = view["equity"]
    strategies = {key: strategy_facts(state, view, key) for key in OWNER_NAMES}
    strategy_rows = [
        [row["name"], row["residual_pct"] if row["residual_pct"] is not None else row["budget_pct"],
         _share(row["assets"], equity), row["assets"], row["cash"],
         row["pending_fund_value"]]
        for row in strategies.values()
    ]
    _table(book, "策略配比", ["策略", "目标占账户", "实际占账户", "策略总资产 / 元",
                          "账面现金 / 元", "申购在途 / 元"], strategy_rows,
           {"目标占账户": PERCENT, "实际占账户": PERCENT, "策略总资产 / 元": AMOUNT,
            "账面现金 / 元": AMOUNT, "申购在途 / 元": AMOUNT})

    allocation_rows = []
    for key, strategy_name in ALLOCATION_NAMES.items():
        assets = config.get(key, ())
        groups = {group["id"]: group for group in allocation_groups(assets, strategies[key], view["prices"], names)
                  if not group.get("outside")}
        budget = budgets.get(key)
        for asset in assets:
            code, weight = asset["code"], asset["weight"]
            group = groups[code]
            listed = [code, *asset.get("etf_codes", ())] if code != "CASH" and not fund_only(asset) else []
            allocation_rows.append([
                strategy_name, code, group["name"], ASSET_NAMES[asset["asset_type"]],
                "、".join(listed), "、".join(asset.get("fund_codes", ())), weight,
                weight * budget if budget is not None else None, group["market_value"],
                asset["tracked_index"], {"none": "无", "csindex": "中证", "overseas": "海外"}[asset["valuation_kind"]],
                "是" if asset["dividend_style"] else "否", asset["volatility_rank"],
            ])
    _table(book, "组合配置", ["策略", "配置代码", "配置名称", "资产类别", "场内代码", "场外代码",
                          "目标占策略", "目标占账户", "当前金额（含该组在途） / 元", "跟踪指数代码",
                          "估值来源", "股息估值", "减仓顺序"], allocation_rows,
           {"目标占策略": PERCENT, "目标占账户": PERCENT, "当前金额（含该组在途） / 元": AMOUNT})


def _fees(book, config):
    rows = []
    fees = config.get("trade_fees", {})
    for kind, name in (("stock", "股票"), ("etf", "ETF")):
        if kind in fees:
            rows.extend([(f"{name}佣金率", fees[kind]["commission_per_10000"], "万分之"),
                         (f"{name}最低佣金", fees[kind]["min_commission"], "元")])
    if "stock_sell_stamp_duty_per_10000" in fees:
        rows.append(("A 股卖出印花税率", fees["stock_sell_stamp_duty_per_10000"], "万分之"))
    if fund_fee := config.get("fund_subscription_fee"):
        units = {"rate": "%", "fixed": "元", "none": "免申购费"}
        rows.append(("场外基金统一申购费", fund_fee["value"], units[fund_fee["kind"]]))
    _table(book, "费用配置", ["项目", "数值", "单位或说明"], rows)


def export_workbook(path, *, now):
    now = now.astimezone(SHANGHAI)
    with locked(path):
        state = load(path)
    day = now.date()
    price_day = valuation_day(day, now=now)
    view, names = valued_holdings(state, day, price_day)
    book = Workbook()
    book.remove(book.active)
    summary = [
        ("导出时间（北京时间）", now.isoformat(timespec="seconds")),
        ("持仓日期", day.isoformat()),
        ("场内估值日期", price_day),
        ("估值状态", "完整" if view["equity"] is not None else "部分持仓待估值"),
        ("总资产 / 元", view["equity"]),
        ("账面现金 / 元", view["cash"]),
        ("场内证券市值 / 元", _sum_values(row["market_value"] for row in view["positions"])),
        ("场外基金市值 / 元", _sum_values(row["market_value"] for row in view["fund_positions"].values())),
        ("待确认申购 / 元", view["pending_fund_value"]),
        ("对账口径", "总资产为账面现金、场内市值、场外基金市值和待确认申购之和。"),
        ("估值口径", "场内使用已采集的收盘价，场外使用各基金已登记的单位净值，日期见持仓表。"),
        ("空白数值", "价格、市值或占比空白表示无法估值或计算。目标权重空白表示未配置。"),
    ]
    summary.extend((f"{ASSET_NAMES[category]}资产 / 元", value)
                   for category, value in view["asset_allocation"].items())
    sheet = _table(book, "账户摘要", ["项目", "值"], summary)
    for cell in sheet["B"]:
        if cell.data_type == "n":
            cell.number_format = AMOUNT
    _configuration(book, state, view, names)
    _holdings(book, view, price_day)
    _table(book, "待确认申购", ["策略", "基金代码", "基金名称", "买入日", "记账日期", "扣款金额 / 元"],
           [(STRATEGIES[row["strategy"]], row["code"], row.get("name") or row["code"],
             row["nav_date"], row["date"], row["amount"]) for row in view["fund_pending"].values()],
           {"扣款金额 / 元": AMOUNT})
    config = state["_config"]
    _table(book, "轮动标的池", ["代码", "名称", "资产分组"],
           [(row["code"], security_name(row["code"]), row["group"])
            for row in config.get("rotation", {}).get("pool", ())])
    _fees(book, config)
    with BytesIO() as stream:
        book.save(stream)
        return stream.getvalue()
