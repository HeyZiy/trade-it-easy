"""国家队 ETF 申赎环境监控（研究工具）。

回答一个问题：A 股宽基 ETF 的申赎环境当前处于「持续净申购（托底在场）」「停止（缓冲垫缺席）」
还是「净赎回（抽水）」状态，以及该状态对 core 权益持仓各标的所属指数组意味着什么。

口径（2026-11 调研定稿，全部为公开数据的描述性统计，不归因任何具体买方）：
- 份额：上交所 ETF 份额日频整表（akshare fund_etf_scale_sse，按日请求）+ 深交所基金规模日频
  （akshare fund_scale_daily_szse，区间批量）；净值：天天基金 fund_open_fund_info_em（单位净值）。
- 净申赎金额 = 当日份额变动 × 当日单位净值。ETF 分红日该式会把份额调整误读成申赎；
  监控标的以无分红宽基为主，误差可接受，不做复权修正。
- 交易所份额为 T+1 日终口径；单日变动是全市场申赎合计，国家队只是主要持有人之一，
  输出的是环境状态而非国家队买卖金额。
- 组级阈值（quantdata 描述性口径）：合计份额 5 日变动 ≥ +7.5% = LARGE_IN（大规模净申购）、
  ≥ +2% = ELEVATED、≤ -2% = REDEEM（净赎回）、其余 FLAT。
- 连续停止净申购天数 = 核心警报：国家队开始买入不是底部，但托底缺席时两融负反馈失去缓冲。

用法（仓库根目录，项目 conda 解释器）：
    python -m research.tools.national_team.national_team
    python -m research.tools.national_team.national_team --start 20260901 --end 20261121
    python -m research.tools.national_team.national_team --no-account
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402

DATA_DIR = REPO_ROOT / "research" / "data" / "national_team"
SHARES_CACHE = DATA_DIR / "shares.csv"
NAVS_CACHE = DATA_DIR / "navs.csv"

# 指数组监控池。基准 = 2025 年报披露的汇金系重仓宽基 ETF（16 只核心 + A500 家族合并组）。
# 家族内多只 ETF 合并为组：组级份额合计变动才是稳市资金的方向信号，单只易受公司申赎噪音影响。
GROUPS: dict[str, dict] = {
    "HS300": {"label": "沪深300", "codes": ["510300", "510310", "510330", "159919"]},
    "SSE50": {"label": "上证50", "codes": ["510050"]},
    "A500": {
        "label": "中证A500",
        # 2026 年 10 月救市名单新增纳入的 A500 家族（quantdata 口径）；按份额规模取主力
        "codes": ["563360", "159338", "159352", "159361", "563220", "159351", "563800", "512050"],
    },
    "CSI1000": {"label": "中证1000", "codes": ["512100", "159845", "560010", "159629"]},
    "CSI500": {"label": "中证500", "codes": ["510500", "512500"]},
    "STAR50": {"label": "科创50", "codes": ["588000", "588080"]},
    "CHINEXT": {"label": "创业板", "codes": ["159915", "159977", "159957"]},
}

# core 权益持仓中受国家队环境影响的标的（跨境/黄金/场外不在监控范围）
HOLDING_GROUPS = {
    "563360": "A500",
    "515180": "DIVIDEND",   # 红利：国家队历史少量参与，作对照
    "159928": "INDUSTRY",   # 行业ETF：国家队不买，作对照
    "512010": "INDUSTRY",
    "159680": "CSI1000",
    "159957": "CHINEXT",
}

WATCHED_CODES = sorted({c for g in GROUPS.values() for c in g["codes"]})
SZSE_CODES = {c for c in WATCHED_CODES if c.startswith(("15", "16", "159"))}


def is_trading_day_like(d: date) -> bool:
    """粗过滤：周末必非交易日（节假日由空表自然跳过，缺口在合并时以 NaN 保留）。"""
    return d.weekday() < 5


def fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


def fetch_shares_window(start: date, end: date, log=print) -> pd.DataFrame:
    """拉取 [start, end] 区间两所份额，返回长表 date,code,shares。"""
    rows = []
    # 深交所：区间批量一次
    try:
        sz = ak.fund_scale_daily_szse(start_date=fmt(start), end_date=fmt(end), symbol="ETF")
        sz = sz[sz["基金代码"].astype(str).isin(SZSE_CODES)]
        for _, r in sz.iterrows():
            rows.append({"date": str(r["日期"]), "code": str(r["基金代码"]), "shares": float(r["基金份额"])})
        log(f"szse rows: {len(sz)}")
    except Exception as e:  # noqa: BLE001
        log(f"szse fetch failed: {type(e).__name__} {e}")

    # 上交所：逐日整表
    d = start
    while d <= end:
        if is_trading_day_like(d):
            try:
                ss = ak.fund_etf_scale_sse(date=fmt(d))
                if ss is None or ss.empty or "基金代码" not in ss.columns:
                    d += timedelta(days=1)  # 节假日/非披露日返回空表
                    continue
                ss = ss[ss["基金代码"].astype(str).isin(WATCHED_CODES)]
                stat = str(ss["统计日期"].iloc[0]) if len(ss) else ""
                got = {str(r["基金代码"]): float(r["基金份额"]) for _, r in ss.iterrows()}
                day_key = f"{d.year}-{d.month:02d}-{d.day:02d}" if stat == "" else stat
                for code, sh in got.items():
                    rows.append({"date": day_key, "code": code, "shares": sh})
            except Exception as e:  # noqa: BLE001
                log(f"sse {fmt(d)} failed: {type(e).__name__} {e}")
        d += timedelta(days=1)

    return pd.DataFrame(rows)


def fetch_navs() -> pd.DataFrame:
    """拉取监控池单位净值历史，返回长表 date,code,nav。"""
    rows = []
    for code in WATCHED_CODES:
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            for _, r in df.iterrows():
                rows.append({"date": str(r["净值日期"]), "code": code, "nav": float(r["单位净值"])})
        except Exception as e:  # noqa: BLE001
            print(f"nav {code} failed: {type(e).__name__} {e}")
    return pd.DataFrame(rows)


def load_cache(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_csv(path, dtype={"code": str})
        return df
    return pd.DataFrame()


def merge_update(old: pd.DataFrame, new: pd.DataFrame, keys=("date", "code")) -> pd.DataFrame:
    if old.empty:
        return new.copy()
    if new.empty:
        return old.copy()
    merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=list(keys), keep="last")
    return merged.sort_values(list(keys)).reset_index(drop=True)


def compute_group_state(shares: pd.DataFrame, navs: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """按指数组聚合：份额合计、5 日变动%、近 N 日净申赎估算。"""
    if shares.empty:
        return pd.DataFrame()
    code2group = {c: g for g, spec in GROUPS.items() for c in spec["codes"]}
    shares = shares.copy()
    shares["group"] = shares["code"].map(code2group)
    shares = shares.dropna(subset=["group"])

    pivot = shares.pivot_table(index="date", columns="group", values="shares", aggfunc="sum")
    pivot = pivot.sort_index()
    diff = pivot.diff()
    chg5 = pivot / pivot.shift(lookback) - 1

    nav_price = {}
    if not navs.empty:
        latest = navs.sort_values("date").groupby("code").tail(1)
        nav_price = dict(zip(latest["code"], latest["nav"]))
    # 组近似价格：组内标的单位净值均值（份额加权更准，但组内净值彼此接近，均值足够）
    nav_map = {}
    for g, spec in GROUPS.items():
        vals = [nav_price.get(c) for c in spec["codes"] if nav_price.get(c)]
        nav_map[g] = sum(vals) / len(vals) if vals else float("nan")

    out = []
    for g in pivot.columns:
        label = GROUPS[g]["label"]
        d5 = chg5[g].iloc[-1]
        state = (
            "LARGE_IN" if d5 is not None and pd.notna(d5) and d5 >= 0.075
            else "ELEVATED" if d5 is not None and pd.notna(d5) and d5 >= 0.02
            else "REDEEM" if d5 is not None and pd.notna(d5) and d5 <= -0.02
            else "FLAT"
        )
        recent_diff = diff[g].dropna().tail(20)
        in_amt = float((recent_diff * nav_map.get(g, float("nan"))).dropna().sum()) if recent_diff.size else 0.0
        # 连续停止净申购：自最近一次非零份额变动日起的连续零变动交易日数
        series = diff[g].dropna()
        streak = 0
        for v in reversed(list(series.values)):
            if abs(v) < 1e4:  # 不足 0.01 亿份视为零
                streak += 1
            else:
                break
        out.append({
            "group": g, "label": label, "state": state,
            "shares_now": float(pivot[g].iloc[-1]),
            "chg5": None if pd.isna(d5) else round(float(d5) * 100, 2),
            "net_amt_20d_yi": round(in_amt / 1e8, 1),
            "stall_streak": streak,
        })
    return pd.DataFrame(out).sort_values("shares_now", ascending=False)


STATE_ZH = {
    "LARGE_IN": "大规模净申购",
    "ELEVATED": "抬升",
    "REDEEM": "净赎回",
    "FLAT": "平稳",
}


def render_report(group_state: pd.DataFrame, end_date: str) -> str:
    lines = [f"国家队ETF申赎环境 · {end_date}（口径：组级份额变动，非国家队买卖金额）", ""]
    lines.append(f"{'组':<8}{'状态':<8}{'份额(亿份)':>12}{'5日变动%':>10}{'近20日净申赎(亿)':>16}{'停止天数':>8}")
    for _, r in group_state.iterrows():
        lines.append(
            f"{r['label']:<8}{STATE_ZH[r['state']]:<8}{r['shares_now']/1e8:>12.1f}"
            f"{('%+.1f' % r['chg5']) if r['chg5'] is not None else 'NA':>10}"
            f"{r['net_amt_20d_yi']:>16.1f}{r['stall_streak']:>8d}"
        )
    return "\n".join(lines)


def render_holdings_view(group_state: pd.DataFrame) -> str:
    if group_state.empty:
        return ""
    gs = group_state.set_index("group")
    lines = ["", "core 持仓视角（国家队环境对持仓标的的覆盖判断）："]
    for code, g in HOLDING_GROUPS.items():
        if g in gs.index:
            r = gs.loc[g]
            lines.append(f"  {code} -> {r['label']}：{STATE_ZH[r['state']]}，5日 {('%+.1f%%' % r['chg5']) if r['chg5'] is not None else 'NA'}，停止 {int(r['stall_streak'])} 日")
        else:
            lines.append(f"  {code} -> {g}：不在国家队监控组（对照标的）")
    lines.append("风险口径：组转停止/净赎回 = 左尾变肥（负反馈失去缓冲），恢复净申购 = 左尾收窄；"
                 "均为风险提示，不构成操作指令。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="国家队 ETF 申赎环境监控")
    parser.add_argument("--start", default=None, help="起始日 YYYYMMDD（默认缓存末尾+1 或 60 个自然日前）")
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD（默认今天）")
    parser.add_argument("--no-account", action="store_true", help="跳过 core 持仓关联视图")
    parser.add_argument("--no-fetch", action="store_true", help="只用缓存计算，不联网")
    args = parser.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    if args.end:
        end = datetime.strptime(args.end, "%Y%m%d").date()
    shares_old = load_cache(SHARES_CACHE)
    if args.start:
        start = datetime.strptime(args.start, "%Y%m%d").date()
    elif not shares_old.empty:
        start = datetime.strptime(str(shares_old["date"].max()), "%Y-%m-%d").date() + timedelta(days=1)
    else:
        start = end - timedelta(days=60)
    if start > end:
        start = end

    if args.no_fetch:
        print("仅用缓存计算")
    else:
        if (end - start).days >= 0:
            new_shares = fetch_shares_window(start, end)
            if not new_shares.empty:
                shares_all = merge_update(shares_old, new_shares)
                shares_all.to_csv(SHARES_CACHE, index=False)
                print(f"shares cache: {SHARES_CACHE} rows={len(shares_all)}")
            else:
                shares_all = shares_old
        else:
            shares_all = shares_old
    shares_all = load_cache(SHARES_CACHE)

    navs_old = load_cache(NAVS_CACHE)
    need_navs = navs_old.empty or (
        not shares_all.empty and str(shares_all["date"].max()) > str(navs_old["date"].max())
    )
    if not args.no_fetch and need_navs:
        navs_new = fetch_navs()
        if not navs_new.empty:
            navs_all = merge_update(navs_old, navs_new)
            navs_all.to_csv(NAVS_CACHE, index=False)
            print(f"navs cache: {NAVS_CACHE} rows={len(navs_all)}")
        else:
            navs_all = navs_old
    else:
        navs_all = navs_old  # 净值历史稳定，仅在滞后于份额缓存时补拉

    group_state = compute_group_state(shares_all, navs_all)
    if group_state.empty:
        print("无份额数据（缓存为空且未联网）")
        return 1
    end_date = str(shares_all["date"].max())
    print(render_report(group_state, end_date))
    if not args.no_account:
        print(render_holdings_view(group_state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
