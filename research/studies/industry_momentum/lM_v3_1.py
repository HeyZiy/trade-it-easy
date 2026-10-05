# -*- coding: utf-8 -*-
# lM_v3_1 —— v3 + 堵商品/债漏网：EXCLUDE_KW 补 '债'、'上海金'（2026-10-03，基于 lM_v3 复制）
# v3_1 唯一变更：名称剔除词末尾加 '债'（单字，一劳永逸盖国开债/地债/市政债等，
#   行业名无'债'字不误杀）、'上海金'（518600；不能用'金'单字，会误杀稀有金属/
#   稀土）。动机=v3 明细实锤：R² 打分器爱笔直趋势，把上海金顶成第 2 大利润
#   （+13.8k，占净利 60%）、债类 4 只 14 笔漏网——本测定行业本体剔除
#   商品/债后的真实成绩（打分器改进的净值）。其余与 lM_v3 一字未改。
# v3_1 台账补丁（2026-10-03，原地加纯记录，信号零差异）：
#   交易 CSV 导出耗积分且已耗尽 → 卖出时记 g.trades 台账，on_strategy_end
#   用免费日志打印：Σ已实现、TOP3/TOP5 集中度、分年平仓盈亏、期末未实现、
#   期末权益。重跑同窗同本金，面板应复现 +82.67%——复现本身就是核对之一。
#   平仓盈亏口径 =（当日 last_price - avg_cost）×份数 - 卖出万一佣金，
#   与交易 CSV"平仓盈亏"近似同口径（撮合价 vs last_price 可能微差）。
#
# 结果头注（回填区）：
#   v2_1（2024-01-01 ~ 2026-06-01，10 万）：+52.63% / 年化 19.96% / 回撤 -25.73% /
#     夏普 0.542 / 胜率 37.0% / 81 笔 / β 1.217 / 超额夏普 -0.030——
#     判读：纯 A 股行业动量本体 ≈ β 放大；37% 胜率 + 71 笔≈白干的结构问题
#     指向 ret20 裸排序不分"好趋势/垃圾趋势"。
#   v3 本轮（2024-01-01 ~ 2026-06-01，10 万，同窗口同本金）：
#     总 +68.13% / 年化 25.05% / 回撤 **-19.17%** / 夏普 0.836 / 索提诺 1.148 /
#     胜率 41.8% / 盈亏比 1.401 / 67 笔（28盈39亏）/ β **0.960** / α **0.095** /
#     超额 +19.08% / 超额夏普 **+0.209** / IR 0.495 / 超额回撤 -27.28% /
#     日胜率 50.9% / 波动率 25.2% / 回撤区间 2024-03-05~09-09。
#     vs v2_1（唯一差异=打分器）：收益 +15.5pp、回撤**浅** 6.6pp（全场最佳）、
#     夏普 0.542→0.836、超额夏普 -0.030→+0.209 转正、β 1.217→0.960、
#     α 0.014→0.095、交易 81→67 笔——纯 A 股本体从"β 放大不合格"升格为
#     "有正 α 嫌疑、勉强够单立赌注"。vs v2（混池漏网）：绝对收益仍差 15pp
#     （跨境趋势互补确实值钱），但回撤更好、夏普打平（0.836 vs 0.866）。
#     遗留疑点：胜率仅 37→41.8（换 R² 的本意是治噪声排名，抬幅有限）、
#     盈亏比 1.634→1.401 反降——利润是否仍前 3 笔极端集中待交易明细验证。
#     明细核对（transaction_IM_v3.csv，139 笔）：Σ平仓盈亏仅 +22.8k，
#     **TOP3=+47.5k=净利 208%**（通信 +23.8k / **上海金 +13.8k** / 科创芯片
#     +9.9k），其余 64 笔合计 **-24.6k**——比 v2 的 97.7% 更集中；2024 年
#     平仓仍 -8.1k（换打分器没救回 2024）。 realized 22.8k 与曲线 +68.1k
#     差额≈45k 为期未平仓浮盈口径。**新漏网：R² 打分器专爱笔直趋势，把
#      commodity 上海金(518600，'黄金'词不盖'上海金')顶成第 2 大利润，
#     债类漏网(国开债/地债，'债券'词不盖'国开债')4 只 14 笔 +2.3k**
#     （v2 亦有 10年地债 4 笔 +0.5k，属老洞）。+15.5pp 改进中约一半是
#     上海金 commodity 趋势，不是行业动量本体变强——收益源异质问题重演。
#   v3_1 本轮（2024-01-01 ~ 2026-06-01，10 万）：
#     总 +82.67% / 年化 29.60% / 回撤 -21.43% / 夏普 0.860 / 索提诺 1.273 /
#     胜率 42.2% / 盈亏比 1.400 / 64 笔（27盈37亏）/ β 1.219 / α 0.110 /
#     超额 +29.38% / 超额夏普 **+0.379** / IR 0.667 / 超额回撤 -16.56% /
#     日胜率 48.9% / 波动率 29.8% / 回撤区间 2024-11-11~2025-05-28。
#     **判读预案反转**：预期"剔了黄金会掉回 v2_1"，实际比 v3 还高 14.5pp——
#     上海金占用的两个槽位被行业票顶上，资金重新分配的收益 > 黄金本身。
#     三版同窗对照：v2_1(ret20)=52.6 / v3(ret20→R²,混着金债)=68.1 /
#     v3_1(R²,纯行业)=82.7——打分器改进是真的，且不靠异质资产。
#     **v3_1 ≈ v2(83.56) 同分但口径干净**：夏普 0.860 vs 0.866、回撤 21.43
#     vs 21.18、超额夏普 0.379 vs 0.364——纯 A 股行业池追平了带跨境漏网的
#     混池成绩，跨境/商品的"互补"贡献被 R² 打分器在行业内部消化了。
#     注意：β 又回到 1.22（黄金在时 0.96——它本是分散器），回撤区间后移到
#     2024-11~2025-05。集中度/漏网复查待 transaction_IM_v3_1.csv。
#     曲线分年（result_IM_v3_1.csv）：2024 **+12.2%**（建线首个不亏的无主线
#     年；v2 -0.1 / v3 -3.5，v3→v3_1 总差 +14.5pp 几乎全在 2024——剔金剔债
#     把槽位让给行业票）/ 2025 +44.6% / 2026H1 +12.6%。但 2024 仍跑输基准
#     +14.7%——无主线年成绩=不亏非超额；超额由 2025/2026 贡献。交易明细
#     导出积分不足，集中度核对挂起（面板粗对照：64 笔/27盈/盈亏比 1.40
#     ≈ v3 的 67/28/1.401，交易结构未变、只是水位抬高）。
#   v3_1+台账 本轮（同窗 2024-01-01 ~ 2026-06-01，10 万，日志口径）：
#     面板完全复现：期末权益 182666 = +82.67%——平台无随机性，可复现性过。
#     明细：平仓 65 笔（27盈38亏；面板 64 系一笔计法差），Σ已实现 +25.1k；
#     **TOP3=+52.0k=占已实现 207.5% ≈ v3 的 208% 同位**——通信 515880 一笔
#     +33.4k（占已实现 133%）、游戏 +9.7k、消费电子 +8.9k，其余 62 笔合计
#     -27.0k。集中度未被池净化治好：它不是黄金的副作用，是本策略固有结构。
#     分年（已实现）：2024 **+3.0k 转正**（v3 -8.1k；剔金债把槽让给行业票，
#     与曲线口径 +12.2% 相合）；2025 +32.5k；2026H1 **-10.5k**——曲线口径
#     的 +12.6% 全靠期末浮盈撑着，交易口径今年至今是亏的。
#     利润构成：总利润 82.7k 中已实现仅 30.3%，浮盈 +53.8k（半导体 512480
#     一只 +43.5k=全押注最大单一利润，还没落袋）。TOP3+半导体浮盈合计
#     ≈95.5k > 总利润——其余 62 笔+其余持仓净亏。判读："不到能用"维持且
#     加重：主线捕获 n=3、edge 全在右尾，且本期近六成利润是回撤一深就能
#     回吐的账面数。
#
# 唯一变更（v2_1 → v3，单变量对照）：信号打分器
#   ret20（20 日涨幅裸值）→ new.py EtfRotation 动量评分镜像：
#   25 根日线收盘 + 当日 last_price（共 26 点）→ 对数价格 x=arange、
#   w=linspace(1,2) 加权 np.polyfit → 年化 = exp(slope*250)-1 →
#   score = 年化 × 加权R²；近 3 个日环比 min < 0.95 则 score 清零（跳水否决）。
#   R² 惩罚歪斜路径，只放行"走得直"的趋势——直接冲着 37% 胜率的噪声排名去。
# 保留决定：
#   - 买入侧加 score > 0 门槛（负分=下降趋势，排序里保留供卖出用，但不买）；
#   - new.py 的 score < 5/6 上界**不抄**：过热防线仍是本线拥挤度 <90，
#     叠别人的调参常数会搅浑归因；
#   - 卖出仍按 score 降序 rank 跌出前 40%，其余引擎与 v2_1 一字未改。
# 已知口径差：new.py 用 attribute_history(code, 25) 拉分，本件复用既有 249 根
#   h 的尾 25 根 + 当日价，观测数与权重完全一致，仅取数方式省一次 API。
#
# v2_1 沿革：= v2 + 跨境硬剔除（513 前缀 + 补词）；v2 空池事故真因 = 引擎 any
#   被 numpy 覆盖对生成器恒真，已改列表推导。本件沿用全部哨兵与池规则。
#
# 池规则（同 v2_1）：每 20 交易日重建——全表 ETF → 513 前缀剔除 → 名称剔除
#   （宽基/债券/货币/商品/跨境/风格）→ 上市 ≥365 自然日 → 近 20 日均额
#   ≥5000 万 → 250 日收益相关 ≥0.90 去重（贪心留流动性最高者）。
# 运行：聚宽回测 2024-01-01 ~ 2026-06-01（对齐 v2/v2_1 窗口），初始资金 10 万，
#   天频率。

