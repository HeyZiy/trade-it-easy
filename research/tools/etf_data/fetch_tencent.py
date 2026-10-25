"""腾讯ETF原始价及hfq请求结果；复权未经验证，不直接用于信号或收益。"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import threading
import time

import numpy as np
import pandas as pd
import requests


SOURCE = 'tencent_fqkline_raw_hfq'
URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
ALTERNATE_URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
VERSION = 1
PRICE_FIELDS = ['open', 'close', 'high', 'low']
LOCAL = threading.local()


def intervals(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start > end:
        raise ValueError('起点晚于截止日')
    while start <= end:
        stop = min(pd.Timestamp(year=start.year + 1, month=12, day=31), end)
        yield start.strftime('%Y-%m-%d'), stop.strftime('%Y-%m-%d')
        start = stop + pd.Timedelta(days=1)


def parse_response(payload, symbol, adjustment, start, end, *,
                   allow_day_response=False, allow_leading_history=False):
    if payload.get('code') != 0:
        raise ValueError('腾讯返回非成功状态')
    data = payload.get('data')
    if not isinstance(data, dict) or symbol not in data:
        raise ValueError('腾讯响应缺少请求标的，不能当作上市前空行情')
    stock = data[symbol]
    key = 'hfqday' if adjustment == 'hfq' else 'day'
    response_key = key
    if key not in stock:
        # 腾讯对上市前的复权请求返回 day=[]；只接受确实为空的区间。
        if adjustment == 'hfq' and stock.get('day') == []:
            rows = []
            response_key = 'day'
        elif adjustment == 'hfq' and allow_day_response and isinstance(stock.get('day'), list):
            rows = stock['day']
            response_key = 'day'
        else:
            raise ValueError('响应缺少%s，不用原始价代替复权价' % key)
    else:
        rows = stock[key]
    if not isinstance(rows, list) or (len(rows) >= 640 and not allow_leading_history):
        raise ValueError('响应不是日线列表，或触及640条截断上限')
    columns = ['date', 'open', 'close', 'high', 'low', 'volume']
    if any(not isinstance(row, list) or len(row) < 6 for row in rows):
        raise ValueError('日线字段不足')
    frame = pd.DataFrame([row[:6] for row in rows], columns=columns)
    frame.attrs['response_key'] = response_key
    frame.attrs['returned_rows'] = len(rows)
    if frame.empty:
        return frame
    frame['date'] = pd.to_datetime(frame['date'], errors='raise').dt.strftime('%Y-%m-%d')
    if frame.date.duplicated().any():
        raise ValueError('响应日期重复')
    if allow_leading_history:
        # newfqkline忽略起点，返回截至终点的最多640根。只有覆盖请求起点
        # 才允许裁去多余的早期行情；不足覆盖时仍报截断，绝不静默少取历史。
        if len(rows) >= 640 and frame.date.min() > start:
            raise ValueError('640条响应未覆盖请求起点，历史被截断')
        if (frame.date > end).any():
            raise ValueError('响应含请求终点之后的行情')
        frame.attrs['leading_rows_discarded'] = int((frame.date < start).sum())
        attrs = dict(frame.attrs)
        frame = frame[frame.date >= start].copy()
        frame.attrs = attrs
    elif not frame.date.between(start, end).all():
        raise ValueError('响应超出请求日期，拒绝混入窗口外行情')
    frame[columns[1:]] = frame[columns[1:]].apply(pd.to_numeric, errors='coerce')
    return frame.sort_values('date').reset_index(drop=True)


def combine(raw, adjusted):
    if raw.empty or adjusted.empty or set(raw.date) != set(adjusted.date):
        raise ValueError('原始/后复权日期不齐或整只行情为空')
    out = raw.merge(adjusted[['date', *PRICE_FIELDS]].rename(
        columns={field: 'hfq_' + field for field in PRICE_FIELDS}),
        on='date', validate='one_to_one')
    # 腾讯原始成交量单位为手；未提供成交额，明确保留为空。
    out['volume'] = out['volume'] * 100.0
    out['amount'] = np.nan
    out = out.replace([np.inf, -np.inf], np.nan)
    price_columns = PRICE_FIELDS + ['hfq_' + field for field in PRICE_FIELDS]
    prices = out[price_columns]
    valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
    for prefix in ('', 'hfq_'):
        values = out[[prefix + field for field in PRICE_FIELDS]]
        valid &= out[prefix + 'high'].ge(values.max(axis=1))
        valid &= out[prefix + 'low'].le(values.min(axis=1))
    active = out.volume.notna() & out.volume.gt(0)
    out['quality'] = np.select([~valid, ~active], ['invalid_price', 'zero_activity'], default='ok')
    return out.sort_values('date').reset_index(drop=True)


def fetch_code(code, start, end, cache, *, direct=False, refresh=False,
               allow_day_response=False, history_floor=None):
    if len(code) != 6 or not code.isdigit() or code[0] not in ('1', '5'):
        raise ValueError('仅接受沪深ETF裸代码')
    path = cache / 'etf_daily' / (code + '.csv')
    meta_path = path.with_suffix('.json')
    if not refresh and path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if (meta.get('source') == SOURCE and meta.get('schema_version') == VERSION
                and meta.get('requested_start') == start and meta.get('requested_end') == end
                and (allow_day_response or not meta.get('hfq_day_response_rows', 0))
                and meta.get('sha256') == hashlib.sha256(path.read_bytes()).hexdigest()):
            return meta
    if not hasattr(LOCAL, 'session'):
        LOCAL.session = requests.Session()
    LOCAL.session.trust_env = not direct
    symbol = ('sz' if code[0] == '1' else 'sh') + code
    frames = {'': [], 'hfq': []}
    queries = []
    download_start = max(start, history_floor) if history_floor else start
    for first, last in intervals(download_start, end):
        for adjustment in ('', 'hfq'):
            params = {'param': '%s,day,%s,%s,640,%s' % (symbol, first, last, adjustment)}
            for attempt in range(2):
                try:
                    endpoint = URL if attempt == 0 else ALTERNATE_URL
                    response = LOCAL.session.get(endpoint, params=params, timeout=(5, 12))
                    response.raise_for_status()
                    frame = parse_response(response.json(), symbol, adjustment, first, last,
                                           allow_day_response=allow_day_response,
                                           allow_leading_history=endpoint == ALTERNATE_URL)
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 1:
                        raise
                    time.sleep(.3)
            frames[adjustment].append(frame)
            queries.append({'start': first, 'end': last, 'adjust': adjustment or 'raw',
                            'rows': len(frame), 'response_key': frame.attrs['response_key'],
                            'returned_rows': frame.attrs['returned_rows'],
                            'leading_rows_discarded': frame.attrs.get('leading_rows_discarded', 0),
                            'endpoint': endpoint})
            time.sleep(.05)
    raw = pd.concat(frames[''], ignore_index=True)
    hfq = pd.concat(frames['hfq'], ignore_index=True)
    if raw.date.duplicated().any() or hfq.date.duplicated().any():
        raise ValueError('分段拼接后日期重复')
    out = combine(raw, hfq)
    temporary = path.with_suffix('.csv.tmp')
    out.to_csv(temporary, index=False)
    temporary.replace(path)
    meta = {'schema_version': VERSION, 'source': SOURCE, 'endpoint': URL,
            'adjustment_verified': False, 'usable_for_returns': False,
            'code': code, 'requested_start': start, 'requested_end': end,
            'history_floor': history_floor,
            'fetched_at': pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),
            'first_bar': out.date.min(), 'last_bar': out.date.max(), 'rows': len(out),
            'quality_counts': {str(k): int(v) for k, v in out.quality.value_counts().items()},
            'hfq_day_response_rows': sum(q['rows'] for q in queries
                                         if q['adjust'] == 'hfq' and q['response_key'] == 'day'),
            'amount_available': False, 'volume_unit': 'shares', 'queries': queries,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    temporary = meta_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(meta_path)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codes', nargs='+', required=True)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--start-date', default='2015-10-01')
    parser.add_argument('--end-date', required=True)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--history-floor-file', type=Path,
                        help='已有行情首日表，仅用于跳过已知上市前空区间，不作为入池条件')
    parser.add_argument('--allow-day-response', action='store_true',
                        help='保留HFQ请求返回day字段的供应商响应，并显式标记，仍需交叉验证')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('workers必须为正')
    start, end = (pd.Timestamp(value).strftime('%Y-%m-%d')
                  for value in (args.start_date, args.end_date))
    list(intervals(start, end))
    now = pd.Timestamp.now(tz='Asia/Shanghai')
    completed = now.normalize() - pd.Timedelta(days=int(now.hour < 16))
    if pd.Timestamp(end) > completed.tz_localize(None):
        parser.error('截止日尚未完成日线')
    cache = args.cache_dir.resolve()
    (cache / 'etf_daily').mkdir(parents=True, exist_ok=True)
    codes = list(dict.fromkeys(args.codes))
    results, failures = {}, {}
    floors = {}
    if args.history_floor_file:
        floor_frame = pd.read_csv(args.history_floor_file, dtype={'code': str})
        floors = {row['code']: pd.Timestamp(row['start_date']).strftime('%Y-%m-%d')
                  for row in floor_frame.to_dict('records') if pd.notna(row['start_date'])}
    pending_codes = iter(codes)
    streak = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {}

        def submit():
            code = next(pending_codes, None)
            if code is not None:
                job = executor.submit(fetch_code, code, start, end, cache,
                                      direct=args.direct, refresh=args.refresh,
                                      allow_day_response=args.allow_day_response,
                                      history_floor=floors.get(code))
                jobs[job] = code

        for _ in range(args.workers):
            submit()
        while jobs:
            done, _ = wait(jobs, return_when=FIRST_COMPLETED)
            for job in done:
                code = jobs.pop(job)
                try:
                    results[code] = job.result()
                    streak = 0
                    print('[%d/%d] %s OK %d行' % (
                        len(results) + len(failures), len(codes), code, results[code]['rows']), flush=True)
                except Exception as error:
                    streak += 1
                    failures[code] = '%s: %s' % (type(error).__name__, str(error)[-200:])
                    print('[failed] %s %s' % (code, failures[code]), flush=True)
            if streak < 10:
                while len(jobs) < args.workers:
                    before = len(jobs)
                    submit()
                    if len(jobs) == before:
                        break
        for code in codes:
            if code not in results and code not in failures:
                failures[code] = '连续失败后未继续请求'
    manifest = {'schema_version': VERSION, 'source': SOURCE, 'start': start, 'end': end,
                'adjustment_verified': False, 'usable_for_returns': False,
                'requested_codes': codes, 'completed_codes': sorted(results), 'failures': failures,
                'amount_available': False,
                'price_basis': '原始价与hfq请求返回序列；day字段响应另行标记，未猜测调整因子',
                'hfq_day_response_codes': sorted(code for code, meta in results.items()
                                                  if meta.get('hfq_day_response_rows', 0)),
                'details': results}
    (cache / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('完成%d/%d，失败%d；%s' % (len(results), len(codes), len(failures), cache), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
