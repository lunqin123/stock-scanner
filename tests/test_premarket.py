"""signals/premarket.py 盘前信号聚合测试 (网络与缓存全部打桩)。"""
import cache
import signals.premarket as pm


def test_signals_aligned():
    assert pm._signals_aligned({}) is False
    assert pm._signals_aligned({'美股': 1}) is False
    assert pm._signals_aligned({'美股': 1, 'A50': 2, '人民币': 0.5}) is True
    assert pm._signals_aligned({'美股': 1, 'A50': 1, '人民币': -1, '流动': 1}) is True
    assert pm._signals_aligned({'美股': 1, 'A50': -1, '人民币': 1, '流动': -1}) is False


def _patch_fetchers(monkeypatch, us=(1.0, 0.5, '美股上涨'), a50=(0.6, 'A50涨'),
                    rmb=(0.3, '人民币升'), liq=(0.5, '流动性好')):
    monkeypatch.setattr(pm, '_fetch_us_market', lambda: us)
    monkeypatch.setattr(pm, '_fetch_a50_overnight', lambda: a50)
    monkeypatch.setattr(pm, '_fetch_rmb_rate', lambda: rmb)
    monkeypatch.setattr(pm, '_fetch_liquidity_signal', lambda: liq)


def test_get_premarket_signal_bullish(monkeypatch):
    _patch_fetchers(monkeypatch)
    monkeypatch.setattr(cache, 'get', lambda name: None)
    put_calls = []
    monkeypatch.setattr(cache, 'put', lambda name, data: put_calls.append(name))
    r = pm.get_premarket_signal()
    assert r['direction'] == '偏多'
    assert r['score'] == 9.5
    assert r['confidence'] == '高'
    assert r['confidence_note'] == '多信号共振'
    assert r['sentiment_mult'] == 1.135
    assert r['factors']['美股'] == 1.0
    assert len(put_calls) == 1


def test_get_premarket_signal_neutral(monkeypatch):
    _patch_fetchers(monkeypatch, us=(0, 0, '无'), a50=(0, '无'), rmb=(0, '无'), liq=(0, '无'))
    monkeypatch.setattr(cache, 'get', lambda name: None)
    monkeypatch.setattr(cache, 'put', lambda name, data: None)
    r = pm.get_premarket_signal()
    assert r['score'] == 5.0
    assert r['direction'] == '震荡'
    assert r['confidence'] == '低'
    assert r['confidence_note'] == '无可用盘前数据'


def test_get_premarket_signal_bearish(monkeypatch):
    _patch_fetchers(monkeypatch, us=(-2.0, -1.0, '美股大跌'), a50=(-3.0, 'A50大跌'),
                    rmb=(-2.0, '人民币贬'), liq=(-1.0, '流动性紧'))
    monkeypatch.setattr(cache, 'get', lambda name: None)
    monkeypatch.setattr(cache, 'put', lambda name, data: None)
    r = pm.get_premarket_signal()
    assert r['direction'] == '偏空'
    assert r['score'] == 0.0
    assert r['confidence'] == '高'


def test_get_premarket_signal_uses_cache(monkeypatch):
    cached = {'direction': '震荡', 'score': 5.0}
    monkeypatch.setattr(cache, 'get', lambda name: cached)
    monkeypatch.setattr(pm, '_fetch_us_market', lambda: (_ for _ in ()).throw(AssertionError('不应拉取')))
    assert pm.get_premarket_signal() == cached


def test_get_premarket_signal_cached(monkeypatch):
    sentinel = {'direction': '偏多'}
    monkeypatch.setattr(pm, 'get_premarket_signal', lambda: sentinel)
    pm._premarket_cache = None
    pm._premarket_cache_time = None
    first = pm.get_premarket_signal_cached(max_age_minutes=10)
    second = pm.get_premarket_signal_cached(max_age_minutes=10)
    assert first == sentinel
    assert second == sentinel
    # 缓存超龄 → 重新拉取
    pm._premarket_cache_time = None
    assert pm.get_premarket_signal_cached(max_age_minutes=10) == sentinel
