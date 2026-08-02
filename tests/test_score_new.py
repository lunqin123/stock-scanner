"""scoring/score_new.py 评分函数测试 (权重文件走临时目录)。"""
import json

import pandas as pd
import pytest

import plans.factors_v2 as fv2
import scoring.score_new as sn


def _full_df():
    return pd.DataFrame({
        '代码': ['600001', '000002', '600003'],
        '封板资金': [2e8, 1e8, 0.5e8],
        '成交额': [1e8, 1e8, 1e8],
        '首次封板时间': ['093000', '103000', '150000'],
        '换手率': [8.0, 3.0, 25.0],
        '连板数': [2, 1, 5],
        '炸板次数': [0, 1, 3],
        '流通市值': [50e8, 25e8, 300e8],
        '所属行业': ['A', 'A', 'B'],
        '最新价': [10.0, 4.0, 30.0],
    })


def test_compute_seal_ratio():
    df = _full_df()
    out = sn.compute_seal_ratio(df)
    assert list(out.round(2)) == [2.0, 1.0, 0.5]
    # 缺列 → 中性 0.5
    assert list(sn.compute_seal_ratio(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_seal_time_score():
    df = pd.DataFrame({'首次封板时间': ['092500', '103000', '113000', '140000', '150000', 'bad']})
    assert list(sn.compute_seal_time_score(df)) == [1.0, 0.9, 0.6, 0.3, 0.1, 0.5]
    assert list(sn.compute_seal_time_score(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_turnover_score():
    df = pd.DataFrame({'换手率': [5.0, 15.0, 3.0, 15.1, 2.0, 20.1, 30.1]})
    assert list(sn.compute_turnover_score(df)) == [1.0, 1.0, 0.8, 0.6, 0.5, 0.3, 0.1]
    assert list(sn.compute_turnover_score(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_consecutive_score():
    df = pd.DataFrame({'连板数': [2, 3, 1, 4, 6, 5]})
    assert list(sn.compute_consecutive_score(df)) == [1.0, 0.9, 0.7, 0.6, 0.2, 0.5]
    assert list(sn.compute_consecutive_score(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_zhaban_score():
    df = pd.DataFrame({'炸板次数': [0, 1, 2, 3]})
    assert list(sn.compute_zhaban_score(df)) == [1.0, 0.7, 0.4, 0.1]
    assert list(sn.compute_zhaban_score(pd.DataFrame({'x': [1]}))) == [1.0]


def test_compute_market_cap_score():
    df = pd.DataFrame({'流通市值': [50e8, 25e8, 180e8, 15e8, 300e8, 5e8, 600e8]})
    assert list(sn.compute_market_cap_score(df)) == [1.0, 0.85, 0.75, 0.6, 0.5, 0.3, 0.3]
    assert list(sn.compute_market_cap_score(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_sector_score():
    df = pd.DataFrame({'所属行业': ['A', 'A', 'A', 'B', 'B', 'C']})
    assert list(sn.compute_sector_score(df)) == [0.8, 0.8, 0.8, 0.6, 0.6, 0.3]
    assert list(sn.compute_sector_score(pd.DataFrame({'x': [1]}))) == [0.5]


def test_compute_price_score():
    df = pd.DataFrame({'最新价': [10.0, 4.0, 30.0, 2.0, 60.0]})
    assert list(sn.compute_price_score(df)) == [1.0, 0.7, 0.6, 0.3, 0.4]
    assert list(sn.compute_price_score(pd.DataFrame({'x': [1]}))) == [0.5]


@pytest.fixture
def weights_file(monkeypatch, tmp_path):
    p = tmp_path / "score_new_weights.json"
    monkeypatch.setattr(sn, '_FACTOR_WEIGHTS_FILE', str(p))
    return str(p)


def test_load_save_factor_weights(weights_file):
    sn.save_factor_weights({'seal_ratio': 30, 'unknown': 99})
    w = sn.load_factor_weights()
    assert w['seal_ratio'] == 30.0
    assert 'unknown' not in w
    assert sum(w.values()) == pytest.approx(108)   # 默认 100 - 22 + 30


def test_load_factor_weights_corrupted(weights_file):
    with open(weights_file, 'w', encoding='utf-8') as f:
        f.write('{broken')
    assert sn.load_factor_weights() == sn.FACTOR_WEIGHTS


def test_score_new_basic(weights_file):
    df = _full_df()
    out = sn.score_new(df)
    assert '新评分' in out.columns
    assert list(out['新评分']) == sorted(out['新评分'], reverse=True)
    for k in sn.FACTOR_WEIGHTS:
        assert f'f_{k}' in out.columns
    # 第三行: 尾盘+高换手+5板+大市值 → 分数最低
    assert out.iloc[-1]['代码'] == '600003'


def test_score_new_interaction_bonus(weights_file):
    df = pd.DataFrame({
        '代码': ['600001', '000002'],
        '封板资金': [0.6e8, 0.4e8],
        '成交额': [1e8, 1e8],
        '首次封板时间': ['093000', '093000'],
        '换手率': [8.0, 8.0],
        '连板数': [2, 2],
        '炸板次数': [0, 0],
        '流通市值': [50e8, 50e8],
        '所属行业': ['A', 'B'],
        '最新价': [10.0, 10.0],
    })
    out = sn.score_new(df)
    scores = dict(zip(out['代码'], out['新评分']))
    # seal_ratio 0.6 → 13.2 分; 0.4 → 8.8 分; 早封板+高封成比额外 +3
    assert scores['600001'] == pytest.approx(87.2)   # 84.2 + 3 交互加分
    assert scores['000002'] == pytest.approx(79.8)   # 无交互加分
    assert out['f_seal_ratio'].iloc[0] - out['f_seal_ratio'].iloc[1] == pytest.approx(4.4)


def test_score_new_v2_position_factor(weights_file, monkeypatch):
    df = _full_df()
    calls = []

    def fake_v2(d, today):
        calls.append(today)
        return {
            'momentum_consistency': pd.Series(5.0, index=d.index),
            'pullback_depth': pd.Series(5.0, index=d.index),
        }
    monkeypatch.setattr(fv2, 'compute_v2_factors', fake_v2)
    out = sn.score_new(df, today_str='20260803')
    assert calls == ['20260803']
    assert 'f_v2_mc' in out.columns
    assert 'f_v2_pd' in out.columns
    # mc=5/pd=5 → 0.95 * 1.00 = 0.95 乘性
    no_v2 = sn.score_new(_full_df())
    # 第 2 名 (000002) 总分未触 100 上限, 可验证 0.95 乘性调节
    assert out['新评分'].iloc[1] == pytest.approx(no_v2['新评分'].iloc[1] * 0.95, rel=1e-2)


def test_score_new_v2_exception_neutral(weights_file, monkeypatch):
    def boom(d, today): raise RuntimeError('v2 down')
    monkeypatch.setattr(fv2, 'compute_v2_factors', boom)
    out = sn.score_new(_full_df(), today_str='20260803')
    assert '新评分' in out.columns
    assert out['新评分'].iloc[0] > 0


def test_score_new_empty():
    assert sn.score_new(pd.DataFrame()) is not None
    assert sn.score_new(None) is None
