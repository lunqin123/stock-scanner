"""scoring/probability_estimator.py 概率估计器测试 (网络部分全部打桩)。"""
import threading

import pytest

import backtest.backtest_engine as be
import scoring.probability_estimator as pe


def _trade(code='000001', score=70, buy_px=10.0, signal_close=10.0,
            next_open=10.1, next_close=10.2, signal_date='20260701'):
    return {
        'code': code, 'score': score, 'buy_px': buy_px,
        'signal_close': signal_close, 'next_open': next_open,
        'next_close': next_close, 'signal_date': signal_date,
    }


@pytest.mark.parametrize("score,expected", [
    (0, '0-60'), (59.99, '0-60'), (60, '60-70'), (69, '60-70'),
    (70, '70-80'), (80, '80-90'), (90, '90-101'), (100, '90-101'),
    (105, '90-100'),   # 超出边界 → 兜底
])
def test_band_of(score, expected):
    assert pe._band_of(score) == expected


def test_cache_key():
    assert pe._cache_key('trend') == 'proba_v1_tabtrend_v1'


def test_window(monkeypatch):
    monkeypatch.setattr(pe, '_trading_date', lambda: '20260731')
    monkeypatch.setattr(be, '_trading_dates_in_range',
                        lambda s, e, max_count=30: ['20260701', '20260731'])
    assert pe._window(30) == ('20260701', '20260731')
    monkeypatch.setattr(be, '_trading_dates_in_range',
                        lambda s, e, max_count=30: [])
    assert pe._window(30) == ('20260601', '20260731')


def test_build_bands_empty():
    assert pe._build_bands([], {}) == {'overall': None, 'bands': []}


def test_build_bands_overall_metrics():
    trades = [
        _trade(score=55, next_open=9.9, next_close=10.5),   # 低开高走, 次日涨
        _trade(score=65, next_open=10.0, next_close=10.2),   # 次日涨
        _trade(score=75, next_open=10.3, next_close=9.8),    # 低开? 否; 次日跌
        _trade(score=85, next_open=9.5, next_close=10.1),   # 低开高走, 次日涨
        _trade(score=95, next_open=10.1, next_close=10.0),   # 次日跌
        _trade(score=100, next_open=9.8, next_close=10.4),  # 低开高走, 次日涨
    ]
    week_map = {'000001': {f'202607{d:02d}': 10.0 + d * 0.1 for d in range(1, 12)}}
    res = pe._build_bands(trades, week_map)
    o = res['overall']
    assert o['n'] == 6
    assert o['next_day_up'] == 66.7   # 4/6
    assert o['low_open_rate'] == 50.0  # 3/6 低开
    assert o['low_open_high_walk'] == 100.0  # 3/3 低开高走
    assert o['week_n'] == 6
    assert o['week_up'] == 100.0
    assert len(res['bands']) == 5


def test_build_bands_small_sample_falls_back(monkeypatch):
    trades = [_trade(score=62), _trade(score=64)]   # band 60-70 只有 2 笔
    week_map = {}
    res = pe._build_bands(trades, week_map)
    assert res['overall']['n'] == 2
    band = res['bands'][0]
    assert band['band'] == '60-70'
    assert band['n'] == 2
    # 样本不足 → next_day_up 回退到总体
    assert band['next_day_up'] == res['overall']['next_day_up']
    assert band['week_up'] is None
    assert band['week_n'] == 0


def test_build_bands_week_missing():
    trades = [_trade(score=62)]
    res = pe._build_bands(trades, {})
    assert res['overall']['week_up'] is None
    assert res['overall']['week_n'] == 0


def test_build_bands_no_low_open():
    trades = [_trade(score=62, next_open=10.5, next_close=10.6)]  # 高开
    res = pe._build_bands(trades, {})
    assert res['overall']['low_open_rate'] == 0.0
    assert res['overall']['low_open_high_walk'] is None