import math

import numpy as np

from jqdata import *

TOPN, EXIT_PCT = 3, 0.40
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 90.0
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
# v2_1 补充（末尾 7 词）：堵 v2 实测漏网的跨境变体命名
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '中证2000',
              '中证A500', 'A500', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '上海金')

# 沪市跨境 ETF 代码段（513xxx），代码级硬剔除，不依赖命名
CROSS_BORDER_PREFIX = '513'


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    set_slippage(FixedSlippage(0))                  # 无滑点（冻结口径）
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=0.0001,
                             close_commission=0.0001, min_commission=0),
                   type="fund")                     # 万一单边、无最低佣金
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []
    g.day = 0
    g.trades = []                         # 卖出台账：{date, code, name, pnl}
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def rebuild_pool(dt):
    """规则动态池：时点候选 → 跨境/名称/成熟度/流动性/相关性去重（全用当时数据）。"""
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    today = dt.date()
    cands = []
    n_xb, n_name = 0, 0
    for code, row in secs.iterrows():
        if code.startswith(CROSS_BORDER_PREFIX):    # 沪市跨境段硬剔除
            n_xb += 1
            continue
        nm = row['display_name']
        # 不用裸 any()：引擎 any 被 numpy 覆盖对生成器恒真（v2 空池事故真因）
        hits = [k for k in EXCLUDE_KW if k in nm]
        if hits:
            continue
        n_name += 1
        start = row['start_date']
        if hasattr(start, 'date'):               # datetime/Timestamp → date
            start = start.date()
        if (today - start).days < MATURE_DAYS:   # date 减 date，类型稳
            continue
        cands.append(code)
    n_mature = len(cands)
    liq = []
    if cands:
        amt20 = history(20, '1d', 'money', security_list=cands).mean(axis=0)
        liq = [c for c in cands if amt20.get(c, 0) == amt20.get(c, 0)
               and amt20[c] >= LIQ_AMT20_MIN]
    if len(liq) <= TOPN:
        log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq)))
        return liq
    rets = history(250, '1d', 'close', security_list=liq).pct_change()
    corr = rets.corr(min_periods=120)
    kept = []
    for c in sorted(liq, key=lambda x: -amt20[x]):
        ok = True
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:     # NaN 视为不可比 → 保留
                ok = False
                break
        if ok:
            kept.append(c)
    log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d | 去重后 %d"
             % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                len(kept)))
    return kept


