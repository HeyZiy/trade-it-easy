# -*- coding: utf-8 -*-
"""将原ROE策略的候选池函数接到研究环境，不调用初始化或交易函数。

直接读取指定源文件的常量和候选池函数，避免复制筛选条件后逐渐分叉。
仅执行下列白名单函数；行情与状态由显式历史日期适配，不访问当前状态。
解禁查询及异常时放行沿用原逻辑，审计日志会暴露数据请求失败。
"""
import ast
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace
import warnings

import pandas as pd


FUNCTIONS = {'_report_date', '_prepare_fundamentals', '_prev_trade_day',
             '_mainboard_pool', '_st_filter', '_ban_filter', '_rank_candidates', 'build_signal'}


def load_pool_functions(source_path, api):
    source = Path(source_path).read_text(encoding='utf-8-sig')
    tree = ast.parse(source, filename=str(source_path))
    namespace = {'pd': pd, 'math': math, 'hashlib': hashlib, 'g': SimpleNamespace()}
    for name in ('query', 'indicator', 'valuation', 'get_trade_days', 'get_all_securities'):
        namespace[name] = getattr(api, name)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper():
                namespace[target.id] = ast.literal_eval(node.value)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in FUNCTIONS]
    missing = FUNCTIONS - {node.name for node in functions}
    if missing:
        raise ValueError('ROE source missing candidate functions: %s' % sorted(missing))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source_path), 'exec'), namespace)
    namespace['RANK_MODE'] = 'prev_change'
    return namespace


class ROECandidatePool:
    def __init__(self, api, source_path=None):
        self.api = api
        self.source_path = Path(source_path or
            Path(__file__).resolve().parents[2] / 'studies' / 'roe_quality' / 'roe_rotation_v1_1_2.py')
        self.ns = load_pool_functions(self.source_path, api)
        self.history_closes = None
        self.history_asof = None
        self.audit = []
        self.ranked = []
        self.valuation_codes = []
        self.signal = None
        self.asof = None
        self.ns.update(get_fundamentals=self._get_fundamentals, get_extras=self._get_extras,
                       get_locked_shares=self._get_locked_shares,
                       get_current_data=self._get_current_data, history=self._history)

    @property
    def rotate_every(self):
        return self.ns['ROTATE_EVERY']

    def _get_fundamentals(self, *args, **kwargs):
        if pd.Timestamp(kwargs['date']).normalize() != self.asof:
            raise ValueError('Unexpected ROE fundamentals cutoff')
        result = self.api.get_fundamentals(*args, **kwargs)
        if {'code', 'pe_ratio', 'pb_ratio'}.issubset(result.columns):
            self.valuation_codes = list(result['code'])
        return result

    def _audited_call(self, name, *args, **kwargs):
        try:
            return getattr(self.api, name)(*args, **kwargs)
        except Exception as exc:
            # 原函数会捕获后放行；保留行为，但不再让这件事悄悄发生。
            item = {'signal': self.signal, 'operation': name,
                    'error_type': type(exc).__name__, 'legacy_fail_open': True}
            self.audit.append(item)
            warnings.warn('%s %s failed (%s); original ROE filter may pass candidates' %
                          (self.signal.date(), name, type(exc).__name__), RuntimeWarning)
            raise

    def _get_extras(self, *args, **kwargs):
        return self._audited_call('get_extras', *args, **kwargs)

    def _get_locked_shares(self, *args, **kwargs):
        return self._audited_call('get_locked_shares', *args, **kwargs)

    def _get_current_data(self):
        # 仅请求信号日暂停交易标记，不读取信号日收盘价或当前日状态。
        codes = self.valuation_codes
        if not codes:
            return {}
        data = self.api.get_price(codes, start_date=self.signal, end_date=self.signal,
                                  frequency='daily', fields=['paused'], panel=False,
                                  fq='pre', skip_paused=False, fill_paused=True)
        if data.empty:
            raise ValueError('Missing signal-day paused states')
        states = data.set_index('code')['paused']
        if not set(codes).issubset(states.index) or states.reindex(codes).isna().any():
            raise ValueError('Incomplete signal-day paused states')
        return {c: SimpleNamespace(paused=bool(states.loc[c])) for c in codes}

    def _history(self, count, unit, field, security_list):
        if unit != '1d' or field != 'close':
            raise ValueError('Only daily close history is supported')
        codes = list(security_list)
        if not codes:
            closes = pd.DataFrame()
        else:
            # 原history保留停牌日并填前收盘；此处不顺带修改筛选口径。
            data = self.api.get_price(codes, count=count, end_date=self.asof,
                                      frequency='daily', fields=['close'], panel=False,
                                      fq='pre', skip_paused=False, fill_paused=True)
            if data.empty:
                closes = pd.DataFrame(columns=codes, dtype=float)
            else:
                data = data.copy()
                data['time'] = pd.to_datetime(data['time']).dt.normalize()
                closes = data.pivot(index='time', columns='code', values='close').reindex(columns=codes)
                if (closes.index > self.asof).any():
                    raise ValueError('History contains signal-day/future prices')
        self.history_closes = closes
        self.history_asof = self.asof
        return closes

    def __call__(self, api, asof, signal):
        if api is not self.api:
            raise ValueError('Pool and study must use the same data API')
        self.asof, self.signal = pd.Timestamp(asof).normalize(), pd.Timestamp(signal).normalize()
        self.history_closes, self.history_asof = None, None
        self.valuation_codes = []
        previous = pd.Timestamp(self.api.get_trade_days(end_date=self.signal.date(), count=2)[0]).normalize()
        if previous != self.asof:
            raise ValueError('asof is not the previous trading day of signal')
        context = SimpleNamespace(current_dt=self.signal + pd.Timedelta(hours=14, minutes=55))
        self.ranked = self.ns['build_signal'](context)
        return list(self.ranked)
