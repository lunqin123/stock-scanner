"""core/scanner_utils.py 纯工具函数测试。"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import cache
import core.scanner_utils as su


def test_money_str():
    assert su.money_str(1.5e8) == "1.50亿"
    assert su.money_str(-2e8) == "-2.00亿"
    assert su.money_str(12345) == "1万"
    assert su.money_str(9999) == "9999"
    assert su.money_str(0) == "0"
    assert su.money_str("abc") == "abc"
    assert su.money_str(None) == "None"


@pytest.mark.parametrize("t,expected", [
    ("092500", 10.0),   # 09:25 竞价封板
    ("100000", 10.0),   # 10:00 整
    ("100100", 7.5),    # 10:01-10:30
    ("103000", 7.5),
    ("103100", 5.0),    # 10:31-11:30
    ("113000", 5.0),
    ("113100", 3.3),    # 11:31-13:00
    ("130000", 3.3),
    ("130100", 1.7),    # 13:01-14:00
    ("140000", 1.7),
    ("140100", 0.0),    # 尾盘
    ("143000", 0.0),
    ("9", 5.0),         # 无法解析
    ("abc", 5.0),
    ("", 5.0),
])
def test_seal_time_score(t, expected):
    assert su.seal_time_score(t) == expected


def test_vectorized_seal_time_score():
    s = pd.Series(['092500', '100000', '103000', '113000', '130000',
                   '140000', '143000', 'xx', ''])
    out = su._vectorized_seal_time_score(s)
    assert list(out) == [10.0, 10.0, 7.5, 5.0, 3.3, 1.7, 0.0, 5.0, 5.0]


def _cst_dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize("now,expected", [
    (_cst_dt(2026, 8, 3, 9, 31), 'trading'),   # 周一 09:31
    (_cst_dt(2026, 8, 3, 11, 29), 'trading'),
    (_cst_dt(2026, 8, 3, 11, 30), 'lunch'),    # 午休
    (_cst_dt(2026, 8, 3, 12, 59), 'lunch'),
    (_cst_dt(2026, 8, 3, 13, 0), 'trading'),   # 下午开盘
    (_cst_dt(2026, 8, 3, 14, 59), 'trading'),
    (_cst_dt(2026, 8, 3, 15, 0), 'closed'),    # 盘后
    (_cst_dt(2026, 8, 3, 9, 0), 'closed'),     # 盘前
])
def test_get_market_status_trading_hours(monkeypatch, now, expected):
    monkeypatch.setattr(cache, '_is_trading_day', lambda s: True)
    assert su.get_market_status(now=now) == expected


def test_get_market_status_weekend_and_holiday(monkeypatch):
    monkeypatch.setattr(cache, '_is_trading_day', lambda s: True)
    assert su.get_market_status(now=_cst_dt(2026, 8, 1, 10, 0)) == 'weekend'
    monkeypatch.setattr(cache, '_is_trading_day', lambda s: False)
    assert su.get_market_status(now=_cst_dt(2026, 8, 3, 10, 0)) == 'holiday'


def test_get_default_mode(monkeypatch):
    monkeypatch.setattr(su, 'get_market_status', lambda: 'trading')
    assert su.get_default_mode() == 'trend'
    monkeypatch.setattr(su, 'get_market_status', lambda: 'closed')
    assert su.get_default_mode() == 'after_hours'
