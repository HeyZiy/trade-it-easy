# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python 标准库单文件服务（`dashboard.py`，ThreadingHTTPServer + Basic 认证）+ 原生 JS/CSS 前端（`dashboard/`）。

## Users

唯一日常用户是仓库所有者本人：目前以桌面为主，但是完善后应该以手机为主。每天收盘后在看板上完成「看策略建议 → 人工确认成交 → 记台账」的闭环。无多用户、无角色区分。

## Product Purpose

个人 A 股交易策略系统的操作看板。把不同策略与待定组合的持仓、建议、流水集中呈现，供用户核对并执行。
成功 = 建议核对成本低、记账准确、与策略研究页口径一致；所有买卖指令仅是建议，确认成交后才记账。

## Positioning

不是通用记账软件，也不是行情终端：它直接绑定本仓策略引擎产出的建议与台账重放（`src/account_replay.py` 事件重放推导余额/持仓/可卖份额），建议与账本同源、口径可自查（盈亏对账、配比说明）。外部工具无法复制「建议引擎 + 台账重放 + 人工确认闸门」这一闭环。

## Operating Context

- 生产：阿里云 ECS `trade-dashboard.service`，对外经 Cloudflare Quick Tunnel + Basic 密码；口令在 `/etc/trade-dashboard.env`。
- 本地开发：`./.conda/python.exe dashboard.py`（依赖 `data/etf_account.json` 等本地数据，gitignored）。开发探针严禁在浏览器里提交记账表单——提交路径只在 pytest 验证，误提交需按 trade-it-easy-dev skill 回滚。
- 日常节奏：cron 任务收盘后跑策略 → 看板展示当日建议 → 用户人工确认；数据异常时整轮不调仓（「数据暂停」），持仓不动、日程照常。
- 相关文档：策略规则 `docs/ALLOCATION.md`，部署 `deploy/dashboard.md`。

## Capabilities and Constraints

- 页面：交易工作台（账户总览/持仓/待确认申购与赎回/流水/建议/研究报告）、ETF 择时研究页（`/research/timing`）、账户设置（目标占比、交易账户、费用、长期配置配比、标的池、历史配置）。
- 记账事件类型见 `src/account_replay.py`（`make_event` 白名单 + `_Replay.apply`），新增类型必须走全触点（见 skill trade-it-easy-dev 的 ledger-event-types 参考）。
- 四个组合：长期配置（allocation）、跨资产动量（signal）、质量动量（signal）、待定组合（holding，承接正式策略外全部持仓与现金）。
- 未决：不做多账户共享、不接实盘自动下单（已确认，2026-11）。

## Brand Commitments

- 名称「trade-it-easy / TRADE IT EASY」与页头 eyebrow 用法已在用；无其他视觉承诺记录（视觉世界归 DESIGN.md，init 不写）。

## Evidence on Hand

- 策略与账户口径文档：`docs/ALLOCATION.md`、`docs/` 其余笔记、`research/`（回测研究均为用户在聚宽平台执行的结果记录）。
- 无外部用户数据、无 testimonial、无 benchmark 宣传素材——未来工作不得虚构此类内容。

## Product Principles

1. 建议只是建议：一切执行以人工确认为闸门，看板不替用户做决定。
2. 账本事实第一：展示与对账必须能从台账重放推导，口径与策略文档一致。
3. 决策质量优先于完备：界面为快速核对与准确记账服务，不做信息装饰。

## Accessibility & Inclusion

单用户自用工具：遵循项目既有做法（语义化标签、role/aria、键盘可达、44px 触达目标已在用），无外部标准强制要求。
