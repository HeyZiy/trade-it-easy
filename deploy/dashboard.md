# 看板部署与排障

- 服务器连接参数见本地 `deploy/server_ssh.md`，迁移范围见[运行数据清单](data_manifest.md)。
- 账户操作与配比口径见看板内的「盈亏怎么算」和「配比说明」，策略规则见 [ALLOCATION.md](../ALLOCATION.md)，定时任务见 [crontab.server](crontab.server)。

## 服务配置

生产服务 `trade-dashboard` 使用 `/root/miniconda/envs/rt/bin/python` 运行仓库内的 `/root/trade-it-easy/dashboard.py`，后端监听 `127.0.0.1:8765`。新装 [systemd 模板](trade-dashboard.service) 时，确认 `ExecStart` 指向仓库内的脚本。静态文件与后台代码使用同一份仓库。

- 在 `/etc/trade-dashboard.env` 中设置 `TRADE_DASHBOARD_USER` 和 `TRADE_DASHBOARD_PASSWORD`，文件权限为 `600`。改口令后重启服务。
- 在 `/etc/systemd/system/trade-dashboard.service.d/runtime-home.conf` 的 `[Service]` 下设置 `Environment=HOME=/root`，供 AmazingData/TGW 原生组件使用。改服务配置后执行 `systemctl daemon-reload` 并重启。

## 发布与验证

1. 在本地和服务器的项目环境验证 `tests/test_dashboard.py`、`tests/test_dashboard_quotes.py` 和 `tests/test_etf_account.py`。
2. 更新服务器代码后立即执行 `systemctl restart trade-dashboard`。静态资源随文件更新生效，后端代码在服务重启后载入。
3. 执行 `systemctl is-active trade-dashboard`，确认服务运行。用本机地址请求 `/api/overview`，未登录应返回 `401`。从访问入口登录后，检查账户总览、估值刷新和策略报告。

## 访问与排障

- 对外入口由 [trade-dashboard-tunnel.service](trade-dashboard-tunnel.service) 提供。Cloudflare Quick Tunnel 重启后地址可能变化，在仓库根目录运行 `sh deploy/tunnel-url.sh` 查看当前地址。
- 页面打不开时查 `journalctl -u trade-dashboard -n 50`。服务正常而外部地址不可用时查 `journalctl -u trade-dashboard-tunnel -n 50`。
- 报告日期或收盘成绩停滞时，按 [crontab.server](crontab.server) 找到对应任务，再查看仓库 `logs/` 下的任务日志和 `cron_error.log`。
