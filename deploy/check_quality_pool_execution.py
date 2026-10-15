"""盘中检查质量池当天执行数据；不读写台账/状态、不出报告、不推送。"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codes', nargs='+', default=['600519', '000001'])
    args = parser.parse_args()
    from src.config import setup_env
    setup_env()
    from data_provider.codes import is_a_stock_code, market_suffix
    from src.quality_pool import feeds
    import pandas as pd

    if any(not is_a_stock_code(c) or market_suffix(c) not in ('SH', 'SZ') for c in args.codes):
        parser.error('仅支持沪深 A 股代码')
    day = pd.Timestamp.now(tz='Asia/Shanghai').date().isoformat()
    frame = feeds.fetch_execution_status(args.codes, day)
    limits = feeds.day_limits(args.codes, day, {}, status=frame)
    result = []
    for code in args.codes:
        flags_known = code in frame.index and all(
            pd.notna(frame.at[code, col]) for col in ('is_st', 'is_suspended'))
        result.append(dict(
            code=code, status_known=bool(flags_known), limits_known=code in limits,
            is_st=bool(frame.at[code, 'is_st']) if flags_known else None,
            is_suspended=bool(frame.at[code, 'is_suspended']) if flags_known else None))
    print(json.dumps(dict(date=day, stocks=result), ensure_ascii=False, indent=2), flush=True)
    if not all(row['status_known'] and row['limits_known'] for row in result):
        raise RuntimeError('当天执行数据未全部确认；检查权限、返回字段和行情新鲜度')
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
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
