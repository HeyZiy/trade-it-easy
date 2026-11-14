"""粘贴到聚宽策略回测。每个落在回测区间内的观察日于收盘后输出一张表。"""

from jqdata import *
import pandas as pd


ETF_CODES = [
    "512880.XSHG", "512800.XSHG", "512400.XSHG", "512200.XSHG",
    "515880.XSHG", "515220.XSHG", "159992.XSHE", "159611.XSHE",
]
DATES = ["2022-12-30", "2026-09-30"]


def probe(etf_codes, dates):
    """沿用 etf_pool.classify_etfs 的公告日和生效区间口径。"""
    last_date = max(dates)
    target = finance.FUND_INVEST_TARGET
    records = finance.run_query(query(
        target.code, target.pub_date, target.start_date, target.end_date,
        target.traced_index_name, target.traced_index_code,
    ).filter(target.code.in_(etf_codes), target.pub_date <= last_date))
    if records is None or records.empty:
        raise ValueError("FUND_INVEST_TARGET 没有返回记录，请检查数据权限和 ETF 代码")
    for field in ("pub_date", "start_date", "end_date"):
        records[field] = pd.to_datetime(records[field])

    output = []
    for day in dates:
        asof = pd.Timestamp(day)
        active = records[
            (records.pub_date <= asof) & (records.start_date <= asof)
            & (records.end_date.isna() | (records.end_date > asof))
        ].sort_values("pub_date").drop_duplicates("code", keep="last").set_index("code")
        for etf in etf_codes:
            result = dict(date=day, etf=etf, index_code="", index_name="",
                          members=None, sample="", status="")
            if etf not in active.index:
                result["status"] = "无生效映射"
            else:
                row = active.loc[etf]
                result["index_name"] = row.traced_index_name
                code = row.traced_index_code
                if pd.isna(code) or not str(code).strip():
                    result["status"] = "缺指数代码"
                else:
                    result["index_code"] = str(code).strip()
                    try:
                        members = get_index_stocks(result["index_code"], date=day)
                        members = [] if members is None else list(members)
                    except Exception as exc:
                        result["status"] = "%s: %s" % (type(exc).__name__, exc)
                    else:
                        result["members"] = len(members)
                        result["sample"] = ", ".join(members[:3])
                        result["status"] = "OK" if members else "成分为空"
            output.append(result)
    return pd.DataFrame(output)


def initialize(context):
    run_daily(probe_on_observation_day, time="after_close")


def probe_on_observation_day(context):
    today = context.current_dt.strftime("%Y-%m-%d")
    if today not in DATES:
        return
    result = probe(ETF_CODES, [today])
    log.info("\n" + result.to_string(index=False, na_rep="—"))
