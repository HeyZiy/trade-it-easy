# -*- coding: utf-8 -*-
"""
===================================
格式化工具模块
===================================

提供各种内容格式化工具函数，用于将通用格式转换为平台特定格式。
"""

import re
from typing import List

import markdown2

TRUNCATION_SUFFIX = "\n\n...(本段内容过长已截断)"
PAGE_MARKER_PREFIX = f"\n\n📄"
PAGE_MARKER_SAFE_BYTES = 16 # "\n\n📄 9999/9999"
PAGE_MARKER_SAFE_LEN = 13   # "\n\n📄 9999/9999"
MIN_MAX_WORDS = 10
MIN_MAX_BYTES = 40

# Unicode code point ranges for special characters.
_SPECIAL_CHAR_RANGE = (0x10000, 0xFFFFF)
_SPECIAL_CHAR_REGEX = re.compile(r'[\U00010000-\U000FFFFF]')

# <br> 换行标签（飞书纯文本不渲染 HTML，统一转 " / "）
_BR_TAG_RE = re.compile(r"\s*<br\s*/?>\s*")


def markdown_to_html_document(markdown_text: str) -> str:
    """
    Convert Markdown to a complete HTML document (for email).

    Uses markdown2 with table and code block support, wraps with inline CSS
    for compact, readable layout. Used by notification email.

    Args:
        markdown_text: Raw Markdown content.

    Returns:
        Full HTML document string with DOCTYPE, head, and body.
    """
    html_content = markdown2.markdown(
        markdown_text,
        extras=["tables", "fenced-code-blocks", "break-on-newline", "cuddled-lists"],
    )

    css_style = """
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
                line-height: 1.5;
                color: #24292e;
                font-size: 14px;
                padding: 15px;
                max-width: 900px;
                margin: 0 auto;
            }
            h1 {
                font-size: 20px;
                border-bottom: 1px solid #eaecef;
                padding-bottom: 0.3em;
                margin-top: 1.2em;
                margin-bottom: 0.8em;
                color: #0366d6;
            }
            h2 {
                font-size: 18px;
                border-bottom: 1px solid #eaecef;
                padding-bottom: 0.3em;
                margin-top: 1.0em;
                margin-bottom: 0.6em;
            }
            h3 {
                font-size: 16px;
                margin-top: 0.8em;
                margin-bottom: 0.4em;
            }
            p {
                margin-top: 0;
                margin-bottom: 8px;
            }
            table {
                border-collapse: collapse;
                width: 100%;
                margin: 12px 0;
                display: block;
                overflow-x: auto;
                font-size: 13px;
            }
            th, td {
                border: 1px solid #dfe2e5;
                padding: 6px 10px;
                text-align: left;
            }
            th {
                background-color: #f6f8fa;
                font-weight: 600;
            }
            tr:nth-child(2n) {
                background-color: #f8f8f8;
            }
            tr:hover {
                background-color: #f1f8ff;
            }
            blockquote {
                color: #6a737d;
                border-left: 0.25em solid #dfe2e5;
                padding: 0 1em;
                margin: 0 0 10px 0;
            }
            code {
                padding: 0.2em 0.4em;
                margin: 0;
                font-size: 85%;
                background-color: rgba(27,31,35,0.05);
                border-radius: 3px;
                font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
            }
            pre {
                padding: 12px;
                overflow: auto;
                line-height: 1.45;
                background-color: #f6f8fa;
                border-radius: 3px;
                margin-bottom: 10px;
            }
            hr {
                height: 0.25em;
                padding: 0;
                margin: 16px 0;
                background-color: #e1e4e8;
                border: 0;
            }
            ul, ol {
                padding-left: 20px;
                margin-bottom: 10px;
            }
            li {
                margin: 2px 0;
            }
        """

    return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                {css_style}
            </style>
        </head>
        <body>
            {html_content}
        </body>
        </html>
        """


def format_feishu_markdown(content: str) -> str:
    """适配 lark_md 文本；表格由 format_feishu_elements 单独提取。"""
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        # 转换标题（# ## ### 等）
        if re.match(r'^#{1,6}\s+', line):
            title = re.sub(r'^#{1,6}\s+', '', line).strip()
            line = f"**{title}**" if title else ""
        # 转换引用块
        elif line.startswith('> '):
            quote = line[2:].strip()
            line = f"💬 {quote}" if quote else ""
        # 转换分隔线
        elif line.strip() == '---':
            line = '────────'
        # 转换列表项
        elif line.startswith('- '):
            line = f"• {line[2:].strip()}"

        # 残留的 <br> 标签转 ' / '（飞书纯文本不渲染 HTML）
        if "<br" in line:
            line = _BR_TAG_RE.sub(" / ", line)

        lines.append(line)

    return "\n".join(lines).strip()


def _feishu_table_cells(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [_BR_TAG_RE.sub(" / ", cell).strip().replace("\\|", "|")
            for cell in re.split(r"(?<!\\)\|", line)]


def _feishu_table_columns(header_line: str, separator_line: str) -> List[dict]:
    header = _feishu_table_cells(header_line)
    separators = _feishu_table_cells(separator_line)
    if not 1 <= len(header) <= 50 or len(header) != len(separators):
        return []

    columns = []
    for index, (name, separator) in enumerate(zip(header, separators)):
        if not re.fullmatch(r":?-{3,}:?", separator):
            return []
        if not separator.endswith(":"):
            alignment = "left"
        elif separator.startswith(":"):
            alignment = "center"
        else:
            alignment = "right"
        columns.append({
            "name": f"col_{index}",
            "display_name": name,
            "data_type": "lark_md",
            "width": "auto",
            "horizontal_align": alignment,
        })
    return columns


def format_feishu_elements(content: str) -> List[dict]:
    """表格使用根级 table 组件；其余内容保留为 lark_md 文本块。"""
    elements = []
    text_lines = []
    lines = content.splitlines()
    fence = None

    def flush_text() -> None:
        text = format_feishu_markdown("\n".join(text_lines))
        if text:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
        text_lines.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None

        columns = []
        if fence is None and "|" in line and index + 1 < len(lines):
            columns = _feishu_table_columns(line, lines[index + 1])
        if not columns:
            text_lines.append(line)
            index += 1
            continue

        flush_text()
        column_names = [column["name"] for column in columns]
        rows = []
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = _feishu_table_cells(lines[index])
            if len(cells) > len(columns):
                break
            cells.extend([""] * (len(columns) - len(cells)))
            rows.append(dict(zip(column_names, cells)))
            index += 1
        elements.append({
            "tag": "table",
            "page_size": 10,
            "row_height": "low",
            "freeze_first_column": True,
            "columns": columns,
            "rows": rows,
        })

    flush_text()
    return elements
