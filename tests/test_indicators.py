"""scoring/indicators.py 快速指标 + 龙虎榜席位分级测试 (无网络)。"""
import pandas as pd
import pytest

import scoring.indicators as ind


@pytest.fixture(autouse=True)
def _clear_hist_cache():
    ind._HIST_CACHE.clear()
    yield
    ind._HIST_CACHE.clear()


def test_calc_seal_ratio():
    df = pd.DataFrame({
        '封板资金': [0, 1e8, None, 2e8],
        '成交额': [1e8, 1e8, 1e8, 0],
    })
    out = ind.calc_seal_ratio(df)
    assert list(out) == [0.0, 1.0, 0.0, 0.0]
    assert ind.calc_seal_ratio(pd.DataFrame({'其他': [1]})).tolist() == [0.0]


def test_calc_sector_leadership():
    df = pd.DataFrame({
        '首次封板时间': ['092500', '100000', '140000', 'bad', '093000'],
        '所属行业': ['A', 'A', 'A', 'B', 'B'],
    })
    out = ind.calc_sector_leadership(df)
    assert list(out) == [1, 2, 3, 2, 1]   # B 组: 'bad'(999) 排在 '093000'(570) 之后


def test_calc_sector_leadership_missing_cols():
    df = pd.DataFrame({'x': [1, 2]})
    assert list(ind.calc_sector_leadership(df)) == [1, 1]


def test_calc_concept_count():
    df = pd.DataFrame({'代码': ['600000', '000001']})
    assert list(ind.calc_concept_count(df)) == [1, 1]


def test_calc_seal_quality_label():
    s = pd.Series([10.0, 2.0, 0.8, 0.3, 0.1])
    assert list(ind.calc_seal_quality_label(s)) == ['死封', '强封', '一般', '偏弱', '弱封']


def test_calc_sector_leader_label():
    df = pd.DataFrame({'所属行业': ['A', 'A', 'A', 'B']}, index=['a', 'b', 'c', 'd'])
    ranks = pd.Series({'a': 1, 'b': 1, 'c': 2, 'd': 3})
    out = ind.calc_sector_leader_label(ranks, df)
    assert list(out) == ['龙头', '龙头', '前排', '跟风']


def test_grade_seat_name():
    assert ind.grade_seat_name('机构专用') == '机构'
    assert ind.grade_seat_name('沪股通专用') == '机构'
    assert ind.grade_seat_name('中关村') == '顶级游资'
    assert ind.grade_seat_name('散户之家') == '散户席位'
    assert ind.grade_seat_name('普通营业部') == '普通游资'


def _hist_df(closes, volumes=None, length=None):
    n = length if length is not None else len(closes)
    closes = list(closes) + [closes[-1]] * (n - len(closes))
    vols = list(volumes) if volumes is not None else [100] * n
    return pd.DataFrame({
        '收盘': closes,
        '成交额': [1e8] * n,
        '成交量': vols,
    })


def test_batch_fetch_hist(monkeypatch):
    calls = []

    def fake_fetch(code, start, end):
        calls.append(code)
        if code == '000002':
            return code, _hist_df([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])  # 恰好 10 行
        if code == '000003':
            return code, _hist_df([1], length=3)  # 不足 10 行, 不缓存
        raise RuntimeError('network fail')

    monkeypatch.setattr(ind, '_fetch_stock_hist', fake_fetch)
    df = pd.DataFrame({'代码': ['1', '2', '3']})
    out = ind._batch_fetch_hist(df, top_n=3)
    assert set(out) == {'000002'}
    assert set(calls) == {'000001', '000002', '000003'}


def test_batch_fetch_hist_uses_cache(monkeypatch):
    ind._HIST_CACHE['000001'] = _hist_df([1] * 10)
    calls = []
    monkeypatch.setattr(ind, '_fetch_stock_hist', lambda c, s, e: calls.append(c) or (c, None))
    df = pd.DataFrame({'代码': ['1', '1', '2']})
    out = ind._batch_fetch_hist(df, top_n=3)
    assert '000001' in out
    assert calls == ['000002']


def test_fetch_volume_ratio(monkeypatch):
    hist = _hist_df([10] * 6, volumes=[100, 100, 100, 100, 100, 200])
    monkeypatch.setattr(ind, '_batch_fetch_hist', lambda df, top_n=10: {'000001': hist})
    assert ind.fetch_volume_ratio(None) == {'000001': 2.0}


def test_fetch_volume_ratio_zero_avg(monkeypatch):
    hist = _hist_df([10] * 6, volumes=[0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(ind, '_batch_fetch_hist', lambda df, top_n=10: {'000001': hist})
    assert ind.fetch_volume_ratio(None) == {'000001': 1.0}


def test_fetch_position_type(monkeypatch):
    cases = {
        '高位加速': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    110, 130, 160],  # 当前 160 = 60日新高, 距20日低点 >30%
        '平台突破': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 105, 118],   # 118 > 20日高 105*0.95, 距低点 18%
        '底部首板': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100],   # 当前=20日低点 → 底部首板
        '趋势上行': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    140, 130, 125],   # 距20日低25%, 但未到20日高(140)的95%
        '震荡区间': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
                    110, 108, 112],   # 距低点12%, 未触任何阈值
    }
    for expected, closes in cases.items():
        monkeypatch.setattr(ind, '_batch_fetch_hist',
                            lambda df, top_n=10, c=closes: {'000001': _hist_df(c)})
        assert ind.fetch_position_type(None) == {'000001': expected}, expected


def test_fetch_position_type_short_history(monkeypatch):
    closes = [80, 90, 100]
    monkeypatch.setattr(ind, '_batch_fetch_hist',
                        lambda df, top_n=10: {'000001': _hist_df(closes)})
    # 当前 100, 距 20日低(80) 25%, 且 100 >= 100*0.95 → 平台突破
    assert ind.fetch_position_type(None) == {'000001': '平台突破'}


def test_fetch_returns_empty_when_ak_none(monkeypatch):
    monkeypatch.setattr(ind, 'ak', None)
    assert ind.fetch_volume_ratio(None) == {}
    assert ind.fetch_position_type(None) == {}
