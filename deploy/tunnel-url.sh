#!/bin/sh
# 打印 quick tunnel 当前地址；进程重启后地址会变，以 journal 最后一条为准。
journalctl -u trade-dashboard-tunnel --no-pager -o cat |
    grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1