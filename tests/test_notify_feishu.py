# -*- coding: utf-8 -*-
"""飞书推送：卡片 payload 结构与分片口径（全程 stub requests，不发网络）。"""

import json
import types

from src.notify import service as svc


class _Resp:
    def json(self):
        return {"code": 0, "msg": "ok"}


def _stub_send(monkeypatch, max_bytes=20000):
    """替掉全局配置与 requests.post，返回捕获到的 payload 列表。"""
    cfg = types.SimpleNamespace(feishu_webhook_url="https://fake/feishu",
                                feishu_max_bytes=max_bytes)
    monkeypatch.setattr(svc, "get_config", lambda: cfg)
    sent = []
    monkeypatch.setattr(
        svc.requests, "post",
        lambda url, json=None, timeout=None: (sent.append(json), _Resp())[1],
    )
    return sent


def test_send_feishu_keeps_multibyte_chars_whole_when_splitting(monkeypatch):
    sent = _stub_send(monkeypatch, max_bytes=700)
    text = "\n".join(["汉字" * 20] * 5)
    assert svc.NotificationService._send_feishu(object(), text) is True
    assert len(sent) > 1
    assert "\n".join(p["card"]["elements"][0]["text"]["content"] for p in sent) == text
    assert all(len(json.dumps(p).encode("utf-8")) <= 700 for p in sent)


def test_send_feishu_keeps_oversized_single_line(monkeypatch):
    sent = _stub_send(monkeypatch, max_bytes=100)
    text = "x" * 500
    assert svc.NotificationService._send_feishu(object(), text) is True
    assert len(sent) == 1
    assert sent[0]["card"]["elements"][0]["text"]["content"] == text


def test_send_feishu_uses_interactive_lark_md_card(monkeypatch):
    sent = _stub_send(monkeypatch)
    ok = svc.NotificationService._send_feishu(object(), "# ETF 周报\n\n**总资产**: 1 元")

    assert ok is True
    assert sent[0]["msg_type"] == "interactive"
    content = sent[0]["card"]["elements"][0]["text"]
    assert content["tag"] == "lark_md"
    assert "**ETF 周报**" in content["content"]   # 标题降级为加粗，卡片里真渲染
    assert "**总资产**: 1 元" in content["content"]


def test_send_feishu_splits_into_multiple_cards(monkeypatch):
    sent = _stub_send(monkeypatch, max_bytes=120)
    assert svc.NotificationService._send_feishu(object(), "\n".join(["abc"] * 60)) is True
    assert len(sent) > 1
    assert all(p["msg_type"] == "interactive" for p in sent)


def _rotation_report():
    rows = [
        ("159561(159561)", "海外权益", "+0.1953", "✅ 合格候选"),
        ("国债ETF国泰(511010)", "利率", "+0.0152", "✅ 合格候选"),
        ("红利ETF易方达(515180)", "国内权益", "+0.0035", "✅ 合格候选"),
        ("恒生ETF华夏(159920)", "海外权益", "+0.0018", "✅ 合格候选"),
        ("科创50ETF华夏(588000)", "国内权益", "+0.0000", "跳水过滤记 0"),
        ("30年国债ETF鹏扬(511090)", "利率", "-0.0000", "不合格"),
        ("标普500ETF博时(513500)", "海外权益", "-0.0665", "不合格"),
        ("豆粕ETF华夏(159985)", "商品", "-0.0890", "不合格"),
        ("沪深300ETF华泰柏瑞(510300)", "国内权益", "-0.1576", "不合格"),
        ("黄金ETF华安(518880)", "商品", "-0.2248", "不合格"),
        ("中证1000ETF南方(512100)", "国内权益", "-0.2257", "不合格"),
        ("纳指ETF国泰(513100)", "海外权益", "-0.3427", "不合格"),
        ("有色ETF大成(159980)", "商品", "-0.4104", "不合格"),
        ("日经ETF华夏(513520)", "海外权益", "-0.5727", "不合格"),
        ("创业板ETF易方达(159915)", "国内权益", "-0.5943", "不合格"),
    ]
    report = "\n".join([
        "## 今日截面", "", "| 标的 | 类别 | 评分 | 状态 |",
        "| --- | --- | ---: | --- |",
        *("| " + " | ".join(row) + " |" for row in rows),
        "", "## 投资账户建议", "", "继续持有第一名",
    ])
    return report, rows


def _table_values(tables):
    return [tuple(row[column["name"]] for column in table["columns"])
            for table in tables for row in table["rows"]]


def test_rotation_report_sends_native_table_between_text_sections(monkeypatch):
    sent = _stub_send(monkeypatch)
    report, rows = _rotation_report()

    assert svc.NotificationService._send_feishu(object(), report) is True

    assert len(sent) == 1
    before, table, after = sent[0]["card"]["elements"]
    assert "今日截面" in before["text"]["content"]
    assert table["tag"] == "table"
    assert [c["display_name"] for c in table["columns"]] == ["标的", "类别", "评分", "状态"]
    assert table["columns"][2]["horizontal_align"] == "right"
    assert _table_values([table]) == rows
    assert table["page_size"] == 10
    assert "投资账户建议" in after["text"]["content"]
    assert "继续持有第一名" in after["text"]["content"]


def test_long_table_splits_with_headers_and_no_missing_rows(monkeypatch):
    budget = 1800
    sent = _stub_send(monkeypatch, max_bytes=budget)
    report, rows = _rotation_report()

    assert svc.NotificationService._send_feishu(object(), report) is True

    assert len(sent) > 1
    tables = [e for p in sent for e in p["card"]["elements"] if e["tag"] == "table"]
    assert len(tables) > 1
    assert _table_values(tables) == rows
    assert all(t["columns"] == tables[0]["columns"] for t in tables)
    assert all(len(json.dumps(p).encode("utf-8")) <= budget for p in sent)


def test_many_tables_respect_card_limit(monkeypatch):
    sent = _stub_send(monkeypatch)
    report = "\n\n".join(f"| 标的 |\n| --- |\n| ETF{i} |" for i in range(7))

    assert svc.NotificationService._send_feishu(object(), report) is True

    tables_by_card = [[e for e in p["card"]["elements"] if e["tag"] == "table"] for p in sent]
    assert len(sent) == 2
    assert all(len(tables) <= 5 for tables in tables_by_card)
    assert _table_values([t for tables in tables_by_card for t in tables]) == [
        (f"ETF{i}",) for i in range(7)]


def test_table_preserves_empty_cells_escaped_pipes_and_formatting(monkeypatch):
    sent = _stub_send(monkeypatch)
    report = ("| 标的 | 说明 | 状态 |\n| --- | :---: | --- |\n"
              "| **ETF** | | ✅ |\n| A\\|B | 一<br>二 | |")

    assert svc.NotificationService._send_feishu(object(), report) is True

    table = sent[0]["card"]["elements"][0]
    assert table["tag"] == "table"
    assert _table_values([table]) == [("**ETF**", "", "✅"), ("A|B", "一 / 二", "")]
    assert table["columns"][1]["horizontal_align"] == "center"


def test_pipe_text_without_table_separator_is_not_lost(monkeypatch):
    sent = _stub_send(monkeypatch)
    report = "| 这只是普通文字 |\n\n```text\n| 示例 |\n| --- |\n| 原样保留 |\n```"

    assert svc.NotificationService._send_feishu(object(), report) is True

    elements = sent[0]["card"]["elements"]
    assert all(e["tag"] == "div" for e in elements)
    assert report in "\n".join(e["text"]["content"] for e in elements)
