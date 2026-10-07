# -*- coding: utf-8 -*-
# ghost_outpool_v1 —— 只读本地缓存，测"持仓出池"这件事本身有没有信息（2026-10-06）
# 问题：含洞版本（v3_1_1 +69.45% / v3_2 +76.93%）比诚实版本（v3_1_2 +52.82% /
#   v3_2_1 +50.78%）高 16~26pp，差值全在"持仓一出池就不再由排名管辖"这一条上。
#   这笔钱是"出池后照样跑"的普遍规律，还是 512480 半导体一张彩票？
# 测法：本地复现 6 关建池（名称/代码段/成熟/流动性/相关性去重），每 20 交易日
#   重建一次；对每次重建取 OUT=上次在池、这次出池 的票，算它出池后 20/60/120
#   交易日收益，与"同期池内成员中位数"（= 诚实版下一槽会买的那一档水位）比。
#   OUT 若普遍跑赢池内中位 → "出池即免疫"有正期望，是规则；若普遍跑输或打平 →
#   含洞优势是单点运气 + 少换手的账面口径差。
# 旁证：相关性去重把票顶出池时，池里必留着一只 corr≥0.90 的孪生 → 顺带算
#   OUT vs 顶替者 的 forward 差（孪生可买，等于诚实版本来就能吃到同一段）。
# 已知局限：本地池成员与平台差 1~3 只（计数别当真，成员判定可用）；价格是
#   复权序列，只用比值。
#
# 结果头注（2026-10-06 实测，34 次重建 / 136 次出池事件）：
#   池成员数序列 26→49（与平台 32→55 同形，差 1~3 只，判定可用）。
#   ① **出池票并不普遍继续跑**：120 日 Δ(OUT − 同期池内中位) 中位 **-2.03pp**、
#     跑赢比例 **44.0%**（20 日 -0.54pp/46.6%、60 日 -0.57pp/46.5% 同向）；
#     但 Δ 均值 +1.38pp 为正 = 右偏：正 Δ 合计 +582pp 里 TOP8（稀有金属 562800
#     +60.6、科创芯片富国 588810 +47.9、稀土 516780/516150 +37.8/+36.9/+35.1、
#     信创 +30.7、金融 +23.6、稀有金属广发 +20.6）就占 293pp = 50%。
#     **删掉这 8 只彩票，全体 136 事件的净 Δ 从 +138pp 变 -155pp。**
#   ② **去重型出池=可替代**：被 corr≥0.90 孪生顶掉的 40 个事件里，OUT 与顶替者
#     120 日收益 Δ 中位 **-0.04pp**、胜率 50% ——统计不可区分。个体例证：
#     512480 半导体自 2024-11-26 起 +102.0%，同期顶掉它的 588200 +105.3%。
#   ③ 但**持有期是真的**：诚实版 v3_2_1 对孪生 588200 依排名闸进出 6 次
#     （-1,002/-5,339/+13,255/+5,398/-3,187/+1,046 = 净 +10,171），而含洞版对
#     512480 长持到期末是浮盈 +38.5k —— 同一段趋势只拿到 **26%**。
#   裁决方向（**已否决，2026-10-06 用户两条理由，别再端上来**）：我曾提议"持有期
#     下限/排名闸宽限期 N=40"。否决理由①**针对性调参**：N=40 的来历就是 512480
#     那一刀（2024-10-17 建仓 → 11-26 被切 ≈28 交易日），用 n=1 的观察给参数定标，
#     正是曲线拟合的定义，任何 N 都是后视的；理由②**槽位机会成本**：虽有 -8% 限亏
#     仍在岗（最坏不是无限跌），但宽限期让一只不上不下（-8%~0%）的票白占一个槽 40
#     天 = v3_5"低波票永生锁死槽位"的温和版本，我提这条时没算这个成本。
#     本线由此**收线于 v3_3**，不再开第四条出场实验。

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).resolve().parent / "_cache"
START, END = "2024-01-02", "2026-09-30"
REBUILD_EVERY = 20
MIN_BARS = 250
MATURE_DAYS = 365
LIQ_AMT20_MIN = 50_000_000.0
CORR_DEDUPE = 0.90
HORIZONS = (20, 60, 120)

EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '2000', '200',
              '中证A500', 'A500', 'A50', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '上海金', '短融', '日利', '添益', '快线')
EXCLUDE_CODE_PREFIXES = ('513', '518')


def load():
    daily = pickle.load(open(CACHE / "etf_daily_full.pkl", "rb"))
    uni = pickle.load(open(CACHE / "etf_universe.pkl", "rb"))
    names = dict(zip(uni["code"].astype(str), uni["name"].astype(str)))
    close = pd.DataFrame({k: v["close"].astype(float) for k, v in daily.items()})
    amt = pd.DataFrame({k: v["amount"].astype(float) for k, v in daily.items()})
    for df in (close, amt):
        df.index = pd.to_datetime(df.index)
    close = close.sort_index()
    amt = amt.reindex(close.index)
    return close, amt, names


def name_ok(code, nm):
    return not [k for k in EXCLUDE_KW if k in nm] and code.startswith(EXCLUDE_CODE_PREFIXES) is False


