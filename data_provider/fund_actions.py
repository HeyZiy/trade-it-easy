"""东财基金分红/拆分表解析（研究核验用）；生产复权用东财官方后复权行情。"""
import io
import re

import pandas as pd


def _distribution_dates(row):
    result = {}
    for field, column in [('record_date', '权益登记日'), ('pay_date', '分红发放日')]:
        if column in row:
            result[field] = pd.Timestamp(row[column]).date().isoformat()
    return result


def parse_events(html):
    tables = pd.read_html(io.StringIO(html))
    events = []
    found_cash = found_split = False
    for table in tables:
        if '除息日' in table.columns:
            found_cash = True
            if table.astype(str).apply(lambda c: c.str.contains('暂无分红')).any().any():
                continue
            amount_column = next((c for c in table.columns if '分红' in str(c) and '发放' not in str(c)), None)
            if amount_column is None:
                raise ValueError('分红表缺少金额字段')
            for row in table.to_dict('records'):
                value = str(row[amount_column]).replace(' ', '')
                match = re.fullmatch(r'每([\d.]+)份派现金([\d.]+)元', value)
                if not match or float(match[1]) <= 0:
                    raise ValueError('无法解析每份分红金额：' + value)
                events.append({'date': str(pd.Timestamp(row['除息日']).date()),
                               'cash_per_share': float(match[2]) / float(match[1]),
                               'split_ratio': 1.0, **_distribution_dates(row)})
        if '拆分折算日' in table.columns:
            found_split = True
            if table.astype(str).apply(lambda c: c.str.contains('暂无拆分')).any().any():
                continue
            ratio_column = next((c for c in table.columns if '比例' in str(c)), None)
            if ratio_column is None:
                raise ValueError('折算表缺少比例字段')
            for row in table.to_dict('records'):
                match = re.fullmatch(r'([\d.]+)[:：]([\d.]+)', str(row[ratio_column]).replace(' ', ''))
                if not match or min(float(match[1]), float(match[2])) <= 0:
                    raise ValueError('无法解析份额折算比例')
                events.append({'date': str(pd.Timestamp(row['拆分折算日']).date()),
                               'cash_per_share': 0.0,
                               'split_ratio': float(match[2]) / float(match[1])})
    if not found_cash and '暂无分红信息' not in html:
        raise ValueError('未找到分红表，不能推断没有分红')
    if not found_split and '暂无拆分信息' not in html:
        raise ValueError('未找到折算表，不能推断没有折算')
    if len({(e['date'], e['cash_per_share'], e['split_ratio']) for e in events}) != len(events):
        raise ValueError('权益事件重复')
    return sorted(events, key=lambda e: e['date'])