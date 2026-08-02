"""scoring/weight_manager.py 权重管理测试 (所有持久化文件走临时目录)。"""
import json
import os

import pandas as pd
import pytest

import backtest_engine
import scoring.weight_manager as wm


@pytest.fixture
def tmp_files(monkeypatch, tmp_path):
    def _p(name):
        return str(tmp_path / name)
    monkeypatch.setattr(wm, '_WEIGHTS_FILE', _p('weights.json'))
    monkeypatch.setattr(wm, '_WEIGHTS_FILE_B', _p('weights_b.json'))
    monkeypatch.setattr(wm, '_ROLLING_FILE', _p('rolling.json'))
    monkeypatch.setattr(wm, '_TAB_PERF_FILE', _p('tab_perf.json'))
    monkeypatch.setattr(wm, '_REV_WEIGHTS_FILE', _p('reversal_weights.json'))
    monkeypatch.setattr(wm, '_TREND_WEIGHTS_FILE', _p('trend_weights.json'))
    monkeypatch.setattr(wm, '_WEIGHT_HISTORY_FILE', _p('weight_history.jsonl'))
    monkeypatch.setattr(wm, '_WEIGHTS_FILES', {
        'zhaban': _p('zhaban_weights.json'),
        'dtqiaoban': _p('dtqiaoban_weights.json'),
        'limit-up': _p('limit_up_weights.json'),
        'reversal': _p('tab_reversal_weights.json'),
    })
    return tmp_path


def _scores(df, v):
    return pd.Series(v, index=df.index)


def test_apply_weights_defaults_max():
    df = pd.DataFrame({'代码': ['600000']})
    out = wm.apply_weights(
        seal_scores=_scores(df, 28), money_scores=_scores(df, 20),
        sector_scores=_scores(df, 15), tech_scores=_scores(df, 10),
        history_scores=_scores(df, 6), sentiment_score=10.0,
        stock_sentiment_scores=_scores(df, 10), principal_scores=_scores(df, 10),
        north_flow_scores=_scores(df, 10), alpha_scores=_scores(df, 10),
        crash_resistance_scores=_scores(df, 12))
    assert out.iloc[0] == pytest.approx(115.0, abs=0.01)


def test_apply_weights_sentiment_clamp():
    df = pd.DataFrame({'代码': ['600000']})
    kw = dict(seal_scores=_scores(df, 5), money_scores=_scores(df, 5),
              sector_scores=_scores(df, 5), tech_scores=_scores(df, 5),
              history_scores=_scores(df, 5))
    base5 = wm.apply_weights(sentiment_score=5.0, **kw).iloc[0]
    base0 = wm.apply_weights(sentiment_score=0.0, **kw).iloc[0]
    base20 = wm.apply_weights(sentiment_score=20.0, **kw).iloc[0]
    assert base0 == pytest.approx(base5 * 0.85)
    assert base20 == pytest.approx(base5 * 1.15)


def test_apply_weights_sector_fallback_and_defaults():
    df = pd.DataFrame({'代码': ['600000']})
    out = wm.apply_weights(
        seal_scores=_scores(df, 5), money_scores=_scores(df, 5),
        sector_scores=None, tech_scores=_scores(df, 5),
        history_scores=_scores(df, 5), sentiment_score=5.0,
        sector_res=_scores(df, 6), sector_mom=_scores(df, 10))
    # sector = (6+10)/2 = 8
    assert out.iloc[0] > 0


def test_load_save_weights_roundtrip(tmp_files):
    assert wm.load_weights() == wm.DEFAULT_WEIGHTS
    wm.save_weights({'seal': 30.0, 'unknown': 99})
    w = wm.load_weights()
    assert w['seal'] == 30.0
    assert w['money'] == wm.DEFAULT_WEIGHTS['money']
    assert 'unknown' not in w
    # Plan B 独立文件
    wm.save_weights({'seal': 10.0}, plan_name='B')
    assert wm.load_weights(plan_name='B')['seal'] == 10.0
    assert wm.load_weights()['seal'] == 30.0


