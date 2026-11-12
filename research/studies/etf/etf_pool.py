# -*- coding: utf-8 -*-
"""全市场指数ETF动态标的池——可复用建池模块。

把 etfs.py 的探索流程固化为可参数化、可测试的形态;reversal_v1.build_pool
是同一流程在天级策略里的内嵌版,新策略从这里复用。

流程与口径(与 reversal_v1.md 的股票池一节一致):
  1. get_all_securities(['fund'], date=asof) 选 type=etf;
  2. 分批查询 finance.FUND_INVEST_TARGET,公告日<=asof 下推到服务端;
  3. 只保留 asof 当日已生效的记录(start<=asof<end 或 end 为空),按公告日取
     每只最新一条——最新公告尚未生效或已终止时,不得顶掉仍生效的旧映射;
  4. 指数标识归一(见 plain_code / index_identity):官方六位代码去已知交易所
     后缀;非六位官方代码单列;名称括号提取六位代码,提取到多个不同代码视为
     歧义,退回名称组;仅名称的按标准化名称成组;两者皆无记 unmapped;
  5. 名称含货币/快线/快钱的产品剔除;仍未映射的产品按参数处理(见 build_pool);
  6. 上市满 listed_days 自然日;过去 liquidity_days 个交易日均成交额
     >= liquidity_min(按返回行均值,缺行不校验完整性,无数据记0);
  7. 每个 pool_key 保留均额最大的一只;同额按代码序取第一,结果确定。

平台依赖按模块级名字引用(get_all_securities / finance / query / get_price),
聚宽编辑器整文件粘贴可用;本地测试对模块对象桩替,见 tests/test_etf_pool.py。
"""
from jqdata import *
import re

import pandas as pd

def _info(text):
    """回测环境用平台注入的 log;本地以桩导入且未设置 log 时退回 print。"""
    try:
        log.info(text)
    except NameError:
        print(text)

# run_query 单次返回行数有上限,且每只基金可能有多条公告记录,一次全查可能
# 被静默截断;小批量配合服务端过滤更稳。50 是 reversal_v1 的保守值。
BATCH_SIZE = 50
# 多标的 get_price 同理:单次标的数过大容易超时,100 是本项目惯例。
PRICE_BATCH_SIZE = 100
LIQUIDITY_DAYS = 20           # 均额统计窗口(交易日)
LIQUIDITY_MIN = 30_000_000.0  # 20日均成交额门槛(元)
LISTED_MIN_DAYS = 365         # 上市满 N 自然日
CASH_FUND_PATTERN = '货币|快线|快钱'
# 只认这些后缀:后缀是交易所定位符,不是指数身份;不去后缀会把同一指数拆成多组。
SUFFIX_RE = re.compile(r'\.(?:XSHG|XSHE|SH|SZ|CSI)$')
NAME_CODE_RE = re.compile(r'[（(](\d{6}(?:\.(?:XSHG|XSHE|SH|SZ|CSI))?)[）)]')


def plain_code(code):
    """去交易所后缀并大写:000300.XSHG / 000300.SH / 000300 归一为 000300。"""
    return SUFFIX_RE.sub('', str(code).strip().upper())


def index_identity(row):
    """单只ETF的指数标识,返回 (index_key, mapping_source)。

    只归并已知后缀的六位代码;非六位官方代码(如HSI)不参与归并,单列一组,
    保留暴露但没有去重收益——单列是安全的,错并才是事故。名称提取到多个
    不同代码视为歧义,退回按标准化名称分组;两者皆无记 unmapped。
    """
    raw = row.get('traced_index_code')
    if pd.notna(raw) and str(raw).strip():
        token = plain_code(raw)
        if token.isdigit() and len(token) == 6:
            return token, 'official_code'
        return token, 'unmapped_code'
    name = row.get('traced_index_name')
    if not (pd.notna(name) and str(name).strip()):
        return None, 'unmapped'
    keys = {plain_code(token) for token in NAME_CODE_RE.findall(str(name).upper())}
    if len(keys) == 1:
        return keys.pop(), 'parsed_code'
    display = 'NAME:' + re.sub(r'\s+', '', str(name)).upper()
    return display, 'name_ambiguous' if keys else 'name_only'


