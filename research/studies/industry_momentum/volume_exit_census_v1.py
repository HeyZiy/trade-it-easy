# -*- coding: utf-8 -*-
# volume_exit_census_v1 —— 只读本地缓存，测"量能脉冲有没有资格进出场侧"（2026-10-06）
#
# 问题：出场侧三次换血（v3_4/v3_4_1 绝对动量、v3_5 高点回撤）全在 v3_3 之下，
#   共同死因是"只看持仓自身的规则会杀死轮动"（3 槽占满、买入侧无补槽）。
#   量能族里"水平/分位"（量价热度）已在候选集实测无区分力（crowd_factor_study：
#   候选集 20 日 IC +0.016 / t +0.30），但"脉冲"（量比 = 当日成交额/近 20 日均额）
#   从没测过，且它在出场侧的假说是具体的：异常放量 = 趋势衰竭的领先信号，
#   应比排名闸更早砍在峰的转点上（v3_3 的痛点正是右尾利润回吐一大截）。
# 测法（不动 v3_3 一行码，只回答"有没有资格谈下一步"）：
#   持仓窗口 = 平台真实日志重放出的 105 个 episode（reports/mae_v1/trades.csv，
#   v3_2_1 受管路径；行业 ETF 的流动性结构与 v3_3 同分布，覆盖率结论可迁）。
#   T1 覆盖率（先决关）：episode 内交易日数、量比≥1.5/2/3 的事件数与每笔期望
#      事件数——事件太少就就地结案，后面不用看。
#   T2 事件后 20/60 交易日 Δ = fwd(该票) − median(fwd(当期池成员))，与"同一批
#      episode 里非事件日"同口径对照，给中位/胜率/Welch t。
#   T3 冗余关：事件日量比与当日 score（src 生产打分器原函数）的 Spearman 相关。
#   T4 提前量与反事实：对 trigger=排名 出场的 episode，取峰值日之后首个量比≥2
#      事件，算它比平台实际出场日提前几个交易日、以及"当日就砍"相对"实际出场"
#      的收益差（全用 qfq 序列内部比值，与平台价格零混用）。
# 判线（事先约定，跑完不改）：T1 事件 <30 就地结案；T3 |corr|≥0.3 判冗余结案；
#   T2/T4 |t|<2 判无信号结案。三条任一命中 → 量能族在本线整体收线。
# 已知局限：① 持仓窗口来自 v3_2_1 而非 v3_3 日志（无 v3_3 全量日志），两者出场
#   规则不同但池/票同源，覆盖率与冗余两关不受影响，T4 的反事实只作方向读数；
#   ② 反事实是路径不重排的一阶近似（提前砍掉的槽位再买什么没算）；③ 价格为新浪
#   qfq 序列，只用比值；成交额经探针确认为原始元值（512800 折算窗口无阶跃），
#   量比作为成交额比值对份额折算免疫。
# 产出：reports/volume_exit_census_v1/tables.txt（四张表原样留档）。
#
# 结果头注（2026-10-06 实测，105 个真实持仓 episode / 2061 持仓日，32s 跑完）：
#   **判线命中第三条：出场方向反号 + 唯一窄口子不成立 → 量能族在本线整体结案。**
#   T1 覆盖率过关：量比≥2 事件 103 日、覆盖 34/105 笔（每笔期望 0.98 日），
#     ≥3 仅 22 日（太稀，不作主判据）。持仓期量比≥2 日占比 5.0% vs 全市场 6.51%
#     —— 持仓票并不更容易放量。
#   T2 假说**反号**（这是核心证据）：事件后 20 日 Δ(票−池中位) 中位 **+2.35pp**、
#     胜率 61.2%、Welch t **+2.63**；非事件日 +0.51pp/52.0%。60 日同向（+2.79pp、
#     t +1.56，重叠样本偏乐观）。→ 持仓期放量之后是**继续涨**，放量不能当卖出理由。
#   T2b 零参数分解（放量与否 × 当日是否 episode 内至今最高价，h=20）：
#     放量×创新高 n=67 Δ+3.56pp t+2.92 | 放量×非新高 n=36 Δ+1.92pp t+2.17
#     | 平量×创新高 n=545 Δ-0.64pp t-0.33 | 平量×非新高 n=1390 Δ+0.81pp t+5.51。
#     两点读法：① 无论创新高与否，放量组前瞻收益都为正 → "放量=派发"在我们的
#     样本域不成立；② **缩量的创新高（545 日）没有前瞻力**，反倒是 t+5.51 的
#     平量非新高组说明持仓期本身是正漂移——这正是"别用自造规则乱砍"的旁证。
#   T3 冗余关过：ρ(量比, 生产 score) = **+0.259** < 0.3（事件日子集 +0.205）
#     → 量比携带 score 之外的信息，但按 T2 那个信息是**正号**的。
#   T4 唯一生路（"峰值日之后放量=转点预警"）**不成立**，三条一起看：
#     ① 覆盖面 11/101 笔 = 改了也几乎不改净值（89% 的排名出场根本不触发）；
#     ② 反事实差额（事件日砍 − 实际砍）中位 +0.91%、均值 +2.84%、t **+1.94 < 2**
#       （预定线，n=11）；
#     ③ 决定性一条：触发组"峰值→实际出场回吐"中位 **-5.77%** vs 未触发组
#       **-5.24%** —— 若放量真是派发，触发组回吐应显著更深，实测差 0.53pp 在噪声
#       内，即峰值后放量**不携带任何关于回吐深度的信息**。
#   与先前证据合流：量能"水平/分位"族（量价热度）早先已在候选集实测无区分力
#     （crowd_factor_study：候选集 20 日 IC +0.016/t +0.30；v3_1_3 闸门常开 +0.92pp
#     而 maxDD 深 4.17pp）；本次"脉冲"族两个方向（续涨为正、转点预警无效）都堵死。
#     → 对"退出靠量能也没啥招"这句话，现在答案是**实测过的**，不是推测。
#   未被本实验支持的外推（明确写下免得日后误读）：T2/T2b 的正号来自"**条件于已持有
#     的强势票**"样本，不能据此说"放量该买"；那条要在候选集/全截面口径重跑 IC 才算，
#     且它改的是买入确认（2024 假突破），不碰右尾捕获数 n=3 这个根本约束。
#   方法学欠账：持仓窗口取自 v3_2_1 平台日志（无 v3_3 全量日志）。T1/T2/T3 与出场
#     规则无关，可直接迁移；T4 的"事件日→出场日"距离受 v3_2_1 自身峰回20%规则影响，
#     属方向读数。零成本复核路径：下次跑 v3_3 时把平台日志整张贴进 logs/，本脚本
#     换 TRADES 源重放即可。

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from ghost_outpool_v1 import build_pool, fwd, load  # noqa: E402
from src.etf.industry_momentum import momentum_score  # noqa: E402