def pct_in_window(vals, lookback):
    """自身近 N 日窗口内分位（0-100），不足 60 观测返回 None（镜像 v1）。"""
    w = vals[-lookback:]
    if len(w) < CROWD_MIN_OBS:
        return None
    last = w[-1]
    return round(sum(1 for v in w if v < last) / float(len(w)) * 100, 1)


def momentum_score(close_tail, price):
    """new.py 动量评分器镜像（EtfRotation.filter_moment_rank 逐条一致）。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×加权R²；
    近 3 个日环比 min<0.95 清零（跳水否决）。失败返回 0（镜像原 except 分支）。
    """
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(price))
        if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0):
            return 0.0
        logp = np.log(prices)
        x = np.arange(len(logp))
        w = np.linspace(1, 2, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250) - 1.0
        ss_res = np.sum(w * (logp - (slope * x + intercept)) ** 2)
        ss_tot = np.sum(w * (logp - np.mean(logp)) ** 2)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        score = ann * r2
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < 0.95:
            score = 0.0
        return round(float(score), 4)
    except Exception:
        return 0.0


def run_rotation(context):
    dt = context.current_dt
    g.day += 1
    if (g.day - 1) % REBUILD_EVERY == 0:
        g.pool = rebuild_pool(dt)
    if not g.pool:
        return
    cd = get_current_data()
    today = dt.strftime('%Y-%m-%d')

    closes = history(CROWD_LOOKBACK, '1d', 'close', security_list=g.pool)
    money = history(CROWD_LOOKBACK, '1d', 'money', security_list=g.pool)
    rowsum = money.sum(axis=1)

    rows = []
    for code in g.pool:
        if cd[code].paused:
            continue                                # 停牌不可计分
        h = attribute_history(code, MIN_BARS - 1, '1d', ['close'],
                              skip_paused=True)
        if h is None or len(h) < MIN_BARS - 1:
            continue                                # 上市未满 250 根
        price = float(cd[code].last_price)
        if price <= 0:
            continue
        score = momentum_score(list(h['close'].values), price)
        comps = []
        if code in money.columns:
            share_vals = [m / s for m, s in zip(money[code].values, rowsum.values)
                          if s > 0 and m == m and m > 0]
            sp = pct_in_window(share_vals, CROWD_LOOKBACK)
            if sp is not None:
                comps.append(sp)
        cp_vals = [v for v in closes[code].values if v == v]
        cp = pct_in_window(cp_vals, CROWD_LOOKBACK)
        if cp is not None:
            comps.append(cp)
        crowd = round(sum(comps) / len(comps), 1) if len(comps) >= 2 else None
        rows.append({'code': code, 'name': get_security_name(code),
                     'price': price, 'score': score, 'crowd': crowd})

    rows.sort(key=lambda r: -r['score'])            # 稳定排序，池序解并列
    n = len(rows)
    rank = {r['code']: i + 1 for i, r in enumerate(rows)}

    # 先卖：持仓跌出前 40%（停牌/未计分/已出池 → 持有）
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue
        if r > math.ceil(EXIT_PCT * n):
            px = float(cd[sec].last_price)
            pnl = ((px - pos.avg_cost) * pos.total_amount
                   - px * pos.total_amount * 0.0001)   # 卖出万一佣金
            order_target(sec, 0)
            g.trades.append({'date': today, 'code': sec,
                             'name': get_security_name(sec), 'pnl': pnl})
            log.info("卖出 %s %s rank %d/%d 平仓盈亏 %+.0f"
                     % (sec, get_security_name(sec), r, n, pnl))

    # 后买：score>0 且拥挤度<90（None 放行）的前空槽数等权
    held = {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}
    slots = TOPN - len(held)
    if slots > 0:
        cash = context.portfolio.available_cash
        sleeve = context.portfolio.total_value
        per = min(cash / slots, sleeve / TOPN)
        bought = 0
        for r in rows:
            if bought >= slots:
                break
            code = r['code']
            if code in held or r['score'] <= 0 \
               or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX):
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            log.info("买入 %s %s %d份 @%.3f score=%.4f crowd=%s rank %d/%d"
                     % (code, r['name'], amount, px, r['score'],
                        r['crowd'], rank[code], n))
            bought += 1


