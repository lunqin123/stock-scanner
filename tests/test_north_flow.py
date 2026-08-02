"""signals/north_flow_tracker.py 北向资金逻辑测试 (网络打桩)。"""
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

import signals.north_flow_tracker as nf


def test_calc_minute_trend_accelerate_in():
    hgt = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    sgt = pd.Series([0.0] * 6)
    assert nf._calc_minute_trend(hgt, sgt, window=3) == '加速流入'


def test_calc_minute_trend_accelerate_out():
    hgt = pd.Series([6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    sgt = pd.Series([0.0] * 6)
    assert nf._calc_minute_trend(hgt, sgt, window=3) == '加速流出'


def test_calc_minute_trend_flat():
    hgt = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    assert nf._calc_minute_trend(hgt, None, window=3) == '持平'


def test_calc_minute_trend_in_out_thresholds():
    hgt = pd.Series([1.0, 1.0, 1.0, 1.5, 1.5, 1.5])   # diff 0.5 → 转流入
    assert nf._calc_minute_trend(hgt, None, window=3) == '转流入'
    hgt2 = pd.Series([1.5, 1.5, 1.5, 1.0, 1.0, 1.0])   # diff -0.5 → 转流出
    assert nf._calc_minute_trend(hgt2, None, window=3) == '转流出'


def test_calc_minute_trend_short_series():
    hgt = pd.Series([1.0, 2.0])
    # 只有 2 个样本: recent=[1,2] mean 1.5, earlier=[1] mean 1 → diff 0.5 → 转流入
    assert nf._calc_minute_trend(hgt, None, window=30) == '转流入'


def test_score_north_flow_factor(monkeypatch):
    monkeypatch.setattr(nf, 'get_north_flow_signal', lambda: {
        'direction_score': 2, 'direction': '流入',
        'cumulative_net': 5.2, 'signal': '强流入',
    })
    df = pd.DataFrame({'代码': ['600000', '000001']})
    scores, meta = nf.score_north_flow_factor(df)
    assert list(scores) == [7.0, 7.0]
    assert meta['north_direction'] == '流入'
    assert meta['north_cumulative_net'] == 5.2
    scores2, _ = nf.score_north_flow_factor(None)
    assert scores2.iloc[0] == 7.0


def test_get_north_flow_history(monkeypatch):
    df = pd.DataFrame({
        '日期': ['2026-07-30', '2026-07-31'],
        '净流入': [5e8, -3e8],
    })
    monkeypatch.setitem(sys.modules, 'akshare',
                        SimpleNamespace(stock_hsgt_north_net_flow_in_em=lambda: df))
    hist = nf.get_north_flow_history(5)
    assert hist[0]['date'] == '2026-07-31'
    assert hist[0]['net_flow_yi'] == -3.0
    assert hist[0]['direction'] == '流出'
    assert hist[1]['date'] == '2026-07-30'
    assert hist[1]['direction'] == '流入'


def test_get_north_flow_history_empty_and_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, 'akshare',
                        SimpleNamespace(stock_hsgt_north_net_flow_in_em=lambda: pd.DataFrame()))
    assert nf.get_north_flow_history(5) == []

    def boom(): raise RuntimeError('down')
    monkeypatch.setitem(sys.modules, 'akshare',
                        SimpleNamespace(stock_hsgt_north_net_flow_in_em=boom))
    assert nf.get_north_flow_history(5) == []


def test_get_north_flow_history_missing_cols(monkeypatch):
    monkeypatch.setitem(sys.modules, 'akshare',
                        SimpleNamespace(stock_hsgt_north_net_flow_in_em=lambda: pd.DataFrame({'x': [1]})))
    assert nf.get_north_flow_history(5) == []