TRADES = HERE / "reports" / "mae_v1" / "trades.csv"
START, END = "2024-01-02", "2026-09-30"
REBUILD_EVERY = 20
VR_WIN, VR_BUCKETS = 20, (1.5, 2.0, 3.0)
HORIZONS = (20, 60)
OUT = HERE / "reports" / "volume_exit_census_v1"


def vol_ratio(amt):
    """量比 = 当日成交额 / 前 VR_WIN 个交易日均额（不含当日）。"""
    return amt / amt.shift(1).rolling(VR_WIN).mean()


def episodes(close):
    """trades.csv → 每个 episode 的 (code, entry, exit, trigger, pnl) + 网格区间。"""
    df = pd.read_csv(TRADES, encoding="utf-8-sig")
    df = df[df["exit"].notna()].copy()
    grid = close.index
    out = []
    for _, r in df.iterrows():
        code = str(r["code"]).zfill(6)
        if code not in close.columns:
            continue
        i = int(grid.get_indexer([pd.Timestamp(r["entry"])], method="nearest")[0])
        j = int(grid.get_indexer([pd.Timestamp(r["exit"])], method="nearest")[0])
        if j <= i:
            continue
        out.append({"code": code, "entry": str(r["entry"]), "exit": str(r["exit"]),
                    "trigger": r["trigger"], "actual_pnl": float(r["actual_pnl"]),
                    "i": i, "j": j, "shares": int(r["shares"])})
    return out