def test_load_weights_corrupted(tmp_files):
    with open(wm._WEIGHTS_FILE, 'w', encoding='utf-8') as f:
        f.write('{bad json')
    assert wm.load_weights() == wm.DEFAULT_WEIGHTS


def test_save_daily_correlations_and_progress(tmp_files):
    wm.save_daily_correlations({'seal': 0.1}, trading_date='20260801')
    wm.save_daily_correlations({'seal': 0.2}, trading_date='2026-08-01')  # 同日期去重
    wm.save_daily_correlations({'seal': 0.3}, trading_date='20260802', plan_name='B')
    data = json.load(open(wm._ROLLING_FILE, encoding='utf-8'))
    assert len(data) == 2
    assert data[0]['date'] == '2026-08-01'
    assert wm.get_rolling_progress('A') == '回测数据  1/20 天'
    assert wm.get_rolling_progress('B') == '回测数据 Plan B 1/20 天'


def test_daily_adjust_weights_insufficient(tmp_files):
    wm.save_daily_correlations({'seal': 0.1}, trading_date='20260801')
    new_w, msg = wm.daily_adjust_weights(dict(wm.DEFAULT_WEIGHTS))
    assert new_w is None
    assert '至少需要 2 天' in msg


def test_daily_adjust_weights_plan_a(tmp_files):
    wm.save_daily_correlations({'seal': 0.1, 'money': 0.05, 'tech': 0.01},
                               trading_date='20260801')
    wm.save_daily_correlations({'seal': 0.3, 'money': 0.15, 'tech': 0.01},
                               trading_date='20260802')
    new_w, msg = wm.daily_adjust_weights(dict(wm.DEFAULT_WEIGHTS))
    assert new_w is not None
    assert 'ICIR+EMA调权' in msg
    # seal: ic_mean=0.2, ic_std=0.1 → ICIR=2; money: ic_mean=0.1, std=0.05 → ICIR=2
    # total=4; seal target=14, blended=(28+14)/2=21; money target=8.5, blended=(17+8.5)/2=12.75
    assert new_w['seal'] == 21.0
    assert new_w['money'] == 12.8
    assert new_w['tech'] == wm.DEFAULT_WEIGHTS['tech']   # 噪声因子保持当前值
    assert os.path.exists(wm._WEIGHTS_FILE)


def test_daily_adjust_weights_plan_b(tmp_files):
    wm.save_daily_correlations({'seal': 0.1, 'money': 0.05},
                               trading_date='20260801', plan_name='B')
    wm.save_daily_correlations({'seal': 0.3, 'money': 0.15},
                               trading_date='20260802', plan_name='B')
    new_w, msg = wm.daily_adjust_weights(dict(wm.DEFAULT_WEIGHTS), plan_name='B')
    assert new_w is not None
    assert 'ICIR调权' in msg
    assert new_w['seal'] == 14.0
    assert new_w['money'] == pytest.approx(8.5)


def test_save_tab_performance_and_compute(tmp_files):
    wm.save_tab_performance('limit-up', {'win_rate': 55, 'ev': 1.2, 'trade_count': 10, 'cumulative_ret': 5})
    wm.save_tab_performance('trend', {'win_rate': 45, 'ev': 0.5, 'trade_count': 8, 'cumulative_ret': 2})
    wm.save_tab_performance('zhaban', {'win_rate': 30, 'ev': 0.5, 'trade_count': 6, 'cumulative_ret': 1})
    wm.save_tab_performance('dtqiaoban', {'win_rate': 40, 'ev': -0.5, 'trade_count': 4, 'cumulative_ret': -2})
    res = {r['tab']: r for r in wm.compute_tab_weights(force_refresh=True)}
    assert res['limit-up']['weight'] == 1.2
    assert res['limit-up']['label'] == '推荐重仓'
    assert res['trend']['weight'] == 1.0
    assert res['zhaban']['weight'] == 0.8
    assert res['dtqiaoban']['weight'] == 0.5
    assert res['reversal']['weight'] == 0.5
    assert res['reversal']['label'] == '无交易'
    total_alloc = sum(r['allocation_pct'] for r in res.values())
    assert total_alloc == pytest.approx(100, abs=1)


