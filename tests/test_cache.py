"""core/cache.py 缓存逻辑测试 (全部走临时目录, 不触网)。"""
import json
import os
import pickle
import time

import pytest

import core.cache as cache_mod


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    d = tmp_path / "cache"
    monkeypatch.setattr(cache_mod, '_CACHE_DIR', str(d))
    monkeypatch.setattr(cache_mod, '_is_market_frozen', lambda: True)
    monkeypatch.setattr(cache_mod, '_trading_date', lambda: '2026-08-01')
    return str(d)


def test_get_put_roundtrip(cache_dir):
    cache_mod.put('foo', {'a': 1})
    assert cache_mod.get('foo') == {'a': 1}


def test_get_expired(cache_dir):
    cache_mod.put('foo', {'a': 1})
    path = os.path.join(cache_dir, f"foo_v{cache_mod._CACHE_VER}.pkl")
    old = time.time() - cache_mod._CACHE_TTL - 10
    os.utime(path, (old, old))
    assert cache_mod.get('foo') is None


def test_get_corrupted(cache_dir):
    path = os.path.join(cache_dir, f"foo_v{cache_mod._CACHE_VER}.pkl")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'not-a-pickle')
    assert cache_mod.get('foo') is None


def test_persistent_get_put_roundtrip(cache_dir):
    cache_mod.persistent_put('hist', [1, 2, 3])
    assert cache_mod.persistent_get('hist') == [1, 2, 3]


def test_persistent_get_migrates_old_version(cache_dir):
    old_ver = cache_mod._CACHE_VER - 1
    old_path = os.path.join(cache_dir, f"hist_v{old_ver}.pkl")
    os.makedirs(cache_dir, exist_ok=True)
    with open(old_path, 'wb') as f:
        pickle.dump({'old': True}, f)
    assert cache_mod.persistent_get('hist') == {'old': True}
    assert os.path.exists(os.path.join(cache_dir, f"hist_v{cache_mod._CACHE_VER}.pkl"))
    assert not os.path.exists(old_path)


def test_make_key():
    key = cache_mod.make_key('bt', 'result', version=6, tab='trend', top_n=3)
    assert key == 'bt_result_tabtrend_top_n3_v6'
    # None 参数跳过, 参数排序稳定
    k2 = cache_mod.make_key('bt', 'result', version=6, b=2, a=1)
    assert k2 == 'bt_result_a1_b2_v6'
    k3 = cache_mod.make_key('bt', 'result', version=6, a=None)
    assert k3 == 'bt_result_v6'


def test_last_trading_date(monkeypatch):
    calendar = {'20260730', '20260731'}
    monkeypatch.setattr(cache_mod, '_is_trading_day', lambda s: s in calendar)
    # 2026-08-03 是周一 → 上个交易日 2026-07-31 (周五)
    assert cache_mod._last_trading_date('20260803') == '20260731'
    # 周末 2026-08-01 → 同样回退到周五
    assert cache_mod._last_trading_date('20260801') == '20260731'
    # 跨节假日: 2026-07-29 非交易日 → 跳到 7-28
    calendar2 = {'20260727', '20260728'}
    monkeypatch.setattr(cache_mod, '_is_trading_day', lambda s: s in calendar2)
    assert cache_mod._last_trading_date('20260729') == '20260728'


def test_daily_set_get(cache_dir):
    cache_mod.daily_set('key1', {'x': 1})
    assert cache_mod.daily_get('key1') == {'x': 1}
    path = os.path.join(cache_dir, "daily_2026-08-01_key1_v16.json")
    assert os.path.exists(path)


def test_daily_set_skips_when_not_frozen(monkeypatch, cache_dir):
    monkeypatch.setattr(cache_mod, '_is_market_frozen', lambda: False)
    cache_mod.daily_set('key1', {'x': 1})
    assert cache_mod.daily_get('key1') is None


def test_daily_set_does_not_overwrite_without_force(cache_dir):
    cache_mod.daily_set('key1', {'x': 1})
    cache_mod.daily_set('key1', {'x': 2})
    assert cache_mod.daily_get('key1') == {'x': 1}
    cache_mod.daily_set('key1', {'x': 2}, force=True)
    assert cache_mod.daily_get('key1') == {'x': 2}


def test_daily_get_removes_stale_cross_day(monkeypatch, cache_dir):
    path = os.path.join(cache_dir, f"daily_2026-08-01_key1_v{cache_mod._CACHE_VER}.json")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'x': 1}, f)
    # 模拟次日读取: _trading_date 返回明天, 文件 mtime 仍是今天 → 删除返 None
    monkeypatch.setattr(cache_mod, '_trading_date', lambda: '2026-08-03')
    # _daily_path 固定指向旧文件, 使"跨日删除"分支可被触发
    monkeypatch.setattr(cache_mod, '_daily_path', lambda key: path)
    assert cache_mod.daily_get('key1') is None
    assert not os.path.exists(path)


def test_daily_pkl_roundtrip(cache_dir):
    payload = {'df': 'raw'}
    cache_mod.daily_set_pkl('raw1', payload, force=True)
    assert cache_mod.daily_get_pkl('raw1') == payload


def test_daily_pkl_corrupted(cache_dir):
    path = os.path.join(cache_dir, f"daily_2026-08-01_raw2_v{cache_mod._CACHE_VER}.pkl")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'bad')
    assert cache_mod.daily_get_pkl('raw2') is None


def test_clear_all(cache_dir):
    cache_mod.put('keep', 1)
    cache_mod.persistent_put('keep2', 2)
    old_path = os.path.join(cache_dir, f"old_v{cache_mod._CACHE_VER - 1}.json")
    with open(old_path, 'w') as f:
        json.dump({}, f)
    notes = os.path.join(cache_dir, "trading_calendar.txt")
    with open(notes, 'w') as f:
        f.write('20260801\n')
    cache_mod.clear_all()
    assert os.path.exists(os.path.join(cache_dir, f"keep_v{cache_mod._CACHE_VER}.pkl"))
    assert os.path.exists(os.path.join(cache_dir, f"keep2_v{cache_mod._CACHE_VER}.pkl"))
    assert not os.path.exists(old_path)
    assert os.path.exists(notes)