def on_strategy_end(context):
    """免费日志替代交易 CSV 导出：集中度/分年/浮盈口径一次性打全。"""
    trades = g.trades
    total = float(sum([t['pnl'] for t in trades]))
    wins = [t for t in trades if t['pnl'] > 0]
    log.info("【台账】平仓 %d 笔（盈 %d 亏 %d）| Σ已实现 %+.0f"
             % (len(trades), len(wins), len(trades) - len(wins), total))
    ts = sorted(trades, key=lambda t: -t['pnl'])
    for i, t in enumerate(ts[:5]):
        log.info("【台账】TOP%d %s %s %s pnl=%+.0f"
                 % (i + 1, t['date'], t['code'], t['name'], t['pnl']))
    top3 = float(sum([t['pnl'] for t in ts[:3]]))
    ratio = top3 / total * 100 if total > 0 else float('nan')
    log.info("【台账】TOP3=%+.0f 占净利 %.1f%% | 其余 %d 笔合计 %+.0f"
             % (top3, ratio, len(ts) - 3, total - top3))
    years = {}
    for t in trades:
        d = years.setdefault(t['date'][:4], [0.0, 0])
        d[0] += t['pnl']
        d[1] += 1
    for y in sorted(years):
        log.info("【台账】分年平仓 %s: %+.0f（%d 笔）" % (y, years[y][0], years[y][1]))
    upnl = 0.0
    for s, p in context.portfolio.positions.items():
        if p.total_amount > 0:
            u = p.value - p.total_amount * p.avg_cost
            upnl += u
            log.info("【台账】期末持仓 %s %s 浮盈 %+.0f"
                     % (s, get_security_name(s), u))
    tv = context.portfolio.total_value
    sc = context.portfolio.starting_cash
    log.info("【台账】期末未实现 %+.0f | 期末权益 %.0f（总收益 %.2f%%）| 已实现占利润 %.1f%%"
             % (upnl, tv, (tv / sc - 1) * 100, total / (tv - sc) * 100))