def test_get_probabilities_cached(monkeypatch):
    cached = {'tab': 'limit-up'}
    monkeypatch.setattr(pe, 'daily_get', lambda key: cached)
    assert pe.get_probabilities('limit-up') == cached


class _FakeThread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        _FakeThread.started.append(self.target)


def test_get_probabilities_starts_build_once(monkeypatch):
    monkeypatch.setattr(pe, 'daily_get', lambda key: None)
    monkeypatch.setattr(threading, 'Thread', _FakeThread)
    _FakeThread.started.clear()
    first = pe.get_probabilities('zhaban')
    second = pe.get_probabilities('zhaban')
    assert first['status'] == 'building'
    assert second['status'] == 'building'
    assert len(_FakeThread.started) == 1
    pe._BUILDING.discard('zhaban')


def test_build_probabilities(monkeypatch):
    calls = {}
    monkeypatch.setattr(pe, '_window', lambda max_days=30: ('20260701', '20260731'))
    monkeypatch.setattr(pe, '_collect_trades',
                        lambda tab, s, e: [_trade(score=72), _trade(score=82)])
    monkeypatch.setattr(pe, '_fetch_week_closes', lambda codes, s, e: {})
    monkeypatch.setattr(pe, 'daily_get', lambda key: None)

    def fake_set(key, data, force=False):
        calls[key] = (data, force)
    monkeypatch.setattr(pe, 'daily_set', fake_set)

    out = pe.build_probabilities('limit-up', force=True)
    data = out['limit-up']
    assert data['tab'] == 'limit-up'
    assert data['window'] == ['20260701', '20260731']
    assert data['n_trades'] == 2
    assert data['overall']['n'] == 2
    assert len(calls) == 1
    _, force = calls[pe._cache_key('limit-up')]
    assert force is True


def test_build_probabilities_cached_skips_collect(monkeypatch):
    cached = {'tab': 'limit-up', 'cached': True}
    monkeypatch.setattr(pe, 'daily_get', lambda key: cached)
    monkeypatch.setattr(pe, '_collect_trades', lambda *a, **k: pytest.fail('不应拉取'))
    out = pe.build_probabilities('limit-up')
    assert out['limit-up'] is cached


def test_collect_trades_limit_up(monkeypatch):
    def fake_run(tab, start_date, end_date, top_n, min_score, use_cache, capital, buy_time):
        return {'trades': [
            {'code': '1', 'signal_date': '20260701', 'buy_date': '20260702',
             'score': 70, 'buy_price': 10.0, 'signal_close': 10.0,
             'raw_ret_pct': 1.0, 'sell_close': 10.2},
            {'code': '2', 'signal_date': '20260701', 'buy_date': '20260702',
             'score': 60, 'buy_price': 5.0, 'signal_close': 5.0,
             'raw_ret_pct': -2.0, 'sell_close': None},
            {'code': '3', 'signal_date': '20260701', 'score': 55},  # 缺价格 → 跳过
        ]}
    monkeypatch.setattr(be, 'run_tab_backtest', fake_run)
    out = pe._collect_trades('limit-up', '20260701', '20260731')
    assert len(out) == 2
    assert out[0]['code'] == '000001'
    assert out[0]['next_open'] == pytest.approx(10.1)
    assert out[0]['next_close'] == 10.2
    assert out[1]['next_close'] is None


def test_collect_trades_trend(monkeypatch):
    def fake_run(tab, start_date, end_date, top_n, min_score, use_cache, capital, buy_time):
        return {'trades': [
            {'code': '600001', 'signal_date': '20260701', 'buy_date': '20260702',
             'score': 66, 'buy_price': 10.0, 'signal_close': 10.0,
             'gap_open_pct': 0.5, 'buy_close': 10.3},
        ]}
    monkeypatch.setattr(be, 'run_tab_backtest', fake_run)
    out = pe._collect_trades('trend', '20260701', '20260731')
    assert out[0]['next_open'] == pytest.approx(10.05)
    assert out[0]['next_close'] == 10.3
