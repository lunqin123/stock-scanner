"""core/scanner_filters.py 过滤逻辑测试。"""
import pandas as pd
import pytest

import config
import core.scanner_filters as sf


def _sample_df():
    return pd.DataFrame({
        '代码': ['600000', '000001', '002001', '300001', '688001', '830001', '920001'],
        '名称': ['浦发银行', '平安银行', '正常股', '创业股', '科创股', '北交股', '北交二'],
    })


def test_filter_non_main_board_default(monkeypatch):
    monkeypatch.setattr(config, 'INCLUDE_CHINEXT', False)
    out = sf.filter_non_main_board(_sample_df())
    assert list(out['代码']) == ['600000', '000001', '002001']


def test_filter_non_main_board_include_chinext(monkeypatch):
    monkeypatch.setattr(config, 'INCLUDE_CHINEXT', True)
    out = sf.filter_non_main_board(_sample_df())
    assert list(out['代码']) == ['600000', '000001', '002001', '300001', '688001']


def test_filter_non_main_board_excludes_st_and_delisted():
    df = pd.DataFrame({
        '代码': ['600000', '000001', '600001', '000002'],
        '名称': ['ST股', '*ST风险', '退市整理', '正常'],
    })
    out = sf.filter_non_main_board(df)
    # '退市整理' 以 '退' 开头同样被排除
    assert list(out['代码']) == ['000002']


def test_filter_xr_xd_dr():
    df = pd.DataFrame({'名称': ['XR平安银行', 'XD某股', 'DR某某', '正常名称']})
    out = sf.filter_xr_xd_dr(df)
    assert list(out['名称']) == ['正常名称']


def test_pre_filter():
    df = pd.DataFrame({
        '代码': ['600000', '600001', '600002', '600003'],
        '名称': ['正常', '一字板', '大市值', '尾盘板'],
        '换手率': [3.0, 0.2, 3.0, 3.0],
        '封板资金': [1e8, 10e8, 1e8, 1e8],
        '流通市值': [50e8, 50e8, 300e8, 50e8],
        '首次封板时间': ['093000', '093000', '093000', '150000'],
    })
    out = sf.pre_filter(df)
    assert list(out['代码']) == ['600000']


def test_pre_filter_missing_columns():
    df = pd.DataFrame({'代码': ['600000']})
    out = sf.pre_filter(df)
    assert list(out['代码']) == ['600000']


def test_filter_by_price():
    df = pd.DataFrame({'代码': ['000001', '600001', '000002']})
    fund_df = pd.DataFrame({
        '_code': ['000001', '600001'],
        '_price': [100.0, 10.0],   # 000001 超 60 元被排除
    })
    out = sf.filter_by_price(df, fund_df)
    assert list(out['代码']) == ['600001', '000002']


def test_filter_by_price_none_or_empty():
    df = pd.DataFrame({'代码': ['000001']})
    assert sf.filter_by_price(df, None) is df
    assert sf.filter_by_price(pd.DataFrame(), None) is not None
