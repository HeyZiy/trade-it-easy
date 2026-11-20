"""场外基金单位净值，按天天基金历史页的报价口径读取。"""

from datetime import date
import math

import requests

HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"


def latest_fund_nav(code: str, through: str) -> tuple[str, float] | None:
    """取截止日期内最新的有效单位净值。"""
    through = date.fromisoformat(through).isoformat()
    response = requests.get(
        HISTORY_URL,
        params={"fundCode": code, "endDate": through, "pageIndex": 1, "pageSize": 10},
        headers={"Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html", "User-Agent": "Mozilla/5.0"},
        timeout=(3, 5),
    )
    response.raise_for_status()
    payload = response.json()
    if payload["ErrCode"] != 0:
        raise ValueError(f"天天基金历史净值查询失败（{payload['ErrCode']}）")
    data = payload["Data"]
    if not data or not data["FundType"]:
        return None

    # 历史页对 004、005 默认在 DWJZ 展示收益，特征 200 对应单位净值。
    fund_type = data["FundType"]
    features = (data["Feature"] or "").split(",")
    reports_income = fund_type in {"004", "005"} and "200" not in features
    if reports_income and (fund_type != "005" or data["SYType"] != "每万份收益"):
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