def test_compute_tab_weights_bootstrap(tmp_files, monkeypatch):
    def fake_backtest(tab, max_days=30, top_n=3, use_cache=False, **kw):
        return {'summary': {'trade_count': 6, 'win_rate': 60, 'ev': 1.0,
                            'cumulative_ret': 4}}
    monkeypatch.setattr(backtest_engine, 'run_tab_backtest', fake_backtest)
    res = wm.compute_tab_weights(force_refresh=False)
    assert len(res) == 5
    assert all(r['trades'] >= 6 for r in res)


def test_reversal_weights(tmp_files):
    assert wm.load_reversal_weights() == wm.REV_DEFAULT_WEIGHTS
    wm.save_reversal_weights({'turnover': 30})
    assert wm.load_reversal_weights()['turnover'] == 30
    assert len(wm.load_reversal_weights()) == 5


def test_adjust_reversal_weights_insufficient(tmp_files):
    w, msg = wm.adjust_reversal_weights_from_backtest([{}] * 3)
    assert '数据不足' in msg


def test_adjust_reversal_weights_missing_factors(tmp_files):
    records = [{'net_ret_pct': i} for i in range(5)]
    w, msg = wm.adjust_reversal_weights_from_backtest(records)
    assert '缺少因子分数' in msg


def test_adjust_reversal_weights(tmp_files):
    records = [
        {'net_ret_pct': 1, 'rev_turnover': 1, 'rev_consecutive': 1,
         'rev_pullback': 1, 'rev_sector': 1, 'rev_retention': 1},
        {'net_ret_pct': 2, 'rev_turnover': 2, 'rev_consecutive': 2,
         'rev_pullback': 2, 'rev_sector': 2, 'rev_retention': 2},
        {'net_ret_pct': 3, 'rev_turnover': 3, 'rev_consecutive': 3,
         'rev_pullback': 3, 'rev_sector': 3, 'rev_retention': 3},
        {'net_ret_pct': 4, 'rev_turnover': 4, 'rev_consecutive': 4,
         'rev_pullback': 4, 'rev_sector': 4, 'rev_retention': 4},
        {'net_ret_pct': 5, 'rev_turnover': 5, 'rev_consecutive': 5,
         'rev_pullback': 5, 'rev_sector': 5, 'rev_retention': 5},
    ]
    w, msg = wm.adjust_reversal_weights_from_backtest(records, lr=0.1)
    assert w['turnover'] == pytest.approx(25 + 2.5)
    assert os.path.exists(wm._REV_WEIGHTS_FILE)
    assert os.path.exists(wm._WEIGHT_HISTORY_FILE)


def test_load_tab_weights_defaults(tmp_files):
    assert wm.load_tab_weights('zhaban') == wm.ZB_DEFAULT_WEIGHTS
    assert wm.load_tab_weights('dtqiaoban') == wm.DT_DEFAULT_WEIGHTS
    assert wm.load_tab_weights('reversal') == wm.REV_DEFAULT_WEIGHTS
    assert wm.load_tab_weights('unknown') == {}


def test_load_tab_weights_dispatch(tmp_files, monkeypatch):
    monkeypatch.setattr(wm, 'load_weights', lambda: {'seal': 1.0})
    monkeypatch.setattr(wm, 'load_trend_weights', lambda: {'chg': 1.0})
    assert wm.load_tab_weights('limit-up') == {'seal': 1.0}
    assert wm.load_tab_weights('trend') == {'chg': 1.0}


