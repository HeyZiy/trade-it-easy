"""获取单只股票分析输入；只有调用函数时才访问共享数据会话。"""
from .analysis import select_categories
from .catalog import DAILY_CATEGORIES


def fetch_inputs(code, begin_date, end_date, *, categories=None):
    """返回可传入 analyze_fundamentals 的关键字参数。

    日期仅限制行情区间，财报沿用官方示例的全量查询；该接口不承诺历史 PIT。
    """
    from data_provider.fetchers.amazingdata.client import client
    from data_provider.fetchers.amazingdata.market import get_raw_kline

    selected = select_categories(categories)
    info = client.get_info_data()
    result = {'code': code, 'categories': selected}
    for key, method in (('bs', 'get_balance_sheet'), ('inc', 'get_income'), ('cf', 'get_cash_flow')):
        frames = getattr(info, method)([code], is_local=False)
        result[key] = frames.get(code) if frames is not None else None
        if result[key] is None or result[key].empty:
            raise ValueError(f'{code} 的 {key} 财报为空')
    if any(category in DAILY_CATEGORIES for category in selected):
        result['equity_structure'] = info.get_equity_structure([code], is_local=False)
        result['dividend'] = info.get_dividend([code], is_local=False)
        result['kline'] = get_raw_kline([code], begin_date, end_date).get(code)
        if 'shareholder' in selected:
            result['holder_num'] = info.get_holder_num([code], is_local=False)
            result['share_holder'] = info.get_share_holder([code], is_local=False)
    return result
