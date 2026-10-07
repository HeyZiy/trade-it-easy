# -*- coding: utf-8 -*-
# pool_curve_v1 —— 质量池裸持曲线探针（本地粗口径，生死筛查，不下单）
#
# 问题（quality_slow v1/v1_1 收线后定的新方向第一步）：
#   质量硬筛选建质量池（去排序层、等权全持、不挑前十），裸持能不能跑赢基准？
#   这是 A 实验；B=基准+门控、C=质量池+门控 只有在 A 有裸超额时才值得做。
#
# 实验（池规格，照抄 roe 池=pool_ic_probe_v1 的 in 组口径，主板）：
#   单季ROE年化>12%、最新报告净利同比>10%、PE<30、PB<5（无正值下界，照抄平台口径）、
#   62根日线算 vol60<35%、上市不足62根剔除；不剔 ST/解禁/停牌（见偏差声明）。
#   重建=每20交易日，信号用 T-1 收盘，等权持有全部通过者到下期重建。
#
# 结论（2026-10-05，完整记录见 README_pool.md）：**有裸超额**——年化超额 +7.51%、
#   期超额夏普 +0.587，值得进平台 PIT 精算；本口径只回答"值不值得花平台时间"。
#
# 粗口径偏差（如实声明，全部为"高估池子"或中性方向，不影响毙掉结论）：
#   1. 财务可见性用法定披露截止日（Q1→4/30、半年→8/31、Q3→10/31、年报→次年4/30），
#      只晚不早；EM 业绩报表快照是最新修订态（历史重述已并入）→ 前视高估。
#   2. 不剔 ST（ROE 屏隐式排除亏损 ST，残留少）、不剔解禁；停牌股无 bar 顺延持有。
#   3. PE/PB = 原始收盘价 ÷ 报告 TTM EPS / 最新每股净资产（股本变动有伪影）。
#   4. 单季差分需同年前一季累计在场，缺季弃该期（沿用 v1 纪律）。
#   5. 主板=60/00 开头（含原中小板），剔 B 股/科创/创业/北交。
#   6. 收益用后复权收盘，费用未计（等权全持+季频级换手，费用敏感度低，注明）。
#
# 运行：python pool_curve_v1.py fetch-fin|fetch-bars|run
#   缓存 research/studies/roe_quality/_cache/pool/，可断点续跑。
import math
import pickle
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import akshare as ak
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache" / "pool"
START = "20150601"          # 日线起点（首 rebuild 2016-01 前留 62 根缓冲）
END = "20260930"
FIRST_REBUILD = "2016-01-04"
STEP = 20                   # 交易日
VOL_BARS = 62               # 62 根 -> 60 个收益样本
ROE_MIN = 0.12              # 单季ROE年化
NP_YOY_MIN = 10.0           # 净利同比 %
PE_MAX = 30.0
PB_MAX = 5.0
VOL_MAX = 0.35              # 年化
OUTSIDER_N = 400            # 池外对照固定样本（从未过财务屏的主板股）
SEED = 42


def _deadline(period):
    """报告期法定披露截止日（只晚不早的可见性近似）。"""
    y, q = period.year, period.quarter
    if q == 4:
        y += 1
    month, day = {1: (4, 30), 2: (8, 31), 3: (10, 31), 4: (4, 30)}[q]
    return pd.Timestamp(year=y, month=month, day=day)


def _is_main_board(code):
    return code[:2] in ("60", "00")


# ---------------- 阶段一：财务（46 次调用，缓存） ----------------