def main():
    close, amt, names = load()
    vr = vol_ratio(amt)
    grid = close.index
    eps = episodes(close)
    lines = []

    def say(s=""):
        print(s)
        lines.append(str(s))

    say("episode 数 %d | 持仓期交易日合计 %d"
        % (len(eps), sum(e["j"] - e["i"] + 1 for e in eps)))

    # ── T1 覆盖率 ──
    say("\n[T1] 持仓期量比事件覆盖率（先决关：<30 事件就地结案）")
    rows = []
    for thr in VR_BUCKETS:
        ev_days, ev_eps = 0, set()
        for k, e in enumerate(eps):
            v = vr.iloc[e["i"]:e["j"] + 1][e["code"]]
            m = int((v >= thr).sum())
            ev_days += m
            if m:
                ev_eps.add(k)
        say("  量比≥%.1f：事件 %5d 日 | 覆盖 %3d/%d 个 episode | 每笔期望 %.2f 日"
            % (thr, ev_days, len(ev_eps), len(eps), ev_days / len(eps)))
        rows.append((thr, ev_days, len(ev_eps)))
    say("  全体票·全市场同窗口对照：量比≥2 的日占比 %.2f%%（持仓期是否天然低放量）"
        % (100.0 * float((vr.loc[START:END] >= 2.0).sum().sum()
                         / vr.loc[START:END].notna().sum().sum())))

    # ── 池（按策略口径每 20 日重建一次，事件日映射到所辖重建）──
    i0 = int(grid.get_indexer([pd.Timestamp(START)], method="nearest")[0])
    i1 = int(grid.get_indexer([pd.Timestamp(END)], method="nearest")[0])
    rb_dates = list(grid[i0:i1 + 1:REBUILD_EVERY])
    pools = {}
    for t in rb_dates:
        pools[t], _ = build_pool(t, close, amt, names)
    med_fwd = {}
    for t, pool in pools.items():
        i = int(grid.get_loc(t))
        for h in HORIZONS:
            vals = [f for f in (fwd(close, c, i, h) for c in pool) if f is not None]
            med_fwd[(t, h)] = float(np.median(vals)) if len(vals) >= 5 else None

    def governing(i):
        prev = [t for t in rb_dates if t <= grid[i]]
        return prev[-1] if prev else None

    # ── T2 事件后前瞻收益 Δ vs 池中位（与同 episode 非事件日对照）──
    say("\n[T2] 事件后 Δ = fwd(票) − median fwd(当期池)，单位 pp")
    for h in HORIZONS:
        ev, non = [], []
        for e in eps:
            pool_t = None
            for d_i in range(e["i"], e["j"] + 1):
                t = governing(d_i)
                if t is None or med_fwd.get((t, h)) is None:
                    continue
                pool_t = t
                f = fwd(close, e["code"], d_i, h)
                if f is None:
                    continue
                delta = f - med_fwd[(pool_t, h)]
                (ev if vr.iloc[d_i][e["code"]] >= 2.0 else non).append(delta)
        if len(ev) >= 3:
            tt = stats.ttest_ind(ev, non, equal_var=False)
            say("  h=%3d 日 | 事件日 n=%4d Δ中位 %+6.2f 胜率 %4.1f%% | 非事件 n=%5d "
                "Δ中位 %+6.2f 胜率 %4.1f%% | Welch t %+5.2f"
                % (h, len(ev), float(np.median(ev)),
                   100.0 * len([x for x in ev if x > 0]) / len(ev),
                   len(non), float(np.median(non)),
                   100.0 * len([x for x in non if x > 0]) / len(non), tt.statistic))
        else:
            say("  h=%3d 日 | 事件日 n=%d → 样本不足" % (h, len(ev)))

    # ── T2b 零参数分解：创新高放量 vs 非新高放量（调和 T2 正号与 T4 负向）──
    say("\n[T2b] 持仓日按（量比≥2 与否）×（当日是否 episode 内至今最高价）拆分，h=20")
    buckets = {}
    for e in eps:
        seg_c = close.iloc[e["i"]:e["j"] + 1][e["code"]]
        seg_v = vr.iloc[e["i"]:e["j"] + 1][e["code"]]
        run_max = seg_c.cummax().values
        for k, d_i in enumerate(range(e["i"], e["j"] + 1)):
            t = governing(d_i)
            if t is None or med_fwd.get((t, 20)) is None:
                continue
            f = fwd(close, e["code"], d_i, 20)
            if f is None:
                continue
            spike = "放量" if seg_v.iloc[k] >= 2.0 else "平量"
            lab = "创新高" if seg_c.values[k] >= run_max[k] - 1e-12 else "非新高"
            buckets.setdefault((spike, lab), []).append(f - med_fwd[(t, 20)])
    for (spike, lab), vals in sorted(buckets.items()):
        if len(vals) >= 3:
            tt = stats.ttest_1samp(vals, 0.0)
            say("  %s×%s n=%4d Δ中位 %+6.2f 胜率 %4.1f%% 均值 %+6.2f t %+6.2f"
                % (spike, lab, len(vals), float(np.median(vals)),
                   100.0 * len([x for x in vals if x > 0]) / len(vals),
                   float(np.mean(vals)), tt.statistic))
        else:
            say("  %s×%s n=%d → 样本不足" % (spike, lab, len(vals)))

    # ── T3 冗余关：事件日量比 vs 当日 score ──
    say("\n[T3] 冗余关：量比 与 生产打分器 score 的 Spearman（|corr|≥0.3 判冗余结案）")
    xs, ys, keep = [], [], []
    for e in eps:
        for d_i in range(e["i"], e["j"] + 1):
            tail = close.iloc[max(0, d_i - 25):d_i + 1][e["code"]].values
            if len(tail) < 26 or np.any(~np.isfinite(tail)) or np.any(tail <= 0):
                continue
            sc = momentum_score(tail[:-1], float(tail[-1]))
            v = vr.iloc[d_i][e["code"]]
            if v != v:
                continue
            xs.append(float(v)); ys.append(sc); keep.append(float(v) >= 2.0)
    xs, ys, keep = map(np.array, (xs, ys, keep))
    rho_all = stats.spearmanr(xs, ys)
    rho_ev = stats.spearmanr(xs[keep], ys[keep])
    say("  持仓期全样本 n=%d  ρ=%+.3f (p=%.3f)" % (len(xs), rho_all.statistic, rho_all.pvalue))
    say("  事件日子集 n=%d  ρ=%+.3f (p=%.3f)" % (int(keep.sum()), rho_ev.statistic, rho_ev.pvalue))

    # ── T4 提前量与反事实（只看 排名 出场的 episode）──
    say("\n[T4] 峰值日之后首个量比≥2 事件 vs 平台实际出场（排名出场 episode，一阶近似）")
    rank_eps = [e for e in eps if str(e["trigger"]).startswith("排名")]
    say("  排名出场 episode 数：%d" % len(rank_eps))
    lead, adv, hit = [], [], 0
    hit_ids, gb_hit, gb_all = set(), [], []
    for e in rank_eps:
        seg = close.iloc[e["i"]:e["j"] + 1][e["code"]]
        pk = int(np.argmax(seg.values))
        gb = 100.0 * float(seg.iloc[-1] / seg.iloc[pk] - 1.0)
        gb_all.append(gb)
        v = vr.iloc[e["i"]:e["j"] + 1][e["code"]]
        cand = [k for k in range(pk + 1, len(seg)) if v.iloc[k] >= 2.0]
        if not cand:
            continue
        hit += 1
        hit_ids.add(id(e))
        gb_hit.append(gb)
        k = cand[0]
        lead.append((len(seg) - 1) - k)
        r_ev = seg.iloc[k] / seg.iloc[0] - 1.0
        r_ex = seg.iloc[-1] / seg.iloc[0] - 1.0
        adv.append(100.0 * (r_ev - r_ex))
    if lead:
        tt = stats.ttest_1samp(adv, 0.0)
        say("  触发 %d/%d 笔 | 提前出场 中位 %d 个交易日（均值 %.1f）"
            % (hit, len(rank_eps), int(np.median(lead)), float(np.mean(lead))))
        say("  反事实差额(事件日砍−实际砍) 中位 %+.2f%% 均值 %+.2f%% | TOP 贡/损 %s"
            % (float(np.median(adv)), float(np.mean(adv)),
               " +".join("%+.1f" % x for x in
                         sorted(adv, key=abs, reverse=True)[:5])))
        say("  单样本 t = %+.2f（n=%d）" % (tt.statistic, len(adv)))
    else:
        say("  峰值日后无量能事件 → 无对照可算")
    non_hit = [g for e, g in zip(rank_eps, gb_all) if id(e) not in hit_ids]
    say("  触发的覆盖面：峰值→实际出场回吐 触发组(n=%d) 中位 %+.2f%% | 未触发组(n=%d) "
        "中位 %+.2f%%（放量规则只够得着前一组）"
        % (len(gb_hit), float(np.median(gb_hit)) if gb_hit else float('nan'),
           len(non_hit), float(np.median(non_hit)) if non_hit else float('nan')))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tables.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("\n产出：%s" % OUT)


if __name__ == "__main__":
    main()
