# -*- coding: utf-8 -*-
# pool_audit_v1 —— 质量池裸持 A 实验的平台 PIT 精算（信号层，不下单）
#
# 问题：本地粗口径筛查（pool_curve_v1，年化超额 +7.51% vs 中证800、
#   期超额夏普 +0.587，已过线）有多少是粗口径幻觉？本脚本用平台数据重画
#   同一条曲线，量化真实 PIT 口径下的残余超额。
#
# 实验（与本地版的唯一差异 = 数据口径，池规格/时钟/等权全持/区间全部不变）：
#   1. 财务可见性：pubDate 精确对齐（本地=法定披露截止日近似）；
#      平台 indicator 表为披露时点快照（本地 EM 快照含历史重述，前视）。
#   2. ST/停牌/解禁剔除：本版启用（本地版未做）。
#   3. PE/PB：平台 valuation 表（本地=原始价÷报告EPS/BPS 手搓，有股本伪影）。
#   4. 波动窗 61 根（roe 池原口径；本地 62 根，差 1 个收益样本）。
#   5. 无池外对照（本地版有 400 只对照组；对表用曲线与年度超额即可）。
# 单季ROE 用 indicator.roe×4 年化近似（沿用 roe v1.1 的平台口径）。
#
# 结论（2026-10-05 平台实跑，完整记录见 README_pool.md）：vs 中证800 年化超额
#   +7.49%、期超额夏普 +0.567，粗口径幻觉在总量上很小——本地-平台差仅 -0.02pp /
#   -0.020；但年度分布有补偿性挪移（2016 +13.0→+23.5、2021 +30.7→+19.1、
#   2024 +4.2→-1.5），粗口径不可用于年度归因。B/C 门控实验据此开工。
#
# 运行（聚宽回测，整文件粘贴）：区间 2016-01-04 ~ 2026-09-01，基准中证800，
#   频率天，资金任意（不下单，资金不影响结果）。每 20 交易日一轮全市场
#   indicator/valuation/ST/解禁/日线查询，预计 20~40 分钟。
# 输出：每期一行日志 + 结束时 write_file 曲线与成员名单（不可用时以日志为准，
#   日志原文贴回，不要自行改字段）。
import math

import pandas as pd

from jqdata import *

ROTATE_EVERY = 20        # 重建间隔（交易日），与本地一致
ROE_MIN = 12.0           # ROE 下限（%，indicator.roe 单季×4 年化近似）
PE_MAX = 30.0            # PE(TTM) 上限（无正值下界，照抄 roe 池）
PB_MAX = 5.0             # PB 上限
NP_YOY_MIN = 10.0        # 净利润同比增长下限（%）
VOL60_MAX = 35.0         # 60 日年化波动率上限（%）
VOL_LOOKBACK = 61        # 61 根日线 -> 60 个收益样本，截止 T-1
BAN_WINDOW = 90          # 未来解禁窗口（自然日）
IDX_MAIN = '000906.XSHG'  # 中证800（主判读）
IDX_SUB = '000905.XSHG'   # 中证500（参照）


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark(IDX_MAIN)
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.day = 0
    g.prev_pool = None         # 上一轮池成员
    g.prev_signal_day = None   # 上一轮信号日（一期起点 S0）
    g.rows = []                # (rebuild日, 生效成员数, 池收益, c800, c500)
    g.pools = []               # (rebuild日, 成员名单)
    g.churn = []
    log.info("pool A 精算：roe 池全量等权（无排序无门控）、%d 日重建、不下单；"
             "pubDate 对齐 + ST/解禁/停牌剔除" % ROTATE_EVERY)
    run_daily(rebuild_check, time='14:55')