def fetch_fin():
    CACHE.mkdir(parents=True, exist_ok=True)
    # 2014Q3 起：最早可见报告期 2015Q3 的单季差分与 TTM 需往前 4 个季度
    for year in range(2014, 2027):
        for q in (3, 4) if year == 2014 else (1, 2, 3, 4):
            f = CACHE / f"yjbb_{year}Q{q}.pkl"
            if f.exists():
                continue
            qend = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}[q]
            for attempt in range(3):
                try:
                    df = ak.stock_yjbb_em(date=f"{year}{qend}")
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"[fin] {year}Q{q} FAIL {e}")
                        df = None
                        break
                    time.sleep(3 * (attempt + 1))
            if df is None:
                continue
            df.to_pickle(f)
            print(f"[fin] {year}Q{q}: {len(df)} 行")
            time.sleep(0.5)


def load_fin():
    """{code: {Period: dict(eps,rev,np,bps,roe,np_yoy)}}，仅主板 A 股。"""
    fin = {}
    for f in sorted(CACHE.glob("yjbb_*.pkl")):
        df = pd.read_pickle(f)
        period = pd.Period(f.stem.split("_")[1].replace("Q", "-Q"), freq="Q")
        for row in df.to_dict("records"):
            code = str(row.get("股票代码", "")).zfill(6)
            if not _is_main_board(code) or code.startswith(("200", "900")):
                continue
            num = lambda v: pd.to_numeric(v, errors="coerce")
            rec = {
                "eps": num(row.get("每股收益")),
                "np": num(row.get("净利润-净利润")),
                "roe": num(row.get("净资产收益率")),
                "bps": num(row.get("每股净资产")),
                "np_yoy": num(row.get("净利润-同比增长")),
            }
            if pd.isna(rec["np"]) or pd.isna(rec["roe"]):
                continue
            fin.setdefault(code, {})[period] = rec
    return fin


# ---------------- 阶段二：日线（原始价 + 后复权，缓存） ----------------

def _bars(code, adjust):
    """仅新浪（东财 K 线接口拒绝直连且其 JS 解码器 py_mini_racer 非线程安全，
    并发下直接 FATAL 崩进程；新浪带 adjust 一次返回，失败不写缓存重跑重试）。"""
    sym = ("sh" if code.startswith("6") else "sz") + code
    try:
        return ak.stock_zh_a_daily(symbol=sym, start_date=START,
                                   end_date=END, adjust=adjust)
    except Exception as e:
        print(f"[bars] {code} {adjust} SINA FAIL {e}", flush=True)
        return None


def _index_daily(code):
    """新浪优先（直连稳）、东财兜底；带缓存。"""
    f = CACHE / f"idx_{code}.pkl"
    if f.exists():
        return pd.read_pickle(f)
    try:
        df = ak.stock_zh_index_daily(symbol=f"sh{code}")
        s = df.rename(columns={"date": "date", "close": "close"})[["date", "close"]]
    except Exception as e:
        print(f"[idx] {code} 新浪 FAIL {e}；试东财", flush=True)
        df = ak.index_zh_a_hist(symbol=code, period="daily",
                                start_date=START, end_date=END)
        s = df.rename(columns={"日期": "date", "收盘": "close"})[["date", "close"]]
    s["date"] = pd.to_datetime(s["date"])
    s = s.set_index("date")["close"].sort_index()
    s = s[(s.index >= pd.Timestamp(START)) & (s.index <= pd.Timestamp(END))]
    s.to_pickle(f)
    return s


def _fetch_one(code):
    raw, hfq = _bars(code, ""), _bars(code, "hfq")
    if raw is None or hfq is None or len(raw) == 0:
        return code, False
    raw = raw.rename(columns={"日期": "date", "收盘": "close"})[
        ["date", "close"]]
    hfq = hfq.rename(columns={"日期": "date", "收盘": "close"})[
        ["date", "close"]]
    raw.to_pickle(CACHE / f"bars_{code}.pkl")
    hfq.to_pickle(CACHE / f"bars_hfq_{code}.pkl")
    return code, True


