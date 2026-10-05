# -*- coding: utf-8 -*-
"""JQData SDK 连通与额度探针（尸检前置）。

凭证放仓库根 .env（已在 .gitignore）：
    JQ_USERNAME=手机号/邮箱
    JQ_PASSWORD=密码
运行：
    .conda/python.exe research/studies/trend/tools/jq_probe.py
"""
import os
import sys

from dotenv import load_dotenv
from jqdatasdk import (auth, is_auth, get_query_count, get_account_info,
                       get_price, get_all_securities, query, valuation,
                       get_fundamentals)

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '.env'))

user = os.environ.get('JQ_USERNAME')
pwd = os.environ.get('JQ_PASSWORD')
if not user or not pwd:
    sys.exit('缺少 .env 项 JQ_USERNAME / JQ_PASSWORD')

auth(user, pwd)  # 失败会抛异常，成功打印 auth success
print('is_auth:', is_auth())

print('额度(剩余记录数):', get_query_count())
print('账户:', get_account_info())

# get_price 是 SDK 取数入口（无 history），支持字段列表+多标的
try:
    bars = get_price(['600519.XSHG'], count=5, end_date='2026-09-25',
                     frequency='daily',
                     fields=['open', 'high', 'low', 'close', 'volume'])
    print('get_price 5 行: 形状=%s 列=%s' % (bars.shape, list(bars.columns)))
except Exception as e:
    print('get_price 异常:', type(e).__name__, str(e)[:200])

try:
    df = get_fundamentals(
        query(valuation.code, valuation.turnover_ratio)
        .filter(valuation.turnover_ratio <= 12),
        date='2026-09-25')
    print('估值表单查行数:', 0 if df is None else len(df))
except Exception as e:
    print('估值表异常:', type(e).__name__, str(e)[:200])

sec = get_all_securities('stock', date='2026-09-25')
print('股票列表行数:', len(sec))
