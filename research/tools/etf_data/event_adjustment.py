"""用明确的ETF现金分红/份额折算事件构建乘法复权，并与聚宽逐段核验。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
import requests


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
                               'split_ratio': 1.0})
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


def build_prices(raw, events):
    """只使用原始价和显式事件，不读取供应商hfq列，不按价格跳变猜事件。"""
    out = raw.copy().sort_values('date').reset_index(drop=True)
    if out.empty or out.date.duplicated().any():
        raise ValueError('原始行情为空或日期重复')
    prices = out['close'].astype(float)
    if not prices.map(np.isfinite).all() or not prices.gt(0).all():
        raise ValueError('原始收盘价无效')
    # 公开“拆分折算日”是份额登记处理日；交易价格在下一交易日除权。
    # SDK能提供精确生效日期/乘数时优先采用，保留公开登记日作来源。
    selected = []
    for event in events:
        if event.get('effective_date'):
            effective = event['effective_date']
        elif event['split_ratio'] != 1:
            following = out.loc[out.date > event['date'], 'date']
            effective = following.iloc[0] if len(following) else None
        else:
            effective = event['date']
        if effective is not None and out.date.iloc[0] < effective <= out.date.iloc[-1]:
            selected.append({**event, 'effective_date': effective})
    selected.sort(key=lambda e: e['effective_date'])
    dates = [e['effective_date'] for e in selected]
    if len(set(dates)) != len(dates):
        raise ValueError('同日多项权益事件需核对单位与处理顺序，拒绝猜测')
    factor, cursor, previous = 1.0, 0, None
    factors, applied = [], []
    for row in out.to_dict('records'):
        while cursor < len(selected) and selected[cursor]['effective_date'] <= row['date']:
            event = selected[cursor]
            cash = float(event['cash_per_share'])
            split = float(event['split_ratio'])
            if not np.isfinite(cash) or cash < 0 or not np.isfinite(split) or split <= 0:
                raise ValueError('权益事件数值无效')
            if cash and split != 1:
                raise ValueError('同一事件同时分红及折算，需明确每份分红单位')
            if previous is None or previous <= cash:
                raise ValueError('缺少除息参考前收盘，或分红金额不合理')
            multiplier = event.get('price_adjustment_multiplier', previous / (previous - cash) * split)
            if not np.isfinite(multiplier) or multiplier <= 0:
                raise ValueError('价格复权乘数无效')
            factor *= multiplier
            applied.append({**event, 'applied_date': row['date'],
                            'previous_raw_close': previous, 'cumulative_factor': factor})
            cursor += 1
        factors.append(factor)
        previous = float(row['close'])
    out['event_factor'] = factors
    out['event_close'] = out['close'] * out['event_factor']
    out.attrs['applied_events'] = applied
    return out


def sdk_split_precision(events, reference):
    """公开折算比例可能舍入，授权区间用聚宽精确价格因子及生效日期。"""
    reference = reference[reference.post_factor.gt(0) & reference.close.gt(0)].sort_values('date')
    result = []
    for event in events:
        adjusted = dict(event)
        if event['split_ratio'] != 1:
            before = reference[reference.date <= event['date']].tail(1)
            after = reference[reference.date > event['date']].head(1)
            if len(before) and len(after) and event['date'] >= reference.date.min():
                multiplier = float(after.iloc[0].post_factor / before.iloc[0].post_factor)
                if abs(multiplier / event['split_ratio'] - 1) > .002:
                    raise ValueError('SDK价格因子与公开折算比例不符，需核对生效日期')
                adjusted.update(effective_date=after.iloc[0].date,
                                price_adjustment_multiplier=multiplier,
                                factor_source='JoinQuant SDK post_factor ratio')
        result.append(adjusted)
    return result


def audit(prices, reference):
    x = prices[['date', 'close', 'event_close']].merge(reference[['date', 'close_post']], on='date')
    x = x[x.event_close.gt(0) & x.close_post.gt(0)].sort_values('date')
    if len(x) < 2:
        raise ValueError('聚宽交叉核验有效样本不足')
    scale = float((x.close_post / x.event_close).median())
    relative = (x.event_close * scale / x.close_post - 1).abs()
    gap = (x.event_close.pct_change() - x.close_post.pct_change()).abs()
    return {'rows': len(x), 'scale': scale, 'max_relative_price_error': float(relative.max()),
            'median_relative_price_error': float(relative.median()),
            'max_daily_return_gap_pp': float(gap.max() * 100),
            'median_daily_return_gap_pp': float(gap.median() * 100)}


def fetch_events(code, directory):
    path = directory / (code + '.json')
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        # 早先单品种探针只有窗口内分红，不能当成完整事件缓存。
        if record.get('complete_event_tables'):
            return record
    url = 'https://fundf10.eastmoney.com/fhsp_' + code + '.html'
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, timeout=(5, 15))
    response.raise_for_status()
    record = {'code': code, 'source': url, 'complete_event_tables': True,
              'fetched_at': pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),
              'events': parse_events(response.text)}
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jq-market', type=Path, required=True)
    parser.add_argument('--jq-sdk-market', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    events_dir = args.output / 'equity_events'
    events_dir.mkdir(exist_ok=True)
    daily_dir = args.output / 'etf_daily'
    daily_dir.mkdir(exist_ok=True)
    source = json.loads((args.source_cache / 'manifest.json').read_text(encoding='utf-8'))
    codes = source['requested_codes']
    old = pd.read_csv(args.jq_market)
    sdk = pd.read_csv(args.jq_sdk_market)
    failures, reports, all_frames = {}, {}, []
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = {executor.submit(fetch_events, code, events_dir): code for code in codes}
        for job in as_completed(jobs):
            code = jobs[job]
            try:
                events = job.result()['events']
                raw = pd.read_csv(args.source_cache / 'etf_daily' / (code + '.csv'))
                ref = sdk[sdk.code.str.startswith(code)]
                calibrated = sdk_split_precision(events, ref)
                prices = build_prices(raw, calibrated)
                report = {'applied_events': prices.attrs['applied_events'],
                          'split_precision_source': 'SDK授权区间使用精确因子；区间外使用公开比例与下一交易日生效规则'}
                report['sdk_overlap'] = audit(prices, ref)
                # 先检验真实构建结果，再决定是否可用于研究，不能用复制参考值掩盖失败。
                if report['sdk_overlap']['max_daily_return_gap_pp'] > .1:
                    raise ValueError('与聚宽同源核验日收益差超过0.1个百分点')
                previous = old[old.code.str.startswith(code)]
                if not previous.empty:
                    report['full_export_overlap'] = audit(prices, previous)
                keep = [c for c in ['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                                    'quality', 'event_factor', 'event_close'] if c in prices]
                prices = prices[keep].copy()
                prices['code'] = code
                prices['source'] = 'tencent_raw_plus_explicit_fund_events'
                if not previous.empty:
                    # 原15只已有平台完整数据，最终研究直接保留其原始/后复权序列。
                    direct = previous[previous.close_raw.gt(0) & previous.close_post.gt(0)].copy()
                    direct = direct.rename(columns={'close_raw': 'close', 'close_post': 'event_close', 'money': 'amount'})
                    direct['event_factor'] = direct.event_close / direct.close
                    direct['code'] = code
                    direct['source'] = 'original_joinquant_export'
                    prices = direct
                prices.to_csv(daily_dir / (code + '.csv'), index=False)
                all_frames.append(prices)
                reports[code] = report
                print('[%d/%d] %s 已通过SDK区间核验' % (len(reports) + len(failures), len(codes), code), flush=True)
            except Exception as error:
                failures[code] = '%s: %s' % (type(error).__name__, str(error))
                print('[failed] %s %s' % (code, failures[code]), flush=True)
    if all_frames:
        pd.concat(all_frames, ignore_index=True).sort_values(['code', 'date']).to_csv(args.output / 'market.csv', index=False)
    manifest = {'source': 'original JoinQuant export for original15; Tencent raw + explicit fund dividends/splits for added22',
                'requested_codes': codes, 'verified_codes': sorted(reports), 'failures': failures,
                'usable_for_returns': bool(codes) and not failures and len(reports) == len(codes),
                'validated_daily_return_max_gap_pp': max(
                    (item['sdk_overlap']['max_daily_return_gap_pp'] for item in reports.values()),
                    default=None),
                'validation_window': {'start': sdk.date.min(), 'end': sdk.date.max()}, 'reports': reports,
                'limits': ['新增行业全历史事件来自公开分红折算表；逐值同源核验仅覆盖SDK授权区间',
                           '复权基准常数不同不影响收益；历史缺价不填充；未验证10:40实时信号复现']}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('已验证%d/%d，失败%d' % (len(reports), len(codes), len(failures)), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