def fetch_bars(codes):
    CACHE.mkdir(parents=True, exist_ok=True)
    todo = [c for c in codes
            if not (CACHE / f"bars_{c}.pkl").exists()]
    print(f"[bars] 待拉 {len(todo)} 只（已缓存 {len(codes) - len(todo)}）",
          flush=True)
    t0 = time.time()
    failed = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for i, (code, ok) in enumerate(ex.map(_fetch_one, todo)):
            if not ok:
                failed.append(code)
            if (i + 1) % 100 == 0:
                print(f"[bars] {i + 1}/{len(todo)} "
                      f"({(time.time() - t0) / (i + 1):.1f}s/只)", flush=True)
    if failed:
        print(f"[bars] 失败 {len(failed)} 只（重跑本阶段自动重试）："
              f"{failed[:20]}{'...' if len(failed) > 20 else ''}", flush=True)


def load_bars(code):
    f = CACHE / f"bars_{code}.pkl"
    fh = CACHE / f"bars_hfq_{code}.pkl"
    if not f.exists() or not fh.exists():
        return None, None
    raw, hfq = pd.read_pickle(f), pd.read_pickle(fh)
    if raw is None or hfq is None:
        return None, None
    for d in (raw, hfq):
        d["date"] = pd.to_datetime(d["date"])
        d.set_index("date", inplace=True)
    return raw["close"], hfq["close"]


# ---------------- 筛选与曲线 ----------------

def _sq(cum_map, key, period):
    """累计值 -> 单季值：Q1=累计；Q2~4 需同年前一季在场。"""
    cur = cum_map.get(period)
    if cur is None:
        return None
    if period.quarter == 1:
        return cur[key]
    prev = cum_map.get(period - 1)
    if prev is None:
        return None
    return cur[key] - prev[key]


def financial_pass(recs, period):
    """单报告期财务屏。recs={Period: rec}；返回 bool。"""
    cur = recs.get(period)
    if cur is None or pd.isna(cur["np_yoy"]) or cur["np_yoy"] <= NP_YOY_MIN:
        return False
    roe_cum = cur["roe"]
    np_cum = cur["np"]
    if pd.isna(roe_cum) or roe_cum <= 0.5 or pd.isna(np_cum):
        return False                      # equity≈np/roe 需同号且分母可用
    np_sq = _sq(recs, "np", period)
    if np_sq is None or pd.isna(np_sq) or np_sq <= 0:
        return False
    # 单季ROE年化 = 4 × 单季净利 × (累计ROE/累计净利)
    roe_sq_ann = 4.0 * np_sq * (roe_cum / 100.0) / np_cum
    if roe_sq_ann <= ROE_MIN:
        return False
    # TTM EPS（最近4个可得单季）
    eps_ttm, n = 0.0, 0
    for k in range(4):
        e = _sq(recs, "eps", period - k)
        if e is None or pd.isna(e):
            return False
        eps_ttm += e
        n += 1
    if n < 4 or eps_ttm == 0:
        return False                    # PE<30 无正值下界（负 TTM 照抄平台口径）
    cur["eps_ttm"] = eps_ttm              # 供 PE 用（缓存到当期记录）
    return True


def _prev_trade_day(idx_index, dt):
    """idx_index：升序交易日索引；返回 dt 之前最近一个交易日。"""
    return idx_index[max(idx_index.searchsorted(dt) - 1, 0)]


def build_rebuild_dates(index_close):
    days = [d for d in index_close.index
            if d >= pd.Timestamp(FIRST_REBUILD)]
    return days[::STEP]


