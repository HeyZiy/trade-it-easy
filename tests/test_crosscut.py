# -*- coding: utf-8 -*-
"""
横切件测试：data_provider/_crosscut.py

锁定 C3b 收敛后必须保持的语义（红线）：
- Throttle 非自适应模式 = 间隔补足 + 随机 jitter（akshare 原行为）
- Throttle 自适应模式 = 原 efinance 增强版：连续错误退避（0.5s/次，上限 3s）、
  60 秒无新错误自动清零、成功计数 -1（下限 0）
- TtlSnapshotCache = 命中/过期判断 + "空 DataFrame 作失败哨兵在 TTL 内不再刷新"
  + store 沿用刷新前 timestamp（把刷新耗时计入缓存年龄）
"""

import pandas as pd
import pytest

from data_provider import _crosscut as cc
from data_provider._crosscut import TtlSnapshotCache, Throttle


class _FakeTime:
    """时间桩：sleep 只记录时长、不推进时钟，时钟由测试显式拨动"""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)


@pytest.fixture
def fake_time(monkeypatch):
    ft = _FakeTime()
    monkeypatch.setattr(cc, "time", ft)
    # jitter 固定取上界，断言可预期
    monkeypatch.setattr(cc.random, "uniform", lambda a, b: b)
    return ft


# ---------- Throttle：非自适应（akshare 原行为） ----------

def test_first_wait_only_jitter(fake_time):
    t = Throttle(2.0, 5.0)
    t.wait()
    assert fake_time.sleeps == [5.0]
    assert t._last_request_time == 1000.0


def test_second_wait_tops_up_interval(fake_time):
    t = Throttle(2.0, 5.0)
    t.wait()
    # 时钟未推进：elapsed=0，补足 2s 后再 jitter 5s
    t.wait()
    assert fake_time.sleeps == [5.0, 2.0, 5.0]


def test_wait_after_interval_no_topup(fake_time):
    t = Throttle(2.0, 5.0)
    t.wait()
    fake_time.now += 10.0
    t.wait()
    assert fake_time.sleeps == [5.0, 5.0]


def test_non_adaptive_ignores_errors(fake_time):
    t = Throttle(2.0, 5.0, adaptive=False)
    for _ in range(4):
        t.record_error()
    fake_time.now += 10.0
    t.wait()
    # 无退避附加休眠，仅 jitter
    assert fake_time.sleeps == [5.0]


# ---------- Throttle：自适应（efinance 原行为） ----------

def test_adaptive_backoff_scales_and_caps(fake_time):
    t = Throttle(1.5, 3.0, adaptive=True)
    for _ in range(8):
        t.record_error()
    fake_time.now += 10.0
    t.wait()
    # min(8*0.5, 3.0) = 3.0 封顶，再接 jitter 3.0
    assert fake_time.sleeps == [3.0, 3.0]


def test_adaptive_error_count_resets_after_window(fake_time):
    t = Throttle(1.5, 3.0, adaptive=True)
    t.record_error()
    fake_time.now += 61.0
    t.wait()
    # 超过 60 秒无新错误：计数清零，无退避附加休眠
    assert fake_time.sleeps == [3.0]
    assert t._consecutive_errors == 0


def test_adaptive_error_within_window_keeps_backoff(fake_time):
    t = Throttle(1.5, 3.0, adaptive=True)
    t.record_error()
    fake_time.now += 59.0
    t.wait()
    assert fake_time.sleeps == [0.5, 3.0]


def test_record_success_decrements_to_floor(fake_time):
    t = Throttle(1.5, 3.0, adaptive=True)
    for _ in range(3):
        t.record_error()
    t.record_success()
    assert t._consecutive_errors == 2
    for _ in range(5):
        t.record_success()
    assert t._consecutive_errors == 0


# ---------- TtlSnapshotCache ----------

def test_cache_miss_before_store():
    cache = TtlSnapshotCache(ttl=600, label="x")
    assert cache.get(now=1000.0) is None


def test_cache_hit_within_ttl_and_expired_beyond():
    cache = TtlSnapshotCache(ttl=600, label="x")
    df = pd.DataFrame({"代码": ["600519"]})
    cache.store(df, timestamp=1000.0)
    assert cache.get(now=1599.0) is df
    assert cache.get(now=1601.0) is None


def test_empty_frame_sentinel_counts_as_hit():
    # 失败时调用方存入空 DataFrame：TTL 内视为命中，避免反复全量拉取
    cache = TtlSnapshotCache(ttl=1200, label="x")
    cache.store(pd.DataFrame(), timestamp=1000.0)
    hit = cache.get(now=1100.0)
    assert hit is not None and hit.empty


def test_store_none_counts_as_miss():
    cache = TtlSnapshotCache(ttl=600, label="x")
    cache.store(None, timestamp=1000.0)
    assert cache.get(now=1001.0) is None


def test_store_explicit_timestamp_counts_refresh_time():
    # 刷新耗时计入缓存年龄：store 用的 timestamp 是刷新前取的 now
    cache = TtlSnapshotCache(ttl=600, label="x")
    cache.store(pd.DataFrame({"a": [1]}), timestamp=1000.0)
    assert cache.get(now=1599.0) is not None
    assert cache.get(now=1601.0) is None