def test_adjust_tab_weights_from_backtest(tmp_files):
    records = [
        {'net_ret_pct': 1, 'zb_seal': 1, 'zb_money': 2},
        {'net_ret_pct': 2, 'zb_seal': 2, 'zb_money': 4},
        {'net_ret_pct': 3, 'zb_seal': 3, 'zb_money': 6},
        {'net_ret_pct': 4, 'zb_seal': 4, 'zb_money': 8},
        {'net_ret_pct': 5, 'zb_seal': 5, 'zb_money': 10},
    ]
    w, _ = wm.adjust_tab_weights_from_backtest('zhaban', records, lr=0.1)
    assert w['seal'] == pytest.approx(25 + 0.1 * 25)   # corr=1 → +2.5
    assert os.path.exists(wm._WEIGHTS_FILES['zhaban'])


def test_trend_weights(tmp_files):
    assert wm.load_trend_weights() == wm.TREND_DEFAULT_WEIGHTS
    wm.save_trend_weights({'chg': 10})
    assert wm.load_trend_weights()['chg'] == 10
    summary = wm.get_trend_weight_summary()
    assert summary['total'] == sum(wm.load_trend_weights().values())
    assert summary['factors'][0]['key'] == 'chg'


def test_adjust_trend_weights_from_backtest(tmp_files):
    records = [
        {'net_ret_pct': 1, 'trend_chg': 1, 'trend_turnover': 1, 'trend_amount': 1},
        {'net_ret_pct': 2, 'trend_chg': 2, 'trend_turnover': 2, 'trend_amount': 2},
        {'net_ret_pct': 3, 'trend_chg': 3, 'trend_turnover': 3, 'trend_amount': 3},
        {'net_ret_pct': 4, 'trend_chg': 4, 'trend_turnover': 4, 'trend_amount': 4},
        {'net_ret_pct': 5, 'trend_chg': 5, 'trend_turnover': 5, 'trend_amount': 5},
    ]
    w, msg = wm.adjust_trend_weights_from_backtest(records, lr=0.1)
    assert w['chg'] == pytest.approx(wm.TREND_DEFAULT_WEIGHTS['chg'] + 0.1 * 5)
    assert '↑' in msg
    w2, msg2 = wm.adjust_trend_weights_from_backtest([{}] * 3)
    assert '数据不足' in msg2


def test_weight_history(tmp_files):
    assert wm.get_weight_history('trend') == []
    wm.save_weight_history('trend', 'turnover', 30, 32, 0.5)
    wm.save_weight_history('zhaban', 'seal', 25, 27, 0.4)
    hist = wm.get_weight_history('trend')
    assert len(hist) == 1
    assert hist[0]['factor'] == 'turnover'
    assert hist[0]['delta'] == 2.0
    assert hist[0]['arrow'] == '↑'
    assert wm.get_weight_history('zhaban', days=30)[0]['tab'] == 'zhaban'
    # 过期数据被过滤
    with open(wm._WEIGHT_HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write('{"date":"2000-01-01","tab":"trend","factor":"x","old":1,"new":2,"delta":1,"corr":0,"arrow":"↑"}\n')
    assert all(e['date'] >= '2026-07-02' for e in wm.get_weight_history('trend'))


def test_get_tab_weight_summary(tmp_files, monkeypatch):
    monkeypatch.setattr(wm, 'load_weights', lambda: dict(wm.DEFAULT_WEIGHTS))
    s = wm.get_tab_weight_summary('limit-up')
    assert s['factors'] and s['total'] > 0
    s2 = wm.get_tab_weight_summary('trend')
    assert s2['factors']
    monkeypatch.setattr(wm, 'compute_tab_weights', lambda: [])
    s3 = wm.get_tab_weight_summary('sector')
    assert '暂无数据' in s3['error']
    s4 = wm.get_tab_weight_summary('nope')
    assert s4['error']