def build_pool(t, close, amt, names):
    """t 日的 6 关池，返回 (kept 列表, 被去重顶掉的 {out_code: 顶替者})。"""
    hist_c = close.loc[:t]
    hist_a = amt.loc[:t]
    if len(hist_c) < MIN_BARS:
        return [], {}
    obs = hist_c.notna().sum()
    first = hist_c.apply(lambda s: s.first_valid_index())
    amt20 = hist_a.tail(20).mean()
    cands = []
    for code in close.columns:
        nm = names.get(code, "")
        if not name_ok(code, nm):
            continue
        if obs.get(code, 0) < MIN_BARS:
            continue
        f = first.get(code)
        if f is None or pd.isna(f) or (t - f).days < MATURE_DAYS:
            continue
        a20 = amt20.get(code)
        if a20 is None or pd.isna(a20) or a20 < LIQ_AMT20_MIN:
            continue
        cands.append((code, a20))
    cands.sort(key=lambda x: -x[1])
    codes = [c for c, _ in cands]
    if not codes:
        return [], {}
    rets = hist_c.tail(250)[codes].pct_change()
    corr = rets.corr(min_periods=120)
    kept, dropped_by = [], {}
    for c in codes:
        twin = None
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:
                twin = k
                break
        if twin is None:
            kept.append(c)
        else:
            dropped_by[c] = twin
    return kept, dropped_by


def fwd(close, code, i, h):
    """网格第 i 位起 h 个交易日收益；越界返回 None。"""
    if i + h >= len(close.index) or code not in close.columns:
        return None
    a, b = close.iloc[i][code], close.iloc[i + h][code]
    if pd.isna(a) or pd.isna(b) or a <= 0:
        return None
    return (b / a - 1) * 100.0


def main():
    close, amt, names = load()
    grid = close.index
    i0 = grid.get_indexer([pd.Timestamp(START)], method="nearest")[0]
    i1 = grid.get_indexer([pd.Timestamp(END)], method="nearest")[0]
    dates = list(grid[i0:i1 + 1:REBUILD_EVERY])
    print("网格 %d 天 | 重建 %d 次 | 池成员数序列：" % (len(grid), len(dates)))

    prev, rows, sizes = None, [], []
    for n, t in enumerate(dates):
        pool, dropped_by = build_pool(t, close, amt, names)
        sizes.append(len(pool))
        if prev is not None:
            i = grid.get_loc(t)
            pool_fwd_med = {}
            for h in HORIZONS:
                vals = [f for f in (fwd(close, c, i, h) for c in pool) if f is not None]
                pool_fwd_med[h] = float(np.median(vals)) if len(vals) >= 5 else None
            for out in sorted(set(prev) - set(pool)):
                if out in pool:
                    continue
                reason = "去重" if out in dropped_by else (
                    "流动性" if (amt.loc[:t].tail(20).mean().get(out) or 0) < LIQ_AMT20_MIN else "其他/退市")
                twin = dropped_by.get(out)
                rec = {"date": str(t.date()), "code": out, "name": names.get(out, ""),
                       "reason": reason, "twin": twin and names.get(twin, ""),
                       "twin_code": twin}
                for h in HORIZONS:
                    f = fwd(close, out, i, h)
                    rec["out%d" % h] = f
                    rec["med%d" % h] = pool_fwd_med[h]
                    rec["twin%d" % h] = fwd(close, twin, i, h) if twin else None
                rows.append(rec)
        prev = pool

    print("池成员数：" + " ".join(str(s) for s in sizes))
    df = pd.DataFrame(rows)
    print("OUT 事件总数 %d | 原因分布：%s"
          % (len(df), df["reason"].value_counts().to_dict()))
    print("\n== 出池票 forward 收益 vs 同期池内中位（正=出池后仍能跑，免疫有期望）==")
    for h in HORIZONS:
        sub = df.dropna(subset=["out%d" % h, "med%d" % h])
        d = sub["out%d" % h] - sub["med%d" % h]
        print("  %3d 日 n=%3d | OUT 均值 %+.2f%% 中位 %+.2f%% | 池内中位均值 %+.2f%% | "
              "Δ 均值 %+.2fpp Δ 中位 %+.2fpp | OUT 跑赢比例 %.1f%%"
              % (h, len(sub), sub["out%d" % h].mean(), sub["out%d" % h].median(),
                 sub["med%d" % h].mean(), d.mean(), d.median(), (d > 0).mean() * 100))
    print("\n== 去重型 OUT：被顶掉的票 vs 顶它的孪生（孪生=诚实版下一槽买得到）==")
    dd = df[df["reason"] == "去重"].dropna(subset=["out120"])
    dd = dd.assign(d120=dd["out120"] - dd["twin120"])
    print("  n=%d | OUT−孪生 120 日 Δ 均值 %+.2fpp 中位 %+.2fpp | OUT 更强的比例 %.1f%%"
          % (len(dd), dd["d120"].mean(), dd["d120"].median(), (dd["d120"] > 0).mean() * 100))
    print("\n== 120 日 Δ(OUT − 池内中位) 极值 TOP/BOTTOM 8（看差值是不是集中在个别票）==")
    ok = df.dropna(subset=["out120", "med120"]).assign(d=0.0)
    ok["d"] = ok["out120"] - ok["med120"]
    cols = ["date", "code", "name", "reason", "out120", "med120", "d"]
    print("最好：\n" + ok.nlargest(8, "d")[cols].to_string(index=False))
    print("最差：\n" + ok.nsmallest(8, "d")[cols].to_string(index=False))
    pos = ok[ok["d"] > 0]
    print("\n正 Δ 合计 %+.0fpp（n=%d）| 负 Δ 合计 %+.0fpp（n=%d）| 全体合计 %+.0fpp"
          % (pos["d"].sum(), len(pos), ok.loc[ok["d"] <= 0, "d"].sum(),
             int((ok["d"] <= 0).sum()), ok["d"].sum()))


if __name__ == "__main__":
    main()
