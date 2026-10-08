"""在线验收质量池取数：不写策略状态/台账、不生成报告、不推送消息。

默认两只股票，--all 查询完整主板宇宙。仅复权因子仍写 SDK 缓存。
服务器可配 /usr/bin/time -v 测量整轮峰值内存，见 quality_pool_recovery.md。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _peak_rss_mib():
    try:
        import resource
    except ImportError:
        return None
    # 该验收指标面向 Linux ECS；ru_maxrss 的 Linux 单位为 KiB。
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _cache_bytes(cache: Path) -> int:
    if not cache.exists():
        return 0
    return sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True, help="财务/行情截止日 T−1，YYYY-MM-DD")
    parser.add_argument("--signal-date", required=True, help="信号日 T，YYYY-MM-DD")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--codes", nargs="+", default=["600519", "000001"])
    group.add_argument("--all", action="store_true", help="查询完整主板宇宙")
    args = parser.parse_args()

    os.chdir(ROOT)
    from src.config import setup_env
    setup_env()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    from src.quality_pool import feeds, screener
    from data_provider.fetchers.amazingdata_fetcher import AMAZINGDATA_CACHE_DIR
    import pandas as pd

    asof = pd.Timestamp(args.asof)
    signal_day = pd.Timestamp(args.signal_date)
    if asof >= signal_day:
        parser.error("--asof 必须早于 --signal-date")
    asof_str = asof.strftime("%Y-%m-%d")
    signal_str = signal_day.strftime("%Y-%m-%d")
    codes = sorted(feeds.fetch_universe(signal_str)) if args.all else args.codes
    if any(len(c) != 6 or not c.isdigit() or c[:2] not in ("60", "00") for c in codes):
        parser.error("--codes 仅支持 60/00 开头的六位主板代码")
    info_cache = Path(AMAZINGDATA_CACHE_DIR) / "infodata"
    before = _cache_bytes(info_cache)
    started = time.monotonic()

    closes = feeds.fetch_closes(codes, asof_str)
    raw_closes = feeds.raw_closes_at(codes, asof_str)
    fundamentals = feeds.fetch_fundamentals(codes, asof_str, raw_closes)
    status = feeds.fetch_status(codes, signal_str)
    unlocks = feeds.fetch_unlock_codes(codes, signal_str)
    pool = screener.build_pool(set(codes), fundamentals, status, closes, unlocks)
    ranked, _ = screener.rank_pool(pool, closes)
    after = _cache_bytes(info_cache)
    print(json.dumps({
        "codes": len(codes), "asof": asof_str, "signal_day": signal_str,
        "close_shape": list(closes.shape), "raw_closes": len(raw_closes),
        "fundamentals": len(fundamentals), "status": len(status),
        "unlocks": len(unlocks), "pool": len(pool), "ranked": len(ranked),
        "infodata_bytes_before": before, "infodata_bytes_after": after,
        "infodata_growth_bytes": after - before,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "peak_rss_mib_linux": _peak_rss_mib(),
    }, ensure_ascii=False, indent=2), flush=True)
    if after != before:
        raise RuntimeError("InfoData 缓存发生变化，停止恢复 cron 并核对服务器 SDK 版本")
    if closes.empty or not raw_closes or fundamentals.empty or status.empty:
        raise RuntimeError("必要数据为空，需确认日期、数据权限与字段口径后再恢复 cron")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    # 同策略入口：SDK 回调线程可能阻止正常退出，先刷新输出再终止。
    exit_code = 1
    try:
        exit_code = main()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
