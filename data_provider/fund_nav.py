"""场外基金单位净值，按天天基金历史页的报价口径读取。"""

from datetime import date
import math

import requests

HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"


def _history(code, **query):
    response = requests.get(
        HISTORY_URL,
        params={"fundCode": code, **query},
        headers={"Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html", "User-Agent": "Mozilla/5.0"},
        timeout=(3, 5),
    )
    response.raise_for_status()
    payload = response.json()
    if payload["ErrCode"] != 0:
        raise ValueError(f"天天基金历史净值查询失败（{payload['ErrCode']}）")
    return payload


def _reports_income(data):
    # 历史页对 004、005 默认在 DWJZ 展示收益，特征 200 对应单位净值。
    return data["FundType"] in {"004", "005"} and "200" not in (data["Feature"] or "").split(",")


def latest_fund_nav(code: str, through: str) -> tuple[str, float] | None:
    """取截止日期内最新的有效单位净值。"""
    through = date.fromisoformat(through).isoformat()
    data = _history(code, endDate=through, pageIndex=1, pageSize=10)["Data"]
    if not data or not data["FundType"]:
        return None

    reports_income = _reports_income(data)
    if reports_income and (data["FundType"] != "005" or data["SYType"] != "每万份收益"):
        return None

    latest = None
    for row in data["LSJZList"] or ():
        try:
            day = date.fromisoformat(row["FSRQ"]).isoformat()
            value = float(row["DWJZ"])
        except (TypeError, ValueError):
            continue
        if day > through or not math.isfinite(value):
            continue
        nav = 1.0 if reports_income else value
        if 0 < nav <= 1e8 and round(nav, 8) == nav and (latest is None or day > latest[0]):
            latest = (day, nav)
    return latest


def fund_daily_returns(code, start, end, *, money_fund=False):
    """基金披露的净值日增长率，或固定净值货基的万份收益。"""
    results = {}
    page, received = 1, 0
    while True:
        payload = _history(code, startDate=start, endDate=end, pageIndex=page, pageSize=100)
        data = payload["Data"]
        if not data or not data["LSJZList"]:
            return results
        income = _reports_income(data)
        if money_fund:
            if not income or data["FundType"] != "005" or data["SYType"] != "每万份收益":
                raise ValueError(f"{code} 未按万份收益披露")
            field, divisor = "DWJZ", 10_000
        else:
            if income:
                raise ValueError(f"{code} 未披露净值日增长率")
            field, divisor = "JZZZL", 100
        rows = data["LSJZList"]
        for row in rows:
            try:
                day = date.fromisoformat(row["FSRQ"]).isoformat()
                daily = float(row[field]) / divisor
            except (TypeError, ValueError):
                continue
            if start <= day <= end and math.isfinite(daily) and daily > -1:
                results[day] = daily
        received += len(rows)
        if received >= payload["TotalCount"]:
            return results
        page += 1
