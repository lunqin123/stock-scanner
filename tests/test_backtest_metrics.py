"""backtest/backtest_metrics.py 聚合统计与确定性成交测试。"""
import pandas as pd
import pytest

import backtest.backtest_metrics as bm


def test_deterministic_fill_boundaries():
    assert bm._deterministic_fill('000001', '20260701', 1.0) is True
    assert bm._deterministic_fill('000001', '20260701', 0.0) is False
    assert bm._deterministic_fill('000001', '20260701', 99.0) is True
    assert bm._deterministic_fill('000001', '20260701', -1.0) is False


def test_deterministic_fill_reproducible():
    a = bm._deterministic_fill('000001', '20260701', 0.5)
    for _ in range(3):
        assert bm._deterministic_fill('000001', '20260701', 0.5) == a


def test_aggregate_empty():
    assert bm._aggregate([]) is None


def test_aggregate_all_win():
    r = bm._aggregate([
        {'net_ret_pct': 2.0, 'pnl': 100},
        {'net_ret_pct': 3.0, 'pnl': 150},
    ])
    assert r['trade_count'] == 2
    assert r['win_rate'] == 100.0
    assert r['ev'] == 2.5
    assert r['cumulative_ret'] == pytest.approx(5.06, abs=0.01)
    assert r['cumulative_ret_sum'] == 5.0
    assert r['max_dd'] == 0.0
    assert r['plr'] == 0
    assert r['best'] == 3.0
    assert r['worst'] == 2.0


def test_aggregate_mixed():
    r = bm._aggregate([
        {'net_ret_pct': 10.0, 'pnl': 100},
        {'net_ret_pct': -5.0, 'pnl': -50},
    ])
    assert r['win_rate'] == 50.0
    assert r['ev'] == 2.5
    assert r['win_avg'] == 10.0
    assert r['loss_avg'] == -5.0
    assert r['plr'] == 2.0
    assert r['cumulative_ret'] == pytest.approx(4.5, abs=0.01)
    assert r['max_dd'] == pytest.approx(-5.0, abs=0.01)


def test_aggregate_all_loss():
    r = bm._aggregate([{'net_ret_pct': -2.0, 'pnl': -20}])
    assert r['win_rate'] == 0.0
    assert r['ev'] == -2.0
    # 单笔样本的资金曲线从 1 直接到 0.98, 峰值即当前值 → 回撤 0
    assert r['max_dd'] == 0.0


def test_compute_factor_ics():
    records = [
        {'f_seal': 1, 'f_money': 2, 'net_ret_pct': 1},
        {'f_seal': 2, 'f_money': 2, 'net_ret_pct': 2},
        {'f_seal': 3, 'f_money': 2, 'net_ret_pct': 3},
    ]
    ics = bm._compute_factor_ics(records, 'limit-up')
    assert ics['seal'] == pytest.approx(1.0, abs=1e-4)
    assert 'money' not in ics   # 常数因子跳过


def test_compute_factor_ics_negative():
    records = [
        {'f_seal': 1, 'net_ret_pct': 3},
        {'f_seal': 2, 'net_ret_pct': 2},
        {'f_seal': 3, 'net_ret_pct': 1},
    ]
    ics = bm._compute_factor_ics(records, 'limit-up')
    assert ics['seal'] == pytest.approx(-1.0, abs=1e-4)


def test_compute_factor_ics_edge_cases():
    assert bm._compute_factor_ics([], 'limit-up') == {}
    assert bm._compute_factor_ics([{'a': 1}, {'a': 2}], 'limit-up') == {}
    assert bm._compute_factor_ics(
        [{'f_seal': 1, 'net_ret_pct': 1}] * 3, 'sector') == {}
    # 缺 net_ret_pct
    assert bm._compute_factor_ics(
        [{'f_seal': 1}, {'f_seal': 2}, {'f_seal': 3}], 'limit-up') == {}
    # 无因子列
    assert bm._compute_factor_ics(
        [{'net_ret_pct': 1}, {'net_ret_pct': 2}, {'net_ret_pct': 3}], 'limit-up') == {}


def test_compute_factor_ics_trend_prefix():
    records = [
        {'trend_chg': 1, 'trend_turnover': 5, 'net_ret_pct': 1},
        {'trend_chg': 2, 'trend_turnover': 6, 'net_ret_pct': 2},
        {'trend_chg': 3, 'trend_turnover': 7, 'net_ret_pct': 3},
    ]
    ics = bm._compute_factor_ics(records, 'trend')
    assert set(ics) == {'chg', 'turnover'}
    assert ics['chg'] == pytest.approx(1.0, abs=1e-4)