def run():
    fin = load_fin()
    print(f"[run] 有财务记录的主板股 {len(fin)}")

    # 指数日线：000906 主对照 / 000905 次对照（新浪直连优先）
    idx = {c: _index_daily(c) for c in ("000906", "000905")}
    dates = build_rebuild_dates(idx["000906"])
    print(f"[run] 重建日 {len(dates)} 个：{dates[0].date()} ~ {dates[-1].date()}")

    # 每 rebuild 的财务屏（PE/PB 需价格，后置到 bars 就绪后）
    bar_codes = set()
    pools = {}
    idx_days = idx["000906"].index
    for T in dates:
        asof = _prev_trade_day(idx_days, T)
        vis = [p for p in {p for recs in fin.values() for p in recs}
               if _deadline(p) <= asof]
        latest = max(vis) if vis else None
        if latest is None:
            pools[T] = set()
            continue
        members = set()
        for code, recs in fin.items():
            # 只要求"最新可见报告"过屏；用最近 4 期差分做 TTM
            if financial_pass(recs, latest):
                members.add(code)
        pools[T] = members
        bar_codes |= members
    sizes = [len(v) for v in pools.values()]
    print(f"[run] 财务屏后池规模：均值 {sum(sizes)/len(sizes):.0f} "
          f"中位 {sorted(sizes)[len(sizes)//2]} 最小 {min(sizes)} 最大 {max(sizes)}")

    never = set(fin) - bar_codes
    random.seed(SEED)
    outsiders = random.sample(sorted(never), min(OUTSIDER_N, len(never)))
    bar_codes |= set(outsiders)
    print(f"[run] 待拉日线 {len(bar_codes)} 只"
          f"（池成员并集 {len(bar_codes) - len(outsiders)} + 池外 {len(outsiders)}）")
    (CACHE / "pools_fin.pkl").write_bytes(pickle.dumps(
        {"pools": pools, "outsiders": outsiders, "dates": [d.isoformat() for d in dates]}))

    fetch_bars(sorted(bar_codes))
    print("[run] 阶段二（日线）完成；再跑 python pool_curve_v1.py run2")


