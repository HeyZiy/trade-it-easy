# 只读交易工作台

`dashboard.py` 只监听服务器本机 `127.0.0.1:8765`，显示四个独立名义账户的流水推导持仓、最近有效收盘成绩、策略状态和最新报告，不查询实时价格，也不提供写入功能。

页面分「汇总 + 四个策略」：四本账分别展示收益、基准、超额、仓位和成绩日期。核心配置使用 `trade_ledger.jsonl`，行业动量使用 `trade_ledger_industry_momentum.jsonl` 并标明模拟；不合并收益和持仓。行业任务、手工记账的 satellite 默认路径及收盘采集均使用独立台账。

收益走势对比账户累计收益与同期基准，支持拖动查看日期和切换时间范围。曲线只读取与流水匹配的收盘记录，缺失或失效记录断开显示；时间范围只筛选日期，不重置累计收益起点。汇总页显示核心配置的曲线，各策略页显示本账曲线。

最新报告在页面内切换和阅读，标题、表格、列表统一排版，长报告支持展开和收起。报告按当前账户默认选中，也可单独切换；正文经格式白名单过滤，仅保留阅读所需的 HTML 标签。

展示口径：不出现名义金额，全部按比例——收益/基准/超额来自最近有效收盘记录，仓位/现金/应收红利取自该记录的 `invested`、`cash`、`receivables` 与 `equity`；持仓表给最近收盘价与占比（本策略内），不给名义股数和市值。

收盘价来自 `collect_close` 写进每条成绩记录的 `positions` 快照（`code/close/market_value/pos_pct`），页面只读该快照，不额外拉行情：快照齐全时按收盘市值算占比，否则整列回退成本占比并标注“N 只尚无收盘价”。当日新建仓位在下一次收盘采集前没有快照，收盘价显示 `—`；旧记录没有 `positions` 字段时整体回退成本口径。

所有路由（页面、静态资源、`/api/overview`、`/api/report/*`、`/report/*`）统一走 Basic Auth，口令只从环境变量 `TRADE_DASHBOARD_PASSWORD` 读取；未配置口令时一律返回 503，不提供未鉴权内容。用户名为 `TRADE_DASHBOARD_USER`，默认 `trader`。鉴权失败会延迟 0.5 秒再返回 401，降低爆破速率。

## 部署

- 代码在 `/opt/trade-dashboard`（独立拷贝，不从仓库同步），由 `trade-dashboard.service` 启动，口令与用户名在 `/etc/trade-dashboard.env`（600，root）。
- 更新流程：修改 `dashboard.py` 与 `dashboard/` → 跑 `tests/test_dashboard.py` 并验证页面交互 → 同步整个工作台到 `/opt/trade-dashboard` → `systemctl restart trade-dashboard` → 本机与隧道各验证访问保护、曲线数据和报告内容。
- 成绩仓库由 `PYTHONPATH=/root/trade-it-easy` 导入，改 `src/trade_ledger/performance.py`（例如收盘快照字段）要同步上传到服务器仓库并重跑当日采集，否则页面拿不到新字段。
- 轮换口令：改 `/etc/trade-dashboard.env` 后重启服务，无其他步骤。

## 远程访问

`trade-dashboard-tunnel.service` 运行 `cloudflared tunnel --url http://127.0.0.1:8765`，即 Cloudflare Quick Tunnel：免账号、免域名、不开任何入站端口，CF 边缘终止 TLS。

- 地址每次隧道重启都会更换，用 `/usr/local/bin/trade-tunnel-url` 查当前地址。
- Quick Tunnel 是官方测试用途通道，没有 SLA，断开后自动重启会换新地址；没有固定域名和书签。
- 鉴权只有一层口令，没有第二因子；口令泄露等于持仓泄露。不要把地址和口令放进任何公开渠道。

## 备份

自动备份暂不启用。
