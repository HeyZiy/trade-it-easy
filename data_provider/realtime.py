"""按来源优先级获取现价，每个来源只接收还没有有效价格的代码。"""
import logging
import math
from numbers import Real
from time import monotonic

logger = logging.getLogger(__name__)


def fetch_prices(codes, fetchers, source_priority) -> dict[str, float]:
    """接收规范化代码，返回有效现价。缺价不入结果，由业务决定如何处理。"""
    pending = dict.fromkeys(codes)
    if not pending:
        return {}
    sources = {}
    for fetcher in fetchers:
        for name, kwargs in getattr(fetcher, "REALTIME_VARIANTS", {}).items():
            sources.setdefault(name, (fetcher, kwargs))
    priority = dict.fromkeys(source.strip().lower() for source in source_priority if source.strip())
    unknown = priority.keys() - sources.keys()
    if unknown:
        logger.warning("[现价] 优先级配置含未声明的源名 %s", sorted(unknown))

    prices = {}
    for source in priority:
        if not pending:
            break
        adapter = sources.get(source)
        if adapter is None:
            continue
        fetcher, kwargs = adapter
        requested_codes = list(pending)
        requested = len(requested_codes)
        started = monotonic()
        try:
            result = fetcher.get_realtime_prices(requested_codes, **kwargs)
        except Exception as error:
            logger.info("[现价] %s 失败，用时 %.2fs: %s",
                        source, monotonic() - started, type(error).__name__)
            continue
        for code, price in result.items():
            if code not in pending or not isinstance(price, Real) or isinstance(price, bool):
                continue
            if math.isfinite(price) and price > 0:
                prices[code] = float(price)
                del pending[code]
        logger.info("[现价] %s 返回 %d/%d，用时 %.2fs",
                    source, requested - len(pending), requested, monotonic() - started)
    if pending:
        logger.warning("[现价] %d 只标的缺少有效价格，前 10 只 %s", len(pending), list(pending)[:10])
    return prices
