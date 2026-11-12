# -*- coding: utf-8 -*-
"""etf_pool 建池模块使用示例。

两种用法:
  1. 研究快照:任意时点建池、落盘盘点(可在聚宽研究环境直接运行);
  2. 策略集成:天级回测中定期重建,是 reversal_v1.build_pool 的替代写法。

整个文件可复制至聚宽策略/研究环境;真实运行需要 finance 数据权限。
本地无法运行(依赖 jqdata 平台接口),行为验证见 tests/test_etf_pool.py。
"""
from jqdata import *
import pandas as pd

# 若在聚宽编辑器:把 etf_pool.py 全文粘贴在本文件上方(或同文件顶部);
# 若在本地研究环境且模块可导入:
#   from research.studies.etf.etf_pool import classify_etfs, build_pool
import etf_pool  # 示例假定模块已就位

ASOF = '2026-11-06'


def example_research_snapshot():
    """研究用法一:三行建池,拿代表ETF清单。"""
    pool = etf_pool.build_pool(ASOF)
    # → DataFrame: code / display_name / pool_key / index_key /
    #              mapping_source / avg_money,按均额降序
    print(pool.head(10))
    pool.to_csv('etf_universe_snapshot.csv', encoding='utf-8-sig')


def example_policy_variants():
    """研究用法二:同一流程下比较未映射产品的三种政策。"""
    # 默认:未映射全剔除(含黄金)——最干净,每个名额暴露都明确。
    strict = etf_pool.build_pool(ASOF)

    # 显式名单:人工确认黄金ETF同质后用代码归并,替代名称正则。
    with_gold = etf_pool.build_pool(
        ASOF,
        manual_groups={'MANUAL:GOLD': ['518880', '159934',
                                       '518800.XSHG']})

    # 盘点:每只未映射产品自成一组,一个不漏;只用于人工核查,不用于交易。
    inventory = etf_pool.build_pool(ASOF, keep_unmapped=True,
                                    liquidity_min=0, listed_days=0)

    print('严格=%d 含黄金=%d 盘点=%d' % (len(strict), len(with_gold),
                                        len(inventory)))


def example_full_inventory():
    """研究用法三:全量分类快照(不做任何剔除),供参数敏感性分析。"""
    etfs = etf_pool.classify_etfs(ASOF)
    print(etfs['mapping_source'].value_counts())
    print(etfs[etfs['index_key'].isna()][['display_name']])  # 人工审阅未映射名单


def example_strategy_integration(context):
    """策略用法:天级回测中每5个交易日重建一次,失败则只管持仓次日重试。"""
    g = etf_pool  # 实际策略里用全局 g;示例简化
    if not hasattr(g, '_pool_date'):
        g._pool_date, g._pool_rows = None, []

    today = str(context.previous_date)
    if g._pool_date is None:
        try:
            g._pool_rows = etf_pool.build_pool(today)
            g._pool_date = today
        except Exception as exc:
            log.info('建池失败 %s:%r,当日只管理持仓' % (today, exc))
    return g._pool_rows