def run2():
    st = pickle.loads((CACHE / "pools_fin.pkl").read_bytes())
    pools = {pd.Timestamp(k): v for k, v in st["pools"].items()}
    outsiders = set(st["outsiders"])
    dates = [pd.Timestamp(d) for d in st["dates"]]

    bars_raw, bars_hfq = {}, {}
    all_codes = set().union(*pools.values()) | outsiders
    for code in sorted(all_codes):
        r, h = load_bars(code)
        if h is not None:
            bars_hfq[code] = h
        if r is not None:
            bars_raw[code] = r
    print(f"[run2] 日线就绪 {len(bars_hfq)} 只")

    idx = {c: pd.read_pickle(CACHE / f"idx_{c}.pkl") for c in ("000906", "000905")}

    # 财务屏（含 PE/PB，需要原始价）重跑一遍：financial_pass 存 eps_ttm
    fin = load_fin()
    pools_full, pool_sizes = {}, []
    idx_days = idx["000906"].index
    for T in dates:
        asof = _prev_trade_day(idx_days, T)
        vis = [p for p in {p for recs in fin.values() for p in recs}
               if _deadline(p) <= asof]
        latest = max(vis) if vis else None
        members = set()
        if latest is not None:
            for code, recs in fin.items():
                if not financial_pass(recs, latest):
                    continue
                raw = bars_raw.get(code)
                if raw is None or asof not in raw.index:
                    continue
                bps = recs[latest]["bps"]
                if pd.isna(bps) or bps <= 0:
                    continue
                pe = raw.loc[asof] / recs[latest]["eps_ttm"]
                pb = raw.loc[asof] / bps
                if pd.isna(pe) or pd.isna(pb) or pe >= PE_MAX or pb >= PB_MAX:
                    continue
                # 波动屏：62 根日线（用后复权，原始价含份额折算伪影）
                h = bars_hfq.get(code)
                if h is None or asof not in h.index:
                    continue
                pos = h.index.get_loc(asof)
                if pos < VOL_BARS - 1:
                    continue
                w = h.iloc[pos - VOL_BARS + 1: pos + 1]
                if w.isna().sum() > 0:
                    continue
                ret = w.pct_change().dropna()
                vol = ret.std() * math.sqrt(250)
                if vol >= VOL_MAX:
                    continue
                members.add(code)
        pools_full[T] = members
        pool_sizes.append(len(members))
    print(f"[run2] 全屏后池规模：均值 {sum(pool_sizes)/len(pool_sizes):.0f} "
          f"中位 {sorted(pool_sizes)[len(pool_sizes)//2]}")
    # 存档全屏池（B/C 门控实验复用）
    (CACHE / "pools_full.pkl").write_bytes(pickle.dumps(
        {"dates": [d.isoformat() for d in dates],
         "pools": {d.isoformat(): sorted(v) for d, v in pools_full.items()}}))

    # 曲线：每期等权全持收益 + 池外对照 + 指数
    rows = []
    for i, T in enumerate(dates[:-1]):
        Tn = dates[i + 1]
        S = _prev_trade_day(idx_days, T)      # 信号日收盘
        E = _prev_trade_day(idx_days, Tn)     # 期末
        members = [c for c in pools_full[T] if c in bars_hfq]
        rets = []
        for c in members:
            h = bars_hfq[c]
            if S in h.index and E in h.index:
                rets.append(h.loc[E] / h.loc[S] - 1)
        out_rets = []
        for c in outsiders:
            h = bars_hfq.get(c)
            if h is None or S not in h.index or E not in h.index:
                continue
            pos = h.index.get_loc(S)
            if pos < VOL_BARS - 1:
                continue
            out_rets.append(h.loc[E] / h.loc[S] - 1)
        row = {"date": T, "pool_n": len(members),
               "pool_ret": sum(rets) / len(rets) if rets else float("nan"),
               "out_ret": sum(out_rets) / len(out_rets) if out_rets else float("nan")}
        for c, s in idx.items():
            if S in s.index and E in s.index:
                row[f"idx_{c}"] = s.loc[E] / s.loc[S] - 1
            else:
                row[f"idx_{c}"] = float("nan")
        rows.append(row)
    df = pd.DataFrame(rows).set_index("date")
    df.to_csv(HERE / "pool_curve_v1.csv")
    print(df[["pool_n", "pool_ret", "out_ret", "idx_000906"]]
          .describe().to_string())

    # 汇总：累计曲线 + 超额夏普 + 年度超额 + 池内外 gap
    for col, name in (("idx_000906", "中证800"), ("idx_000905", "中证500")):
        nav = (1 + df["pool_ret"].fillna(0)).cumprod()
        bnav = (1 + df[col].fillna(0)).cumprod()
        exc = nav / bnav
        ann = (exc.iloc[-1]) ** (245.0 / (20 * len(df))) - 1
        e_ret = df["pool_ret"] - df[col]
        sharpe = e_ret.mean() / e_ret.std() * math.sqrt(245 / 20) if e_ret.std() > 0 else float("nan")
        dd = (exc / exc.cummax() - 1).min()
        print(f"[汇总] vs {name}: 累计超额 {(exc.iloc[-1]-1)*100:+.1f}% "
              f"年化超额 {ann*100:+.2f}% 期超额夏普 {sharpe:+.3f} "
              f"超额最大回撤 {dd*100:+.1f}%")
    gap = (df["pool_ret"] - df["out_ret"]).dropna()
    t = gap.mean() / gap.std() * math.sqrt(len(gap)) if gap.std() > 0 else float("nan")
    print(f"[汇总] 池内外 20 日期 gap：均值 {gap.mean()*100:+.3f}pp "
          f"t≈{t:+.2f}（pool_ic_probe 参考 +0.20pp / t=+0.61）")
    print(f"[汇总] 平均池规模 {df['pool_n'].mean():.0f}")
    churn = []
    for i in range(1, len(dates) - 1):
        a, b = pools_full[dates[i]], pools_full[dates[i + 1]]
        if a:
            churn.append(1 - len(a & b) / len(a | b))
    print(f"[汇总] 期均成员换手(Jaccard 距离) {sum(churn)/len(churn)*100:.0f}%")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"fetch-fin": fetch_fin, "run": run, "run2": run2}[stage]()
