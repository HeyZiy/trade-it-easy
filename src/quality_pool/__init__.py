# -*- coding: utf-8 -*-
"""
===================================
质量池 + 中期相对强弱轮动 — 模拟盘（影子台账）
===================================

规格唯一来源：strategy/roe_quality_pool.md（2026-10-05 定稿）。
研究口径对照：research/studies/roe_quality/pool_rotation_v3a.py（聚宽参考实现）。

分层（同 src/pullback_trend 先例：判定核纯函数、取数走 seam）：

- config.py      阈值常量（数字与规格一一对应）
- screener.py    质量池筛选 + 强弱分数排名 + 留存/补足判定（纯函数）
- execution.py   09:31 执行计划：退出重试、差额再平衡、买入（纯函数）
- feeds.py       数据装配 seam（AmazingData 批量实现，测试注入假数据）
- state.py       模拟盘状态持久化（待执行计划、退出队列、下一调仓日）
- report.py      报告渲染

持仓/资金事实来源是名义成交台账（src/trade_ledger，account=quality_pool），
本包不做任何真实下单。
"""
