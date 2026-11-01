# 公告分析准备

提供公告筛选、查询、原文下载和 PDF 文本提取。各步骤单独调用，保存路径及是否联网由调用方决定。

```python
from research.tools.announcement_analysis import (
    filter_announcements, search_announcements, download_pdfs, PdfConverter,
)

# 已有公告表时，筛选不联网，也不修改原表。
selected = filter_announcements(frame, 20260101, 20261030, tag_ids=['10301'])

selected = search_announcements(
    ['600001.SH'], 20260101, 20261030,
    asset_type='stock', tag_ids=['10301'],
)
pdfs = download_pdfs(selected, 20260101, 20261030, asset_type='stock')
markdown = PdfConverter(output_dir='local/announcements/md').convert_batch(pdfs)
selected.to_pickle('local/announcements/results.pkl')  # 需要保存时显式执行
```

- `filter_announcements`：日期包含起止两端；TAG 以 `|` 分隔，多个字面前缀取并集。缺少筛选字段时报错，空输入返回空表。
- `search_announcements`：通过共享数据模块查询后筛选；`stock/fund/bond` 对应股票、基金、可转债。代码列表显式传入；全市场列表可从 `data_provider.fetchers.amazingdata.basics` 获取。
- `download_pdfs`：返回 `{SOURCE_ID: 本地PDF路径}`，仅包含实际存在的文件；请求失败抛出异常。PDF 存储沿用共享数据模块的目录约定。
- `PdfConverter`：按 pdfplumber、PyMuPDF、pdfminer.six 顺序尝试提取，返回成功的 Markdown 文件映射；用 `get_failed_files()` 查看转换失败。PDF 引擎在构造转换器时检测，导入包不加载这些依赖。

搜索不会自动保存、下载或转换文件。`is_local=True` 会使用供应商本地缓存；时间和 TAG 筛选仍会执行。TAG 目录见 [公告分类](references/announcement_categories.md)，数据配置见 [共享数据模块](../../../data_provider/fetchers/amazingdata/README.md)，另见 [上游声明](../references/upstream_notice.md)。

此模块只准备研究材料，不自动解读公告，也不产出策略信号。