def classify_etfs(asof, batch_size=BATCH_SIZE):
    """点时全量分类快照:全市场ETF带 index_key / pool_key / mapping_source /
    cash_like。未映射的 index_key、pool_key 为 NaN;这里不做政策剔除,
    也不做流动性与上市期过滤,供策略参数与人工盘点使用。
    """
    date = pd.Timestamp(asof).normalize()
    date_str = str(date.date())
    securities = get_all_securities(['fund'], date=date_str)
    if securities is None or securities.empty:
        raise ValueError('历史基金名单为空')
    etfs = securities[securities['type'].astype(str).str.lower() == 'etf'].copy()
    codes = sorted(etfs.index)
    if not codes:
        raise ValueError('历史ETF名单为空')

    records = []
    for offset in range(0, len(codes), batch_size):
        batch = codes[offset:offset + batch_size]
        result = finance.run_query(query(finance.FUND_INVEST_TARGET).filter(
            finance.FUND_INVEST_TARGET.code.in_(batch),
            finance.FUND_INVEST_TARGET.pub_date <= date_str))
        if result is not None and not result.empty:
            records.append(result)

    if records:
        mapping = pd.concat(records, ignore_index=True)
        for field in ['pub_date', 'start_date', 'end_date']:
            mapping[field] = pd.to_datetime(mapping[field])
        # 公告日服务端已过滤,此处兜底防截断或脏行。
        mapping = mapping[mapping.pub_date <= date]
        # 只用 asof 当日已生效的记录,再按公告日取每只最新一条;同一公告日
        # 多条时取结果顺序最后一条,不保证稳定。
        mapping = mapping[(mapping.start_date <= date)
                          & (mapping.end_date.isna() | (mapping.end_date > date))]
        mapping = mapping.sort_values('pub_date').drop_duplicates('code', keep='last')
        etfs = etfs.join(mapping.set_index('code')[
            ['traced_index_name', 'traced_index_code']])
    else:
        etfs['traced_index_name'] = None
        etfs['traced_index_code'] = None

    identities = [index_identity(row) for _, row in etfs.iterrows()]
    etfs['index_key'] = [key for key, _ in identities]
    etfs['mapping_source'] = [source for _, source in identities]
    etfs['pool_key'] = etfs['index_key']
    # 货币类产品无论是否映射都不是想要的暴露;名称匹配只做剔除标记。
    etfs['cash_like'] = etfs['display_name'].astype(str).str.contains(
        CASH_FUND_PATTERN, na=False)
    _info('分类 %s:ETF=%d 有标识=%d 未映射=%d 现金类=%d'
          % (date_str, len(etfs), int(etfs.index_key.notna().sum()),
             int(etfs.index_key.isna().sum()), int(etfs.cash_like.sum())))
    return etfs


def average_money(codes, date_str, days=LIQUIDITY_DAYS,
                  batch_size=PRICE_BATCH_SIZE):
    """过去 days 个交易日均成交额;skip_paused 缺行按实际返回行数平均,
    不逐日校验完整性,无数据记0。"""
    records = []
    for offset in range(0, len(codes), batch_size):
        data = get_price(codes[offset:offset + batch_size], end_date=date_str,
                         count=days, frequency='daily', fields=['money'],
                         skip_paused=True, panel=False)
        if data is not None and not data.empty:
            records.append(data)
    if not records:
        return pd.Series(0.0, index=codes)
    means = pd.concat(records, ignore_index=True).groupby('code')['money'].mean()
    return means.reindex(codes).fillna(0.0).astype(float)


def build_pool(asof, *, liquidity_min=LIQUIDITY_MIN, liquidity_days=LIQUIDITY_DAYS,
               listed_days=LISTED_MIN_DAYS, manual_groups=None,
               keep_unmapped=False, batch_size=BATCH_SIZE):
    """点时快照建池,返回 DataFrame(code / display_name / pool_key / index_key /
    mapping_source / avg_money),按均额降序。

    未映射产品的三种处理,由参数表达:
      默认           全部剔除(含黄金)——pool_key 的意义是“一个暴露一个名额”,
                     未知暴露无法安全去重;
      manual_groups  人工审核的显式名单 {组名: [ETF代码]},把确认同质的未映射
                     产品(如黄金)归并成组;用代码名单,不用名称正则;
      keep_unmapped  每只未映射产品自成一组,一个不漏,但同暴露会占多个名额,
                     去重保护失效;只用于盘点,不用于交易。
    """
    date = pd.Timestamp(asof).normalize()
    date_str = str(date.date())
    etfs = classify_etfs(asof, batch_size=batch_size)
    total = len(etfs)
    etfs = etfs[~etfs['cash_like']].copy()

    if manual_groups:
        manual = {plain_code(code): str(key)
                  for key, group in manual_groups.items() for code in group}
        assigned = pd.Series([manual.get(plain_code(code)) for code in etfs.index],
                             index=etfs.index)
        fill = etfs['pool_key'].isna() & assigned.notna()
        etfs.loc[fill, 'index_key'] = assigned[fill]
        etfs.loc[fill, 'pool_key'] = assigned[fill]
        etfs.loc[fill, 'mapping_source'] = 'manual'

    rest = etfs['pool_key'].isna()
    if keep_unmapped:
        etfs.loc[rest, 'pool_key'] = [plain_code(code) for code in etfs.index[rest]]
    else:
        etfs = etfs[~rest].copy()
    classifiable = len(etfs)

    etfs = etfs.sort_index()
    start = pd.to_datetime(etfs['start_date'], errors='coerce')
    etfs = etfs[start.notna()
                & (start <= date - pd.Timedelta(days=listed_days))].copy()
    listed = len(etfs)

    etfs['avg_money'] = average_money(etfs.index.tolist(), date_str,
                                      days=liquidity_days)
    etfs = etfs[etfs['avg_money'] >= liquidity_min].copy()
    liquid = len(etfs)

    # 稳定排序 + 同额按代码序,去重结果确定。
    etfs = etfs.sort_values('avg_money', ascending=False, kind='stable')
    selected = etfs.drop_duplicates(subset=['pool_key'], keep='first')
    _info('建池 %s:ETF=%d 可归类=%d 上市满%d日=%d 均额≥%.0f万=%d 代表=%d'
          % (date_str, total, classifiable, listed_days, listed,
             liquidity_min / 1e4, liquid, len(selected)))

    rows = [dict(code=str(code), display_name=str(row['display_name']),
                 pool_key=str(row['pool_key']),
                 index_key=(None if pd.isna(row['index_key'])
                            else str(row['index_key'])),
                 mapping_source=str(row['mapping_source']),
                 avg_money=float(row['avg_money']))
            for code, row in selected.iterrows()]
    return pd.DataFrame(rows, columns=['code', 'display_name', 'pool_key',
                                       'index_key', 'mapping_source',
                                       'avg_money'])
