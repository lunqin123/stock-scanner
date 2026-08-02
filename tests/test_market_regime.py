"""signals/market_regime.py 市场状态分类器测试 (网络全部打桩)。"""
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

import signals.market_regime as mr


def _patch_fetchers(monkeypatch, *, status='trading', north=(1, '流入', []),
                    limits=(50, 5, 3), promo=0.2, rotation=0.5):
    monkeypatch.setattr(mr, 'get_market_status', lambda: status)
    monkeypatch.setattr(mr, '_get_north_flow_streak', lambda: north)
    monkeypatch.setattr(mr, '_get_today_limit_counts', lambda: limits)
    monkeypatch.setattr(mr, '_get_promotion_rate', lambda: promo)
    monkeypatch.setattr(mr, '_get_sector_rotation_speed', lambda: rotation)


def test_classify_regime_unknown(monkeypatch):
    _patch_fetchers(monkeypatch, status='weekend')
    r = mr.classify_regime()
    assert r['regime'] == 'unknown'
    assert r['confidence'] == 0
    assert r['strategy_weights'] == {}
    assert r['signals']['status'] == 'weekend'


def test_classify_regime_north_driven(monkeypatch):
    _patch_fetchers(monkeypatch, north=(3, '流入', [{}]), limits=(30, 5, 3),
                    promo=0.2, rotation=0.3)
    r = mr.classify_regime()
    assert r['regime'] == 'north_driven'
    assert r['label'] == '北向驱动市'
    assert r['confidence'] == 0.8
    assert r['position_advice'] == 1.1
    assert r['strategy_weights']['limit-up'] == 1.1
    assert r['signals']['north_signal'] == '强流入'


def test_classify_regime_sentiment_driven(monkeypatch):
    _patch_fetchers(monkeypatch, north=(1, '流入', []), limits=(100, 10, 5),
                    promo=0.2, rotation=0.5)
    r = mr.classify_regime()
    assert r['regime'] == 'sentiment_driven'
    assert r['label'] == '游资情绪市'
    assert r['confidence'] == 0.6
    assert r['position_advice'] == 1.0
    assert r['signals']['limit_up_cnt'] == 100
    assert r['strategy_weights']['zhaban'] == 1.1


def test_classify_regime_mixed_tie(monkeypatch):
    _patch_fetchers(monkeypatch, north=(1, '流入', []), limits=(50, 5, 3),
                    promo=0.2, rotation=0.5)
    r = mr.classify_regime()
    assert r['confidence'] == 0.4
    assert r['label'] == '混和市'


def test_classify_regime_defensive(monkeypatch):
    _patch_fetchers(monkeypatch, north=(4, '流出', []), limits=(10, 2, 5),
                    promo=0.05, rotation=0.3)
    r = mr.classify_regime()
    assert r['regime'] == 'defensive'
    assert r['label'] == '防御避险市'
    assert r['position_advice'] == 0.3
    assert r['signals']['north_signal'] == '持续流出'
    assert r['strategy_weights']['limit-up'] == 0.3


def test_classify_regime_quant_dominant(monkeypatch):
    _patch_fetchers(monkeypatch, north=(1, '流入', []), limits=(10, 40, 40),
                    promo=0.2, rotation=0.5)
    r = mr.classify_regime()
    assert r['regime'] == 'quant_dominant'
    assert r['position_advice'] == 0.5
    assert r['signals']['zhaban_rate'] == pytest.approx(0.44, abs=0.01)


def _fake_akshare(monkeypatch, **attrs):
    monkeypatch.setitem(sys.modules, 'akshare', SimpleNamespace(**attrs))


def test_get_today_limit_counts(monkeypatch):
    def pool(date): return pd.DataFrame({'a': [1, 2]})
    def zb(date): return pd.DataFrame({'a': [1]})
    def dt(date): return pd.DataFrame({'a': [1, 2, 3]})
    _fake_akshare(monkeypatch, stock_zt_pool_em=pool,
                  stock_zt_pool_zbgc_em=zb, stock_zt_pool_dtgc_em=dt)
    assert mr._get_today_limit_counts() == (2, 1, 3)


def test_get_today_limit_counts_fallback(monkeypatch):
    def boom(date): raise RuntimeError('akshare down')
    _fake_akshare(monkeypatch, stock_zt_pool_em=boom,
                  stock_zt_pool_zbgc_em=boom, stock_zt_pool_dtgc_em=boom)
    assert mr._get_today_limit_counts() == (0, 0, 0)


def test_get_north_flow_streak(monkeypatch):
    import north_flow_tracker as nf_shim
    history = [
        {'direction': '流入'}, {'direction': '流入'}, {'direction': '流入'},
        {'direction': '流出'},
    ]
    monkeypatch.setattr(nf_shim, 'get_north_flow_history', lambda days=10: history)
    assert mr._get_north_flow_streak() == (3, '流入', history)

    monkeypatch.setattr(nf_shim, 'get_north_flow_history', lambda days=10: [])
    assert mr._get_north_flow_streak() == (0, '无数据', [])

    def boom(days=10): raise RuntimeError('down')
    monkeypatch.setattr(nf_shim, 'get_north_flow_history', boom)
    assert mr._get_north_flow_streak() == (0, '无数据', [])


def test_get_sector_rotation_speed(monkeypatch):
    def pool(date):
        if date.endswith('20260731'):
            return pd.DataFrame({'所属行业': ['A', 'B', 'C']})
        return pd.DataFrame({'所属行业': ['A', 'B']})
    _fake_akshare(monkeypatch, stock_zt_pool_em=pool)
    # 重叠 {A,B} / 并集 {A,B,C} = 2/3 → speed = 1-2/3 = 0.33
    assert mr._get_sector_rotation_speed() == pytest.approx(0.33, abs=0.01)


def test_get_sector_rotation_speed_empty_today(monkeypatch):
    def pool(date): return pd.DataFrame()
    _fake_akshare(monkeypatch, stock_zt_pool_em=pool)
    assert mr._get_sector_rotation_speed() == 0.5


def test_get_sector_rotation_speed_exception(monkeypatch):
    def boom(date): raise RuntimeError('down')
    _fake_akshare(monkeypatch, stock_zt_pool_em=boom)
    assert mr._get_sector_rotation_speed() == 0.5


def test_get_promotion_rate(monkeypatch):
    prev = pd.DataFrame({
        'c0': ['a', 'b', 'c', 'd'],
        'c1': [1, 1, 1, 1],
        'c2': [1, 1, 1, 1],
        '涨幅': [10.0, -1.0, 9.5, 5.0],
    })
    _fake_akshare(monkeypatch, stock_zt_pool_previous_em=lambda date: prev)
    assert mr._get_promotion_rate() == 0.5


def test_get_promotion_rate_empty(monkeypatch):
    _fake_akshare(monkeypatch, stock_zt_pool_previous_em=lambda date: pd.DataFrame())
    assert mr._get_promotion_rate() == 0.0


def test_get_promotion_rate_exception(monkeypatch):
    def boom(date): raise RuntimeError('down')
    _fake_akshare(monkeypatch, stock_zt_pool_previous_em=boom)
    assert mr._get_promotion_rate() == 0.0
