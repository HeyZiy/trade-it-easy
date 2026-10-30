"""Split the former shared ledger without rescaling trades. Dry-run by default."""

import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

from src.trade_ledger.closing_prices import ClosingPrices, _at
from src.trade_ledger.ledger import derive, load_trades
from src.trade_ledger.performance import collect_close, performance_path

INDUSTRY_LEDGER = "trade_ledger_industry_momentum.jsonl"


class MigrationPrices(ClosingPrices):
    """Fetch each ETF history once and select exact unadjusted trading dates."""

    def __init__(self):
        super().__init__()
        self.frames = {}

    def close_prices(self, codes, asof):
        from data_provider.bars import get_etf_daily

        result = {}
        for code in codes:
            if code not in self.frames:
                self.frames[code] = get_etf_daily(code, adjust=None)
            value = _at(self.frames[code], asof, "date")
            if value is not None:
                result[code] = value
        return result


def prepare_split(root, feed, trading_dates):
    """Build both ledgers and their independent histories without modifying source files."""
    root = Path(root)
    source = root / "data" / "trade_ledger.jsonl"
    target = source.with_name(INDUSTRY_LEDGER)
    if target.exists() or performance_path(target).exists():
        raise ValueError("行业独立台账已存在，拒绝重复迁移")
    source_bytes = source.read_bytes()
    old_performance_bytes = performance_path(source).read_bytes()
    original = json.loads(old_performance_bytes)
    groups = {"core": [], "satellite": []}
    for line in source_bytes.splitlines(keepends=True):
        if not line.strip():
            continue
        tag = json.loads(line).get("account", "core")
        if tag not in groups:
            raise ValueError(f"共享台账存在未预期账户标签：{tag}")
        groups[tag].append(line)
    if not all(groups.values()):
        raise ValueError("原台账必须同时有核心和行业记录")
    days = sorted({row["date"] for row in original.get("records", [])})
    if not days:
        raise ValueError("原台账无收盘成绩日期，无法验证拆账结果")
    files = {}
    summaries = []
    with tempfile.TemporaryDirectory(prefix="industry-split-") as staging:
        for tag, name in (("core", source.name), ("satellite", target.name)):
            ledger = Path(staging) / name
            ledger.write_bytes(b"".join(groups[tag]))
            trades = load_trades(ledger)
            first_day = min(t.date for t in trades)
            if first_day > days[-1]:
                raise ValueError(f"{tag} 尚无已采集的收盘日，拒绝迁移")
            for day in days:
                if day < first_day:
                    continue
                derive(trades, day)  # 拆分后每个日期的卖出必须由本账买入支持。
                state = collect_close(
                    ledger, day, original["benchmark"],
                    close_prices=feed.close_prices, index_prices=feed.index_prices,
                    trading_dates=trading_dates,
                )
            latest = state["records"][-1]
            if latest.get("nav") is None or latest.get("issues"):
                raise ValueError(f"{tag} 最新成绩不完整，保留原账：{latest.get('issues')}")
            files[root / "data" / name] = ledger.read_bytes()
            files[performance_path(root / "data" / name)] = performance_path(ledger).read_bytes()
            summaries.append({"account": tag, "trades": len(trades),
                              "holdings": len(derive(trades, days[-1]).positions),
                              "date": latest["date"], "issues": latest["issues"]})
    return {"source": source, "source_bytes": source_bytes,
            "performance_bytes": old_performance_bytes,
            "files": files, "summary": summaries}


def apply_split(plan):
    """Keep the original mixed files for rollback, then atomically replace each result."""
    source = plan["source"]
    if (source.read_bytes() != plan["source_bytes"]
            or performance_path(source).read_bytes() != plan["performance_bytes"]):
        raise ValueError("迁移期间原始数据已变化，拒绝覆盖")
    target = source.with_name(INDUSTRY_LEDGER)
    if target.exists() or performance_path(target).exists():
        raise ValueError("迁移期间目标台账已出现，拒绝覆盖")
    archive = source.parent / "migrations" / datetime.now().strftime("industry-split-%Y%m%d-%H%M%S")
    archive.mkdir(parents=True, exist_ok=False, mode=0o700)
    (archive / source.name).write_bytes(plan["source_bytes"])
    (archive / performance_path(source).name).write_bytes(plan["performance_bytes"])
    staged = {}
    try:
        for path, content in plan["files"].items():
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as temp:
                temp.write(content)
                staged[path] = Path(temp.name)
        for path, temporary in staged.items():
            temporary.replace(path)
    except Exception:
        # 回退已提交的文件，避免只拆了一半。
        source.write_bytes(plan["source_bytes"])
        performance_path(source).write_bytes(plan["performance_bytes"])
        target.unlink(missing_ok=True)
        performance_path(target).unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return archive


def main():
    import fcntl

    from src.config import setup_env
    from src.trading_calendar import get_trading_dates

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    setup_env()
    with ExitStack() as locks:
        for name in ("etf", "industry_momentum", "portfolio-report"):
            lock = locks.enter_context(open(f"/tmp/{name}.lock", "a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = prepare_split(args.root.resolve(), MigrationPrices(), get_trading_dates)
        print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                          "source_sha256": hashlib.sha256(plan["source_bytes"]).hexdigest(),
                          "accounts": plan["summary"]}, ensure_ascii=False))
        if args.apply:
            print("original_files:", apply_split(plan))


if __name__ == "__main__":
    main()
