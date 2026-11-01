"""股票、基金和可转债的公告列表与原文下载；筛选和报告由研究工具负责。"""
from data_provider.types import DataFetchError
from .client import client

_METHODS = {
    "stock": ("get_announcement_stock_list", "get_announcement_stock"),
    "fund": ("get_announcement_fund_list", "get_announcement_fund"),
    "bond": ("get_announcement_bond_list", "get_announcement_bond"),
}


def _method(asset_type, download=False, data_client=None):
    if asset_type not in _METHODS:
        raise ValueError(f"不支持的公告类型: {asset_type}")
    source = client if data_client is None else data_client
    return getattr(source.get_info_data(), _METHODS[asset_type][int(download)])


def get_announcement_list(asset_type, code_list, begin_date, end_date,
                          *, local_path, is_local=False, data_client=None):
    method = _method(asset_type, data_client=data_client)
    kwargs = dict(code_list=code_list, is_local=is_local, local_path=local_path)
    if not is_local:
        kwargs.update(begin_date=begin_date, end_date=end_date)
    try:
        return method(**kwargs)
    except Exception as exc:
        raise DataFetchError(f"AmazingData 公告列表查询失败: {exc}") from exc


def download_announcements(asset_type, announcement_df, begin_date, end_date,
                           *, local_path, data_client=None):
    try:
        return _method(asset_type, download=True, data_client=data_client)(
            announcement_df, tag_id_list=None, begin_date=begin_date,
            end_date=end_date, local_path=local_path)
    except Exception as exc:
        raise DataFetchError(f"AmazingData 公告原文下载失败: {exc}") from exc
