# -*- coding: utf-8 -*-
"""飞书推送：卡片 payload 结构与分片口径（全程 stub requests，不发网络）。"""

import types

from src.notify import service as svc


class _Resp:
    def json(self):
        return {"code": 0, "msg": "ok"}


def _stub_send(monkeypatch, max_bytes=200):
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


def test_chunk_by_line_keeps_multibyte_chars_whole():
    line = "汉字" * 20          # 单行 120 字节
    text = "\n".join([line] * 5)
    chunks = svc._chunk_by_line(text, 250)
    assert "\n".join(chunks) == text          # 内容零丢失（旧法字节硬切会吃掉半截汉字）
    assert len(chunks) > 1


def test_chunk_by_line_passes_oversized_single_line():
    long_line = "x" * 500
    assert svc._chunk_by_line(long_line, 100) == [long_line]


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
