"""signals/recommendation_tracker.py 推荐追踪测试 (文件走临时目录)。"""
import json
import os

import cache
import signals.recommendation_tracker as rt


def test_date_path(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, 'DATA_DIR', str(tmp_path))
    assert rt._date_path('20260801') == str(tmp_path / '20260801.json')


def test_next_trading_day(monkeypatch):
    calendar = {'20260804'}
    monkeypatch.setattr(cache, '_is_trading_day', lambda s: s in calendar)
    assert rt._next_trading_day('20260803') == '20260804'
    monkeypatch.setattr(cache, '_is_trading_day', lambda s: False)
    assert rt._next_trading_day('20260803') is None


def test_save_and_get_recommendations(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, 'DATA_DIR', str(tmp_path / 'rec'))
    monkeypatch.setattr(rt, 'PERF_FILE', str(tmp_path / 'tracker_perf.json'))
    stocks = [{'code': '600000', 'name': '浦发银行', 'score': 90}]
    assert rt.save_recommendations('limit-up', stocks, date_str='20260803') == 1
    path = tmp_path / 'rec' / '20260803.json'
    assert os.path.exists(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    assert 'limit-up' in data
    assert data['limit-up'][0]['code'] == '600000'
    assert data['limit-up'][0]['rank'] == 1
    assert rt.save_recommendations('', [], '20260803') is None
    # PERF_FILE 尚未生成 → 追踪详情为空
    assert rt.get_daily_tracker('20260803') == {}
    perf = {'20260803': {'limit-up': {'count': 1, 'wins': 1, 'win_open': 1,
                                      'buyable': 1, 'details': []}}}
    with open(rt.PERF_FILE, 'w', encoding='utf-8') as f:
        json.dump(perf, f, ensure_ascii=False)
    got = rt.get_daily_tracker('20260803')
    assert got['limit-up']['count'] == 1


def test_ensure_dir(monkeypatch, tmp_path):
    target = tmp_path / 'a' / 'b'
    monkeypatch.setattr(rt, 'DATA_DIR', str(target))
    rt._ensure_dir()
    assert target.exists()
