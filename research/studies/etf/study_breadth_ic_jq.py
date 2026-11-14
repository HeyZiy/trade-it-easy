"""粘贴到聚宽策略回测。每 20 个交易日计算行业/宽基股票指数广度，不下单。"""

from jqdata import *
import re

import numpy as np
import pandas as pd


SAMPLE_EVERY = 20
HORIZONS = (20, 60)
MIN_AMOUNT = 30000000
MIN_COVERAGE = 0.80
MIN_MEMBERS = 10
MIN_IC = 20
BATCH_SIZE = 100
A_STOCK = re.compile(r"^(?:6\d{5}\.XSHG|[03]\d{5}\.XSHE|[489]\d{5}\.XBSE)$")


def initialize(context):
    set_option("avoid_future_data", True)
    g.day = -1
    g.pending = []
    g.ic_rows = []
    g.written = set()
    for name in ("coverage", "observations", "daily", "summary"):
        write_file("etf_breadth_ic_" + name + ".csv", "", append=False)
    run_daily(observe, time="after_close")


def tracked_indices(codes, today):
    target = finance.FUND_INVEST_TARGET
    records = []
    for offset in range(0, len(codes), 50):
        batch = finance.run_query(query(target).filter(
            target.code.in_(codes[offset:offset + 50]), target.pub_date <= today
        ))
        if batch is not None and not batch.empty:
            records.append(batch)
    if not records:
        return {}
    mapping = pd.concat(records, ignore_index=True)
    date = pd.Timestamp(today)
    for field in ("pub_date", "start_date", "end_date"):
        mapping[field] = pd.to_datetime(mapping[field])
    active = mapping[(mapping.pub_date <= date) & (mapping.start_date <= date)
                     & (mapping.end_date.isna() | (mapping.end_date > date))]
    current = active.sort_values("pub_date").drop_duplicates("code", keep="last")
    return {
        row.code: (str(row.traced_index_code).strip().upper(),
                   str(row.traced_index_name) if pd.notna(row.traced_index_name) else "")
        for row in current.itertuples()
        if pd.notna(row.traced_index_code) and str(row.traced_index_code).strip()
    }


def quote_panels(codes, dates, fields, fq="post", tradable=False):
    codes = sorted(set(codes))
    panels = {field: [] for field in fields}
    request_fields = list(fields) + (["paused"] if tradable else [])
    for offset in range(0, len(codes), BATCH_SIZE):
        batch = codes[offset:offset + BATCH_SIZE]
        bars = get_price(batch, end_date=dates[-1].date(), count=len(dates), frequency="daily",
                         fields=request_fields, fq=fq, skip_paused=False, panel=False)
        if bars is None or bars.empty:
            continue
        bars = bars.copy()
        if "time" not in bars.columns:
            bars.index.name = "time"
            bars = bars.reset_index()
        if "code" not in bars.columns:
            if len(batch) == 1 and set(request_fields).issubset(bars.columns):
                bars["code"] = batch[0]
            elif len(request_fields) == 1 and set(batch).issubset(bars.columns):
                bars = bars.melt(id_vars="time", value_vars=batch, var_name="code",
                                 value_name=fields[0])
            else:
                raise ValueError("get_price 返回缺少 code，字段 %s" % list(bars.columns))
        bars["time"] = pd.to_datetime(bars["time"]).dt.normalize()
        for field in fields:
            if tradable:
                bars[field] = bars[field].where(bars.paused.eq(0))
            panels[field].append(bars.pivot(index="time", columns="code", values=field))
    result = {}
    for field, frames in panels.items():
        panel = pd.concat(frames, axis=1) if frames else pd.DataFrame(dtype=float)
        panel = panel.reindex(index=dates, columns=codes).astype(float)
        result[field] = panel.where(np.isfinite(panel) & panel.ge(0 if field == "money" else 1e-12))
    return result


def index_key(code):
    return re.sub(r"\.(XSHG|XSHE|CSI|SH|SZ)$", "", code)


