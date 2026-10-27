"""Explicit fund distributions: historical price adjustment and cash entitlement dates."""
from datetime import date
import io
import json
import math
from pathlib import Path
import re

import pandas as pd
import requests


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


def get_fund_events(code, as_of, *, cache_dir=None):
    """Same-day cached complete event tables; failed fetches never imply no dividend."""
    date.fromisoformat(str(as_of))
    if not re.fullmatch(r'\d{6}', code):
        raise ValueError('Invalid fund code')
    directory = Path(cache_dir) if cache_dir is not None else Path(__file__).resolve().parents[1] / 'data/fund_actions'
    path = directory / f'{code}-{as_of}.json'
    if path.exists():
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('code') != code or payload.get('as_of') != str(as_of):
            raise ValueError('Fund event cache identity mismatch')
        events = payload['events']
    else:
        for attempt in range(2):
            try:
                response = requests.get(f'https://fundf10.eastmoney.com/fhsp_{code}.html', timeout=15)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt:
                    raise
        response.encoding = 'utf-8'
        events = parse_events(response.text)
    for event in events:
        cash = float(event['cash_per_share'])
        if not math.isfinite(cash) or cash < 0:
            raise ValueError('Invalid dividend amount')
        if cash:
            registered, ex, paid = (date.fromisoformat(event[k]) for k in ('record_date', 'date', 'pay_date'))
            if not registered <= ex <= paid:
                raise ValueError('Invalid dividend entitlement/ex/payment dates')
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(code=code, as_of=str(as_of), events=events), ensure_ascii=False), encoding='utf-8')
    return [event for event in events if event['date'] <= str(as_of)]


def adjusted_close(raw, events, decision_date):
    """Split-adjusted history plus explicit cash adjustment, anchored to decision-day units.

    Retains the existing split treatment; cash dividends use the previous RAW close.
    No event or bar after the decision date may alter the signal.
    """
    from data_provider.bars import adjust_series
    raw = raw.copy()
    raw.index = pd.to_datetime(raw.index)
    if raw.index.has_duplicates:
        raise ValueError('Duplicate daily bars')
    raw = raw.loc[raw.index <= pd.Timestamp(decision_date)].sort_index().astype(float)
    result = adjust_series(raw)
    for event in events:
        ex = pd.Timestamp(event['date'])
        cash = float(event['cash_per_share'])
        if not cash or ex > pd.Timestamp(decision_date) or not len(raw) or ex <= raw.index[0]:
            continue
        prior = raw.loc[raw.index < ex]
        if prior.empty or not math.isfinite(prior.iloc[-1]) or prior.iloc[-1] <= cash:
            raise ValueError('Missing/invalid pre-dividend closing price')
        if event.get('split_ratio', 1.) != 1.:
            raise ValueError('Simultaneous distribution and split require verified units')
        result.loc[result.index < ex] *= (prior.iloc[-1] - cash) / prior.iloc[-1]
    return result
