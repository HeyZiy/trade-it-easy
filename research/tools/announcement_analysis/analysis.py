"""公告筛选、查询和原文下载；结果保存由调用方负责。"""
import os
import re

import pandas as pd


def filter_announcements(frame, begin_date, end_date, *, tag_ids=None):
    """按包含端点的日期范围和 TAG 前缀筛选，返回副本。

    多个前缀取并集，TAG 以 | 分隔；缺少筛选所需字段时抛出 ValueError。
    """
    if begin_date > end_date:
        raise ValueError('begin_date 不能晚于 end_date')
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy().reset_index(drop=True)
    result = frame.copy()
    if tag_ids:
        if 'TAG_ID' not in result:
            raise ValueError('公告缺少 TAG_ID，无法按标签筛选')
        pattern = r'(?:^|\|)(?:' + '|'.join(re.escape(str(tag)) for tag in tag_ids) + ')'
        result = result[result['TAG_ID'].astype('string').str.contains(pattern, na=False)]
    if 'PUBLISH_TIME' not in result:
        raise ValueError('公告缺少 PUBLISH_TIME，无法按日期筛选')
    dates = pd.to_numeric(result['PUBLISH_TIME'].astype('string').str.replace('-', '', regex=False).str[:8], errors='coerce')
    return result[dates.between(begin_date, end_date)].reset_index(drop=True)


def search_announcements(codes, begin_date, end_date, *, asset_type='stock',
                         tag_ids=None, is_local=False, local_path=None, data_client=None):
    """查询并筛选公告；不保存结果，不自动下载 PDF 或转换 Markdown。"""
    from data_provider.fetchers.amazingdata import announcements
    from data_provider.fetchers.amazingdata._utils import _resolve_cache_dir

    if begin_date > end_date:
        raise ValueError('begin_date 不能晚于 end_date')
    frame = announcements.get_announcement_list(
        asset_type, list(codes), begin_date, end_date,
        local_path=local_path or _resolve_cache_dir(),
        is_local=is_local, data_client=data_client,
    )
    return filter_announcements(frame, begin_date, end_date, tag_ids=tag_ids)


def download_pdfs(frame, begin_date, end_date, *, asset_type='stock', local_path=None, data_client=None):
    """下载选中的公告，返回 {SOURCE_ID: 已存在的本地文件路径}；请求失败抛出异常。"""
    from data_provider.fetchers.amazingdata import announcements
    from data_provider.fetchers.amazingdata._utils import _resolve_cache_dir

    if frame.empty:
        return {}
    paths, _ = announcements.download_announcements(
        asset_type, frame, begin_date, end_date,
        local_path=local_path or _resolve_cache_dir(), data_client=data_client,
    )
    return {source_id: path for source_id, path in (paths or {}).items() if os.path.isfile(path)}