def snapshot(today):
    dates = pd.DatetimeIndex(get_trade_days(end_date=today, count=121)).normalize()
    securities = get_all_securities(["fund"], date=today)
    if securities is None or securities.empty or len(dates) < 121:
        raise ValueError("%s 缺交易日历或历史基金名单" % today)
    etfs = securities[securities["type"].astype(str).str.lower().eq("etf")]
    listed = pd.to_datetime(etfs.start_date) <= dates[0]
    eligible = sorted(etfs.index[listed])
    mapping = tracked_indices(eligible, today)
    money = quote_panels(mapping, dates[-20:], ["money"], fq=None)["money"]
    liquid = money.mean().where(money.count().eq(20))
    liquid = liquid[liquid.ge(MIN_AMOUNT)]
    representatives = {}
    for etf in sorted(liquid.index, key=lambda code: (-liquid[code], code)):
        code, name = mapping[etf]
        representatives.setdefault(index_key(code), (etf, code, name))

    members = {}
    failed = non_a = too_small = 0
    for _, code, _ in representatives.values():
        try:
            result = get_index_stocks(code, date=today)
            group = set() if result is None else set(result)
        except Exception as exc:
            failed += 1
            if failed <= 3:
                log.warn("%s %s 成分查询失败 %s" % (today, code, exc))
            continue
        if len(group) < MIN_MEMBERS:
            too_small += 1
        elif not all(A_STOCK.match(stock) for stock in group):
            non_a += 1
        else:
            members[code] = sorted(group)

    stocks = {stock for group in members.values() for stock in group}
    closes = quote_panels(stocks, dates[-41:], ["close"])["close"]
    rep_codes = [etf for etf, code, _ in representatives.values() if code in members]
    etf_close = quote_panels(rep_codes, dates, ["close"])["close"]
    momentum = (etf_close.iloc[-1].div(etf_close.iloc[0])
                + etf_close.iloc[-1].div(etf_close.iloc[-61])) / 2 - 1
    ret20 = etf_close.iloc[-1].div(etf_close.iloc[-21]) - 1
    rows = []
    for etf, code, name in sorted(representatives.values()):
        if code not in members:
            continue
        prices = closes.reindex(columns=members[code]).iloc[[0, -21, -1]].dropna(axis=1)
        coverage = float(len(prices.columns)) / len(members[code])
        if coverage < MIN_COVERAGE or not np.isfinite(momentum[etf]) or not np.isfinite(ret20[etf]):
            continue
        before = (prices.iloc[1] > prices.iloc[0]).mean()
        now = (prices.iloc[2] > prices.iloc[1]).mean()
        rows.append(dict(etf=etf, index_code=code, index_name=name, members=len(members[code]),
                         coverage=coverage, amount20=liquid[etf], momentum=momentum[etf],
                         ret20=ret20[etf], breadth=now, breadth_change=now - before))
    counts = dict(signal_date=today, listed_etfs=len(etfs), eligible_etfs=len(eligible),
                  mapped_etfs=len(mapping), liquid_etfs=len(liquid), unique_indices=len(representatives),
                  constituent_errors=failed, small_or_empty_indices=too_small,
                  non_a_indices=non_a, usable_indices=len(rows))
    return rows, counts


def rank_stats(observations):
    fields = ["breadth_change", "breadth", "momentum", "ret20", "forward_return"]
    complete = observations[fields].replace([np.inf, -np.inf], np.nan).dropna()
    if len(complete) < MIN_IC:
        return []
    ranked = complete.rank(method="average")
    y = ranked.forward_return.values
    controls = np.column_stack((np.ones(len(ranked)), ranked[["momentum", "ret20"]].values))
    residual_y = y - controls.dot(np.linalg.lstsq(controls, y, rcond=-1)[0])
    metrics = []
    for factor in fields[:-1]:
        x = ranked[factor].values
        raw_ic = correlation(x, y)
        partial_ic = np.nan
        if factor in ("breadth", "breadth_change"):
            residual_x = x - controls.dot(np.linalg.lstsq(controls, x, rcond=-1)[0])
            partial_ic = correlation(residual_x, residual_y)
        spread = np.nan
        if np.isfinite(raw_ic):
            # 排名相同的标的进同一组，避免按 ETF 代码拆分平分。
            bottom = x <= np.percentile(x, 20)
            top = x >= np.percentile(x, 80)
            returns = complete.forward_return.values
            if not (bottom & top).any():
                spread = returns[top].mean() - returns[bottom].mean()
        metrics.append(dict(factor=factor, n=len(complete), rank_ic=raw_ic,
                            partial_rank_ic=partial_ic, top_bottom_spread=spread))
    return metrics


