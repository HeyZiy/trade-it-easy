from jqdata import *
import pandas as pd

# 测试日期
TEST_DATE = '2026-11-06'

# 1. 获取场内基金，筛选ETF
funds = get_all_securities(['fund'], date=TEST_DATE)
print('基金类型分布：')
print(funds['type'].value_counts())

etfs = funds[
    funds['type'].astype(str).str.lower() == 'etf'
].copy()
etf_codes = etfs.index.tolist()

print('\nETF总数：', len(etf_codes))

# 2. 分批查询跟踪指数
records = []
batch_size = 100

for i in range(0, len(etf_codes), batch_size):
    batch = etf_codes[i:i + batch_size]

    q = query(
        finance.FUND_INVEST_TARGET
    ).filter(
        finance.FUND_INVEST_TARGET.code.in_(batch),
        finance.FUND_INVEST_TARGET.pub_date <= TEST_DATE
    )

    result = finance.run_query(q)
    if not result.empty:
        records.append(result)

if records:
    mapping = pd.concat(records, ignore_index=True)

    # 只使用测试日期已经生效的映射
    start = pd.to_datetime(mapping['start_date'])
    end = pd.to_datetime(mapping['end_date'])
    date = pd.Timestamp(TEST_DATE)

    mapping = mapping[
        (start <= date) &
        (end.isna() | (end > date))
    ].copy()

    # 如有重复记录，保留最新公告
    mapping = mapping.sort_values('pub_date')
    mapping = mapping.drop_duplicates('code', keep='last')

    etfs = etfs.join(
        mapping.set_index('code')[
            ['traced_index_name', 'traced_index_code']
        ]
    )
else:
    etfs['traced_index_name'] = None
    etfs['traced_index_code'] = None

# 3. 覆盖率统计
total = len(etfs)
has_name = etfs['traced_index_name'].notna().sum()
has_code = etfs['traced_index_code'].notna().sum()
unique_index = etfs['traced_index_code'].nunique()

print('\n====== ETF跟踪指数覆盖率 ======')
print('ETF总数:', total)
print('有跟踪指数名称:', has_name)
print('有跟踪指数代码:', has_code)
print('代码覆盖率: {:.2%}'.format(
    has_code / total if total else 0
))
print('不同跟踪指数数量:', unique_index)
print('无跟踪指数代码:', total - has_code)

# 4. 查看同指数重复情况
valid = etfs.dropna(subset=['traced_index_code'])
counts = valid.groupby('traced_index_code').size()

print('\n====== 重复ETF统计 ======')
print('存在多个ETF的指数数量:', (counts > 1).sum())
print('同指数重复ETF数量:', (counts - 1).clip(lower=0).sum())

print('\n====== 重复最多的指数 ======')
print(counts.sort_values(ascending=False).head(15))

print('\n====== 无法映射的ETF样本 ======')
print(etfs[etfs['traced_index_code'].isna()][
    ['display_name', 'traced_index_name']
].head(30))

# 5. 输出供后续分析
etfs.to_csv('etf_index_mapping.csv', encoding='utf-8-sig')



import pandas as pd
import re

# =====================================
# 1. 重建指数标识
# =====================================

def normalize_code(x):
    if pd.isna(x):
        return None
    s = str(x).strip().upper()
    s = re.sub(r'\.[A-Z]+$', '', s)
    return s or None

def extract_code(name):
    if pd.isna(name):
        return None

    matches = re.findall(
        r'[\(（]([^()（）]+)[\)）]',
        str(name)
    )

    for token in reversed(matches):
        token = re.sub(r'\s+', '', token).upper()
        if re.fullmatch(
            r'\d{4,8}|[A-Z][A-Z0-9._-]{3,}',
            token
        ):
            return normalize_code(token)

    return None

def normalize_name(x):
    if pd.isna(x):
        return None
    return re.sub(r'\s+', '', str(x)).upper()

# 原始代码
etfs['index_code_norm'] = (
    etfs['traced_index_code'].apply(normalize_code)
)

# 从名称提取代码
etfs['index_code_parsed'] = (
    etfs['traced_index_name'].apply(extract_code)
)