def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。（照抄 roe v1.1）"""
    if pd.isna(value):
        return pd.NaT
    text = str(value).strip().lower()
    try:
        if (len(text) == 6 and text[:4].isdigit()
                and text[4] == 'q' and text[5] in '1234'):
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _prepare_fundamentals(rows, asof):
    """仅保留 asof 已披露（pubDate<=asof）的有效季度指标，ROE×4 年化。（roe v1.1）"""
    ind = rows.copy().set_index('code')
    cutoff = pd.Timestamp(asof).normalize()
    report_dates = ind['statDate'].apply(_report_date)
    publication_dates = pd.to_datetime(ind['pubDate'], errors='coerce')
    visible = (report_dates.notna() & publication_dates.notna()
               & (report_dates <= cutoff) & (publication_dates <= cutoff))
    ind = ind.loc[visible].copy()
    for field in ('roe', 'inc_net_profit_year_on_year'):
        ind[field] = pd.to_numeric(ind[field], errors='coerce')
    ind['roe_ann'] = ind['roe'] * 4.0
    ind = ind.replace([float('inf'), float('-inf')], float('nan'))
    return ind.dropna(subset=['roe_ann', 'inc_net_profit_year_on_year'])


def _prev_trade_day(dt):
    """T-1 交易日：统一财务、估值和历史日线的可见截止日。（roe v1.1）"""
    return get_trade_days(end_date=dt.date(), count=2)[0]


def _mainboard_pool(context):
    """全 A 主板（时点）：剔科创板(68x)、创业板(30x)、北交所(4/8/92)。（roe v1.1）"""
    secs = get_all_securities(['stock'], date=context.current_dt)
    return [c for c in secs.index
            if not (c[:2] in ('68', '30') or c[0] in ('4', '8') or c[:2] == '92')]


def _st_filter(cands, dt):
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        return [c for c in cands if not bool(st[c].iloc[-1])]
    except Exception:
        return cands


def _ban_filter(cands, dt):
    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = set(rows['code']) if rows is not None and len(rows) else set()
        return [c for c in cands if c not in banned]
    except Exception:
        return cands


def build_pool(context, asof):
    """roe 池全量成员：筛链条照抄 roe_rotation_v1_1，去掉排序与 TOP-N。"""
    dt = context.current_dt
    pool = _mainboard_pool(context)
    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate,
              indicator.pubDate), date=asof)
    ind = _prepare_fundamentals(ind, asof)
    ind = ind[(ind['roe_ann'] > ROE_MIN)
              & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    cands = [c for c in pool if c in ind.index]
    if not cands:
        return []
    cands = _st_filter(cands, dt)
    if not cands:
        return []
    val = get_fundamentals(
        query(valuation).filter(valuation.code.in_(cands)), date=asof)
    val = val.set_index('code')
    val = val[(val['pe_ratio'] < PE_MAX) & (val['pb_ratio'] < PB_MAX)]
    cands = [c for c in cands if c in val.index]
    if not cands:
        return []
    cd = get_current_data()
    cands = [c for c in cands if not cd[c].paused]
    closes = history(VOL_LOOKBACK, '1d', 'close', security_list=cands)
    cands = [c for c in cands if closes[c].notna().sum() >= VOL_LOOKBACK]
    if not cands:
        return []
    rets = closes[cands].pct_change()
    vol60 = rets.std() * math.sqrt(250) * 100
    cands = [c for c in cands if vol60[c] < VOL60_MAX]
    if not cands:
        return []
    return _ban_filter(cands, dt)


def _close_matrix(codes, start, end):
    """成员后复权收盘矩阵 date×code；skip_paused=False 顺延停牌收盘。
    平台 get_price 多标的的返回形态随版本而异（长表 time/code/close、Panel、
    MultiIndex），逐一折成 date×code；识别不了才原样返回。"""
    raw = get_price(codes, start_date=start, end_date=end,
                    fields=['close'], skip_paused=False, fq='post',
                    panel=False)
    if isinstance(raw, pd.DataFrame) and {'time', 'code', 'close'} <= set(raw.columns):
        df = raw.pivot(index='time', columns='code', values='close')
        df.index = pd.to_datetime(df.index).normalize()
        return df
    if isinstance(raw, pd.Panel):
        if 'close' in raw.items:
            df = raw['close']
        elif 'close' in raw.minor_axis:
            df = raw.minor_xs('close').T
        else:
            df = raw.major_xs('close').T
        if isinstance(df.columns, pd.DatetimeIndex) and \
                not isinstance(df.index, pd.DatetimeIndex):
            df = df.T
        return df
    if isinstance(raw.index, pd.MultiIndex):
        df = raw.unstack('code')
        if isinstance(df.columns, pd.MultiIndex):
            df = df.xs('close', axis=1)
        return df
    return raw


def _period_ret(codes, start, end):
    """等权全持一期收益：成员 close(end)/close(start)-1 均值，缺端点者剔除。"""
    if not codes or start is None or end is None or start >= end:
        return float('nan'), 0
    s0, s1 = pd.Timestamp(start), pd.Timestamp(end)   # date 与行索引判等需同型
    df = _close_matrix(codes, s0, s1)
    rets = []
    for c in codes:
        if c not in df.columns:
            continue
        s = df[c]
        if s0 not in s.index or s1 not in s.index:
            continue
        r = s.loc[s1] / s.loc[s0] - 1
        if pd.notna(r):
            rets.append(r)
    if not rets:
        if not getattr(g, 'diag_done', False):
            g.diag_done = True
            log.warning("[pool A] 诊断: matrix=%s shape=%s cols[:3]=%s "
                        "idx[:2]=%s"
                        % (type(df).__name__, df.shape,
                           list(df.columns[:3]), list(df.index[:2])))
        return float('nan'), 0
    return sum(rets) / len(rets), len(rets)


def _idx_ret(code, start, end):
    p = get_price(code, start_date=start, end_date=end, fields=['close'])
    if p is None or len(p) == 0:
        return float('nan')
    s = p['close']
    s0, s1 = pd.Timestamp(start), pd.Timestamp(end)
    if s0 not in s.index or s1 not in s.index:
        return float('nan')
    return float(s.loc[s1] / s.loc[s0] - 1)


def rebuild_check(context):
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    dt = context.current_dt
    T = pd.Timestamp(dt).normalize()      # 平台 current_dt 是原生 datetime
    asof = _prev_trade_day(dt)            # 本期终点 = 上一交易日收盘
    # 先结算上一期 [prev_signal_day, asof]
    if g.prev_pool is not None:
        pr, n_eff = _period_ret(g.prev_pool, g.prev_signal_day, asof)
        r8 = _idx_ret(IDX_MAIN, g.prev_signal_day, asof)
        r5 = _idx_ret(IDX_SUB, g.prev_signal_day, asof)
        g.rows.append((T, n_eff, pr, r8, r5))
        log.info("[pool A] %s 生效%d/%d 池%+.2f%% c800%+.2f%% c500%+.2f%%"
                 % (dt.strftime('%F'), n_eff, len(g.prev_pool),
                    (pr if pd.notna(pr) else 0.0) * 100,
                    (r8 if pd.notna(r8) else 0.0) * 100,
                    (r5 if pd.notna(r5) else 0.0) * 100))
    pool = build_pool(context, asof)
    if g.prev_pool:
        a, b = set(g.prev_pool), set(pool)
        if a and b:
            g.churn.append(1 - len(a & b) / len(a | b))
    g.pools.append((T, list(pool)))
    g.prev_pool = pool
    g.prev_signal_day = asof


def on_strategy_end(context):
    # 收尾最后一期（不足 20 日的部分期，单独标注）
    if g.prev_pool is not None and g.prev_signal_day is not None:
        end_day = get_trade_days(end_date=context.current_dt.date(), count=1)[0]
        if pd.Timestamp(end_day) > pd.Timestamp(g.prev_signal_day):
            pr, n_eff = _period_ret(g.prev_pool, g.prev_signal_day, end_day)
            r8 = _idx_ret(IDX_MAIN, g.prev_signal_day, end_day)
            r5 = _idx_ret(IDX_SUB, g.prev_signal_day, end_day)
            g.rows.append((pd.Timestamp(end_day), n_eff, pr, r8, r5))
            log.info("[pool A] 末期（部分期）%s 生效%d 池%+.2f%% "
                     "c800%+.2f%% c500%+.2f%%"
                     % (pd.Timestamp(end_day).strftime('%F'), n_eff,
                        (pr if pd.notna(pr) else 0.0) * 100,
                        (r8 if pd.notna(r8) else 0.0) * 100,
                        (r5 if pd.notna(r5) else 0.0) * 100))
    df = pd.DataFrame(g.rows, columns=['date', 'n_eff', 'pool', 'c800', 'c500'])
    df = df.dropna(subset=['pool', 'c800'])
    if df.empty:
        log.warning("[pool A] 无有效期数据，检查日志报错")
        return
    ppy = 245.0 / ROTATE_EVERY
    nav = (1 + df['pool']).cumprod()
    lines = ["", "==================== pool A 精算汇总 ===================="]
    for label, col in (("中证800", 'c800'), ("中证500", 'c500')):
        bnav = (1 + df[col]).cumprod()
        exc = nav / bnav
        e = df['pool'] - df[col]
        sharpe = e.mean() / e.std() * math.sqrt(ppy) if e.std() > 0 else float('nan')
        ann = exc.iloc[-1] ** (ppy / len(df)) - 1
        dd = (exc / exc.cummax() - 1).min()
        lines.append("vs %s: 累计超额 %+.1f%% | 年化超额 %+.2f%% | 期超额夏普 %+.3f"
                     " | 超额最大回撤 %+.1f%%"
                     % (label, (exc.iloc[-1] - 1) * 100, ann * 100, sharpe, dd * 100))
    df['year'] = df['date'].dt.year
    gby = df.groupby('year')
    lines.append("年度（池% vs c800%，超额pp）: " + "; ".join(
        "%d %+.1f/%+.1f/%+.1f" % (y, r['pool'].sum() * 100,
                                  r['c800'].sum() * 100,
                                  (r['pool'] - r['c800']).sum() * 100)
        for y, r in gby))
    lines.append("期数 %d | 平均池规模 %.0f | 期均成员换手 %.0f%% | 平均生效占比 %.0f%%"
                 % (len(df), df['n_eff'].mean(),
                    (sum(g.churn) / len(g.churn) * 100) if g.churn else 0.0,
                    (df['n_eff'] / df['n_eff'].clip(lower=1)).mean() * 100
                    if 'n_eff' in df else 0.0))
    log.info("\n".join(lines))
    try:
        write_file('pool_audit_v1_curve.csv', df.to_csv(index=False))
        pools_txt = 'date,members\n' + '\n'.join(
            '%s,%s' % (d.strftime('%F'), ';'.join(cs)) for d, cs in g.pools)
        write_file('pool_audit_v1_pools.csv', pools_txt)
        log.info("[pool A] 已写出 pool_audit_v1_curve.csv / "
                 "pool_audit_v1_pools.csv")
    except Exception as e:
        log.warning("[pool A] write_file 不可用（%s），以上日志行为准" % e)