def correlation(x, y):
    if np.std(x) < 1e-10 or np.std(y) < 1e-10:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def append_table(name, frame):
    # 同一文件只有一种列顺序，首批写表头。
    first = name not in g.written
    write_file("etf_breadth_ic_" + name + ".csv",
               frame.reindex(columns=sorted(frame.columns)).to_csv(index=False, header=first),
               append=not first)
    g.written.add(name)


def summary():
    daily = pd.DataFrame(g.ic_rows)
    groups = [("all", daily)]
    groups.extend(daily.groupby(daily.signal_date.str[:4]))
    rows = []
    for period, sample in groups:
        for (horizon, factor), group in sample.groupby(["horizon", "factor"]):
            for metric in ("rank_ic", "partial_rank_ic", "top_bottom_spread"):
                values = group[metric].dropna()
                if values.empty:
                    continue
                rows.append(dict(period=period, horizon=horizon, factor=factor, metric=metric,
                                 dates=len(values), mean=values.mean(), median=values.median(),
                                 positive_rate=values.gt(0).mean(),
                                 median_cross_section=group.loc[values.index, "n"].median()))
    return pd.DataFrame(rows)


def mature_returns(today):
    due_ages = (1,) + tuple(horizon + 1 for horizon in HORIZONS)
    due = [cohort for cohort in g.pending if g.day - cohort["day"] in due_ages]
    if not due:
        return
    codes = {row["etf"] for cohort in due for row in cohort["rows"]}
    opening = quote_panels(codes, pd.DatetimeIndex([today]), ["open"], tradable=True)["open"].iloc[0]
    daily_rows = []
    for cohort in due:
        age = g.day - cohort["day"]
        if age == 1:
            cohort["entry"] = opening.to_dict()
            cohort["entry_date"] = today
            continue
        observations = pd.DataFrame(cohort["rows"])
        etfs = observations.etf
        observations["forward_return"] = etfs.map(opening).div(etfs.map(cohort["entry"])) - 1
        observations["signal_date"] = cohort["date"]
        observations["entry_date"] = cohort["entry_date"]
        observations["exit_date"] = today
        observations["horizon"] = age - 1
        append_table("observations", observations)
        for metric in rank_stats(observations):
            metric.update(signal_date=cohort["date"], exit_date=today, horizon=age - 1)
            daily_rows.append(metric)
    g.pending = [cohort for cohort in g.pending if g.day - cohort["day"] < HORIZONS[-1] + 1]
    if daily_rows:
        g.ic_rows.extend(daily_rows)
        append_table("daily", pd.DataFrame(daily_rows))
        table = summary()
        if not table.empty:
            write_file("etf_breadth_ic_summary.csv", table.to_csv(index=False), append=False)
            brief = table[(table.period == "all") & (table.factor == "breadth_change")
                          & table.metric.isin(["rank_ic", "partial_rank_ic"])]
            log.info("\n" + brief.to_string(index=False))


def observe(context):
    today = context.current_dt.strftime("%Y-%m-%d")
    g.day += 1
    mature_returns(today)
    if g.day % SAMPLE_EVERY:
        return
    rows, counts = snapshot(today)
    append_table("coverage", pd.DataFrame([counts]))
    if len(rows) >= MIN_IC:
        g.pending.append(dict(day=g.day, date=today, rows=rows))
    log.info("%s 可用指数 %d/%d，流动 ETF %d，成分接口报错 %d，成分不足或为空 %d"
             % (today, len(rows), counts["unique_indices"], counts["liquid_etfs"],
                counts["constituent_errors"], counts["small_or_empty_indices"]))