# 优先使用官方代码，其次使用名称提取代码
etfs['index_key'] = (
    etfs['index_code_norm'].combine_first(
        etfs['index_code_parsed']
    )
)

etfs['mapping_source'] = 'unmapped'

etfs.loc[
    etfs['index_code_norm'].notna(),
    'mapping_source'
] = 'official_code'

etfs.loc[
    etfs['index_code_norm'].isna() &
    etfs['index_code_parsed'].notna(),
    'mapping_source'
] = 'parsed_code'

# 剩余有名称的，用标准化名称作为临时标识
name_key = etfs['traced_index_name'].apply(normalize_name)

mask = etfs['index_key'].isna() & name_key.notna()

etfs.loc[mask, 'index_key'] = 'NAME:' + name_key[mask]

etfs.loc[mask, 'mapping_source'] = 'name_only'

# 注意：没有名称的ETF保持NaN，不再错误合并



# ==========================================
# ETF动态标的池：单日快照测试
# ==========================================

import pandas as pd
import numpy as np

TEST_DATE = '2026-11-06'

# 1. 处理特殊产品
etfs['pool_key'] = etfs['index_key']

# 黄金：暂时作为独立的宽泛暴露组
gold_mask = (
    etfs['index_key'].isna() &
    etfs['display_name'].str.contains(
        '黄金|金ETF|上海金', na=False
    )
)
etfs.loc[gold_mask, 'pool_key'] = 'MANUAL:GOLD'

# 货币基金：不参与Alpha排名
cash_mask = (
    etfs['index_key'].isna() &
    etfs['display_name'].str.contains(
        '货币|快线|快钱', na=False
    )
)

# 其余未映射产品暂时排除
pool = etfs[
    etfs['pool_key'].notna() & ~cash_mask
].copy()

print('初始可归类产品:', len(pool))
print('独立标识:', pool['pool_key'].nunique())

# 2. 上市时间过滤：至少365个自然日
date = pd.Timestamp(TEST_DATE)

pool['start_date'] = pd.to_datetime(pool['start_date'])

pool = pool[
    pool['start_date'] <= date - pd.Timedelta(days=365)
].copy()

print('上市时间过滤后:', len(pool))

# 3. 批量获取过去20日成交额
codes = pool.index.tolist()
money_records = []

for i in range(0, len(codes), 100):
    batch = codes[i:i+100]

    df = get_price(
        batch,
        end_date=TEST_DATE,
        count=20,
        frequency='daily',
        fields=['money'],
        skip_paused=True,
        panel=False
    )

    if df is not None and not df.empty:
        money_records.append(df)

if money_records:
    money_df = pd.concat(money_records, ignore_index=True)

    avg_money = money_df.groupby('code')['money'].mean()
    pool['avg_money'] = avg_money
else:
    pool['avg_money'] = np.nan

pool['avg_money'] = pool['avg_money'].fillna(0)

# 4. 不同流动性门槛下的结果
print('\n====== 流动性门槛敏感性 ======')

for threshold in [10000000, 30000000, 50000000]:

    eligible = pool[
        pool['avg_money'] >= threshold
    ].copy()

    # 同一指数只保留成交额最大的产品
    eligible = eligible.sort_values(
        'avg_money', ascending=False
    )

    selected = eligible.drop_duplicates(
        subset=['pool_key'], keep='first'
    )

    print('\n平均成交额门槛:',
          threshold / 10000, '万元')

    print('符合条件的ETF:', len(eligible))
    print('独立标的:', selected['pool_key'].nunique())

    print('前10只ETF:')
    print(selected[
        ['display_name', 'pool_key', 'avg_money']
    ].head(10).to_string())

# 5. 保存3000万元版本
eligible = pool[
    pool['avg_money'] >= 30000000
].copy()

eligible = eligible.sort_values(
    'avg_money', ascending=False
)

selected = eligible.drop_duplicates(
    subset=['pool_key'], keep='first'
)

selected.to_csv(
    'etf_universe_snapshot.csv',
    encoding='utf-8-sig'
)

print('\n最终3000万元版本数量:', len(selected))




