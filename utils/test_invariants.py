#!/usr/bin/env python3
"""系统不变性验证 (pytest 版) — 原 check()/section() 脚本已统一为 pytest。

覆盖: BUG-1~10 回归 / 缓存 / 权重 / 封板时间 / 真实数据扫描 / 日期逻辑 /
市场状态 / 多 Tab 回测 / 持久化安全 / 代码质量 / 核心函数可调用性 / 回测正确性。

运行方式:
    python -m pytest utils/test_invariants.py          # 默认 (联网项自动跳过)
    python -m pytest utils/test_invariants.py -m network  # 含真实行情数据
    python utils/test_invariants.py                    # 兼容旧命令 (等价全量)
"""
import inspect
import os
import pickle
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

# 项目根目录 (脚本在 utils/ 子目录)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import app  # noqa: E402  (导入即校验 app 可加载)
import core.cache as cache  # noqa: E402
from core.scanner_utils import (  # noqa: E402
    seal_time_score, _vectorized_seal_time_score,
)
import backtest.backtest_engine as be  # noqa: E402
import scoring.score_new as sn  # noqa: E402
import scoring.weight_manager as wm  # noqa: E402
import scoring.weight_scheduler as ws  # noqa: E402

_CST = timezone(timedelta(hours=8))

# ── 静态源码缓存 (各测试类共用, 只读一次) ──
with open(os.path.join(_PROJECT_ROOT, 'app.py'), 'r', encoding='utf-8') as _f:
    APP_SRC = _f.read()
WM_SRC = inspect.getsource(wm)
WS_SRC = inspect.getsource(ws)
BE_SRC = inspect.getsource(be)
SN_SRC = inspect.getsource(sn)


# ═══════════════════════════════════════════════════════════════════════
#  A. BUG 回归测试 (v1.25.0)
# ═══════════════════════════════════════════════════════════════════════
class TestBugRegressions:
    def test_bug1_build_trend_items_vol_col(self):
        """BUG-1: _build_trend_items 中 vol_col 不再 NameError (静态检查)。"""
        build_start = APP_SRC.find('def _build_trend_items(')
        assert build_start > 0, "未找到 _build_trend_items 函数定义"
        body_start = APP_SRC.find(':', build_start)
        body = APP_SRC[body_start:body_start + 800]
        assert ("vol_col = cols.get('vol')" in body
                or 'vol_col = cols.get("vol")' in body), \
            "_build_trend_items 函数体内应有 vol_col = cols.get('vol') 赋值"

    def test_bug2_signals_today_route_once(self):
        count = (APP_SRC.count("@app.get('/api/signals/today')")
                 + APP_SRC.count('@app.get("/api/signals/today")'))
        assert count == 1, f"/api/signals/today 路由应恰好出现 1 次, 实际 {count} 次"

    def test_bug3_rev_weights_single_definition(self):
        rev_assigns = re.findall(r'REV_DEFAULT_WEIGHTS\s*=\s*\{[^}]+\}', WM_SRC)
        assert len(rev_assigns) == 1, \
            f"REV_DEFAULT_WEIGHTS 应恰好定义 1 次, 实际 {len(rev_assigns)} 次"

    def test_bug3_rev_weights_has_retention(self):
        assert 'retention' in wm.REV_DEFAULT_WEIGHTS, \
            f"REV_DEFAULT_WEIGHTS 应含 retention 键: {list(wm.REV_DEFAULT_WEIGHTS)}"

    def test_bug3_rev_weights_five_factors(self):
        assert len(wm.REV_DEFAULT_WEIGHTS) == 5, \
            f"REV_DEFAULT_WEIGHTS 应有 5 个因子: {list(wm.REV_DEFAULT_WEIGHTS)}"

    def test_bug4_scheduler_capital_30000(self):
        assert 'capital=30000' in WS_SRC, "weight_scheduler 应使用 capital=30000"
        assert 'capital=20000' not in WS_SRC, "weight_scheduler 不应再使用 capital=20000"

    def test_bug5_sector_stream_uses_log1p(self):
        stream_idx = APP_SRC.find('def api_sector_stream(')
        assert stream_idx >= 0, "未找到 api_sector_stream 定义"
        next_func_idx = APP_SRC.find('\n\n', stream_idx + 100)
        if next_func_idx < 0:
            next_func_idx = stream_idx + 3000
        code = APP_SRC[stream_idx:next_func_idx]
        assert 'log1p' in code, "api_sector_stream 应使用 log1p 公式"
        assert ('eff_bonus' in code or 'zb_penalty' in code), \
            "api_sector_stream 应包含 eff_bonus/zb_penalty 罚分因子"

    def test_bug6_seal_time_scalar_max(self):
        assert seal_time_score('093000') <= 10, \
            f"seal_time_score 标量版最大值应 ≤10, 实际 {seal_time_score('093000')}"

    def test_bug6_vectorized_range(self):
        vec = _vectorized_seal_time_score(pd.Series(['093000', '100000', '143000']))
        assert vec.max() <= 10, f"向量化版最大值应 ≤10, 实际 {vec.max()}"
        assert vec.min() >= 0, f"向量化版最小值应 ≥0, 实际 {vec.min()}"

    def test_bug6_scalar_vector_consistent(self):
        scalar = seal_time_score('093000')
        vec = _vectorized_seal_time_score(pd.Series(['093000']))[0]
        assert abs(scalar - vec) < 0.01, \
            f"标量/向量化对 093000 应一致, 标量={scalar}, 向量化={vec}"

    def test_bug8_score_new_no_bare_except(self):
        bare = re.findall(r'^\s*except\s*:', SN_SRC, re.MULTILINE)
        assert len(bare) == 0, f"score_new.py 不应有 bare except, 发现 {len(bare)} 处"

    def test_bug9_no_hardcoded_windows_path(self):
        assert 'C:\\\\Users\\\\16689' not in BE_SRC and 'r"C:\\Users' not in BE_SRC, \
            "backtest_engine 不应硬编码 C:\\Users 路径"

    def test_bug9_dynamic_project_root(self):
        assert '_PROJECT_ROOT = os.path.abspath' in BE_SRC, \
            "backtest_engine 应使用 _PROJECT_ROOT = os.path.abspath 动态路径"

    def test_bug10_app_no_bare_except(self):
        bare = re.findall(r'^\s*except\s*:', APP_SRC, re.MULTILINE)
        assert len(bare) == 0, f"app.py 不应有 bare except, 发现 {len(bare)} 处"


# ═══════════════════════════════════════════════════════════════════════
#  1. cache 模块
# ═══════════════════════════════════════════════════════════════════════
class TestCacheModule:
    def test_real_trading_calendar_size(self):
        cal = cache._load_trading_calendar()
        assert len(cal) > 5000, f"交易日历应有>5000天, 实际{len(cal)}"

    def test_real_trading_calendar_known_dates(self):
        cal = cache._load_trading_calendar()
        assert '20260529' in cal, '2026-05-29 应在交易日历中'
        assert '20260531' not in cal, '2026-05-31(周日)不应在交易日历中'
        assert cache._is_trading_day('20260529'), '_is_trading_day 应返回 True'

    def test_market_frozen_time_consistency(self, monkeypatch):
        today_str = datetime.now(_CST).strftime('%Y%m%d')
        monkeypatch.setattr(cache, '_load_trading_calendar', lambda: {today_str})
        now = datetime.now(_CST)
        frozen = cache._is_market_frozen()
        if now.weekday() >= 5 or now.hour >= 15:
            assert frozen, "周末/盘后应冻结缓存"
        else:
            assert not frozen, "盘中应不冻结"

    def test_trading_date_format(self, monkeypatch):
        today_str = datetime.now(_CST).strftime('%Y%m%d')
        monkeypatch.setattr(cache, '_load_trading_calendar', lambda: {today_str})
        td = cache._trading_date()
        assert td is not None and len(td) == 10, \
            f"trading_date 应返回 YYYY-MM-DD, 实际: {td}"

    def test_daily_set_get_force(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache, '_CACHE_DIR', str(tmp_path))
        monkeypatch.setattr(cache, '_is_market_frozen', lambda: True)
        monkeypatch.setattr(cache, '_trading_date', lambda: '2026-08-01')
        data = {'test': True, 'val': 42}
        cache.daily_set('_test_invariant', data, force=True)
        retrieved = cache.daily_get('_test_invariant')
        assert retrieved is not None, "daily_set 后 daily_get 应非空"
        assert retrieved.get('val') == 42, "缓存数据应一致"


# ═══════════════════════════════════════════════════════════════════════
#  2. weight_manager 模块
# ═══════════════════════════════════════════════════════════════════════
class TestWeightManager:
    def test_default_weights_structure(self):
        w = wm.DEFAULT_WEIGHTS
        assert isinstance(w, dict)
        assert w['seal'] == 28.0, f"seal 默认权重应为 28.0, 实际 {w['seal']}"
        assert w['money'] == 17.0, f"money 默认权重应为 17.0, 实际 {w['money']}"
        assert 'community' not in w, "DEFAULT_WEIGHTS 不应含 community"
        assert 'principal_score' in w
        assert w.get('principal_score', 0) >= 8.0
        assert 'buyability' in w
        assert 'sector' in w
        assert 'north_flow' in w
        assert 'alpha' in w
        assert 'crash_resistance' in w

    def test_active_factors_nonnegative(self):
        w = wm.DEFAULT_WEIGHTS
        active = ['seal', 'tech', 'sector', 'history', 'money', 'stock_sentiment',
                  'principal_score', 'north_flow', 'alpha', 'crash_resistance']
        active_sum = sum(w[k] for k in active)
        assert active_sum > 0, f"活跃因子加权和应 >0, 实际 {active_sum}"
        assert all(w[k] >= 0 for k in active), "所有活跃因子权重应 >=0"
        assert w.get('buyability', -1) == 0.0, \
            f"buyability应=0(已退场), 实际 {w.get('buyability', -1)}"

    def test_apply_weights_scale(self):
        idx = pd.Index([0, 1])
        zeros = pd.Series(0.0, index=idx)
        ones = pd.Series(5.0, index=idx)
        maxes_seal = pd.Series([28.0, 28.0], index=idx)
        result = wm.apply_weights(maxes_seal, zeros, ones, zeros, zeros, ones,
                                  stock_sentiment_scores=ones,
                                  principal_scores=ones,
                                  weights=wm.DEFAULT_WEIGHTS)
        assert result.max() <= 100, f"满分时应 <=100, 实际 {result.max():.1f}"
        assert result.min() >= 0, f"零分时应 >=0, 实际 {result.min():.1f}"
        assert len(result) == 2, f"应返回 2 只股票, 实际 {len(result)}"

    def test_reversal_weights_atomic_write(self, monkeypatch, tmp_path):
        rev_file = str(tmp_path / 'rev.json')
        monkeypatch.setattr(wm, '_REV_WEIGHTS_FILE', rev_file)
        wm.save_reversal_weights({'turnover': 25, 'consecutive': 30, 'pullback': 25,
                                  'sector': 15, 'retention': 5})
        leftover = [f for f in os.listdir(str(tmp_path)) if f.endswith('.tmp')]
        assert len(leftover) == 0, f"save_reversal_weights 原子写不应留 .tmp: {leftover}"
        loaded = wm.load_reversal_weights()
        assert loaded.get('turnover') == 25, \
            f"save 后 load 应拿到新值, 实际 turnover={loaded.get('turnover')}"


# ═══════════════════════════════════════════════════════════════════════
#  3. seal_time_score 范围一致性
# ═══════════════════════════════════════════════════════════════════════
class TestSealTimeScoreRange:
    @pytest.mark.parametrize('t', ['093000', '103000', '113000', '133000', '143000'])
    def test_scalar_in_range(self, t):
        assert 0 <= seal_time_score(t) <= 10, f"{t} → {seal_time_score(t)}"

    def test_scalar_early_beats_late(self):
        assert seal_time_score('093000') > seal_time_score('143000'), \
            "早盘封板分应 > 尾盘封板分"

    def test_scalar_invalid_input(self):
        assert 0 <= seal_time_score('INVALID') <= 10, \
            f"无效输入应返回中位分, 实际 {seal_time_score('INVALID')}"

    def test_vectorized_range_and_order(self):
        vec = _vectorized_seal_time_score(
            pd.Series(['093000', '103000', '113000', '133000', '143000', 'INVALID']))
        assert vec.max() <= 10, f"向量化版最大值应 ≤10, 实际 {vec.max()}"
        assert vec.min() >= 0, f"向量化版最小值应 ≥0, 实际 {vec.min()}"
        assert vec.iloc[0] > vec.iloc[4], "向量化版: 早盘(0930)应 > 尾盘(1430)"


# ═══════════════════════════════════════════════════════════════════════
#  4. scanner 评分函数 (网络依赖)
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.network
class TestScannerScoringNetwork:
    def test_fetch_limit_up_pool_scores(self):
        import scanner
        pool = scanner.fetch_limit_up_pool()
        if pool is None or pool.empty:
            pytest.skip('涨停池为空 — 可能非交易时间')
        assert len(pool) > 0, f"涨停池应非空, 实际 {len(pool)} 只"
        filtered = scanner.pre_filter(pool)
        if filtered.empty:
            pytest.skip('前置过滤后为空')
        seal_s = scanner.score_seal_strength(filtered)
        assert seal_s.max() <= 28, f"封板强度应 <=28, 实际 {seal_s.max():.1f}"
        assert seal_s.min() >= 0, f"封板强度应 >=0, 实际 {seal_s.min():.1f}"
        tech_s = scanner.score_tech_form(filtered)
        assert tech_s.max() <= 10, f"量价应 <=10, 实际 {tech_s.max():.1f}"
        sector_mom = scanner.get_sector_heat_scores(filtered)
        assert sector_mom.max() <= 12, f"板块热度应 <=12, 实际 {sector_mom.max():.1f}"
        sector_res = scanner.get_sector_resonance(filtered)
        assert sector_res.max() <= 8, f"板块共振应 <=8, 实际 {sector_res.max():.1f}"
        principal_s = scanner.score_by_principal(filtered, 30000)
        assert principal_s.max() <= 10, f"本金评分应 <=10, 实际 {principal_s.max():.1f}"


# ═══════════════════════════════════════════════════════════════════════
#  5. 完整扫描管道 (网络依赖)
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.network
class TestFullScanPipeline:
    def test_scan_limit_up_data(self):
        from app import _scan_limit_up_data, _make_cache_entry, _today_trading
        today_str = _today_trading()
        data = _scan_limit_up_data(today_str, principal=30000)
        if data is None:
            pytest.skip('扫描返回 None — 可能非交易时间')
        stocks = data.get('stocks', [])
        assert len(stocks) > 0, f"TOP_N 应非空, 实际 {len(stocks)}"
        s = stocks[0]
        assert 0 <= s['total_score'] <= 100, f"总分应在 0-100, 实际 {s['total_score']}"
        assert 0 <= s.get('seal_score', -1) <= 28, "seal_score 应在 0-28"
        assert 0 <= s.get('money_score', -1) <= 20, "money_score 应在 0-20"
        assert 0 <= s.get('tech_score', -1) <= 10, "tech_score 应在 0-10"
        assert 'community_score' not in s, "不应含 community_score"
        assert 'principal_score' in s, "应含 principal_score"
        sentiment = data.get('sentiment_level', '')
        assert sentiment != '未知' and sentiment != '', f"情绪应非空: {sentiment}"
        required = ['stocks', 'seal_scores', 'money_scores', 'sector_mom',
                    'sector_res', 'tech_scores', 'history_scores', 'sentiment_score']
        for r in required:
            assert r in data, f"返回 dict 应含 '{r}'"
        cache_entry = _make_cache_entry(data['stocks'], data['sentiment_score'],
                                        data['sentiment_level'], data['date'])
        assert cache_entry.get('ok'), "缓存条目 ok=True"
        assert len(cache_entry['stocks']) == len(stocks), "缓存 stocks 数量一致"


# ═══════════════════════════════════════════════════════════════════════
#  6. 日期逻辑
# ═══════════════════════════════════════════════════════════════════════
class TestDateLogic:
    def test_today_trading_format(self, monkeypatch):
        from app import _today_trading
        today_str = datetime.now(_CST).strftime('%Y%m%d')
        monkeypatch.setattr(cache, '_load_trading_calendar', lambda: {today_str})
        t = _today_trading()
        assert len(t) == 8, f"_today_trading 应返回 8 位 YYYYMMDD, 实际 {t}"
        assert t == _today_trading(), "_today_trading 两次调用应一致"


@pytest.mark.network
class TestMarketSentimentNetwork:
    def test_detect_market_sentiment(self):
        import scanner
        from app import _today_trading
        sent = scanner.detect_market_sentiment(_today_trading())
        assert sent is not None, "detect_market_sentiment 不应返回 None"
        score, level, _details = sent
        assert 0 <= score <= 10, f"情绪分应在 0-10, 实际 {score}"
        assert any(kw in level for kw in ['冰点', '低迷', '正常', '活跃', '高潮', '未知']), \
            f"情绪等级应含有效关键词, 实际: {level}"


# ═══════════════════════════════════════════════════════════════════════
#  7. 市场状态检测 + 趋势扫描
# ═══════════════════════════════════════════════════════════════════════
class TestMarketStatus:
    def test_market_status_valid(self, monkeypatch):
        import scanner
        today_str = datetime.now(_CST).strftime('%Y%m%d')
        monkeypatch.setattr(cache, '_load_trading_calendar', lambda: {today_str})
        status = scanner.get_market_status()
        assert status in ('trading', 'lunch', 'closed', 'weekend', 'holiday'), \
            f"市场状态应有效, 实际: {status}"

    def test_default_mode_matches_status(self, monkeypatch):
        import scanner
        today_str = datetime.now(_CST).strftime('%Y%m%d')
        monkeypatch.setattr(cache, '_load_trading_calendar', lambda: {today_str})
        status = scanner.get_market_status()
        mode = scanner.get_default_mode()
        assert mode in ('trend', 'after_hours'), f"默认模式应有效, 实际: {mode}"
        if status == 'trading':
            assert mode == 'trend', f"盘中默认应为 trend, 实际: {mode}"
        else:
            assert mode == 'after_hours', f"非盘中默认应为 after_hours, 实际: {mode}"


@pytest.mark.network
class TestTrendScanNetwork:
    def test_scan_trend_no_exception(self):
        """scan_trend FAIL-FAST: 异常 = 真 BUG。"""
        import scanner
        from datetime import date
        scanner.scan_trend(date.today().strftime('%Y%m%d'), _table_mode=False, top_n=5)

    def test_fetch_limit_up_pool_dated(self):
        import scanner
        from datetime import date
        pool = scanner.fetch_limit_up_pool(date_str=date.today().strftime('%Y%m%d'))
        assert pool is not None, "fetch_limit_up_pool(date_str=...) 应非 None"


# ═══════════════════════════════════════════════════════════════════════
#  8. 多 Tab 回测引擎
# ═══════════════════════════════════════════════════════════════════════
class TestBacktestEngineTabs:
    @staticmethod
    def _expected_tabs():
        return {'limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal', 'sector'}

    def test_all_tabs_constant(self):
        from backtest_engine import ALL_TABS
        expected = self._expected_tabs()
        assert set(ALL_TABS) == expected, \
            f"ALL_TABS 应含 6 个 tab, 实际 {ALL_TABS}"

    def test_pool_fetchers_cover_tabs(self):
        from backtest_engine import SIGNAL_POOL_FETCHERS
        assert set(SIGNAL_POOL_FETCHERS.keys()) == self._expected_tabs()

    def test_score_funcs_cover_tabs(self):
        from backtest_engine import SCORE_FUNCS
        assert set(SCORE_FUNCS.keys()) == self._expected_tabs()

    def test_score_columns_cover_tabs(self):
        from backtest_engine import SCORE_COLUMNS
        assert set(SCORE_COLUMNS.keys()) == self._expected_tabs()

    def test_no_pending_tabs(self):
        from backtest_engine import _PENDING_TABS
        assert len(_PENDING_TABS) == 0, f"全部 tab 应已实现, PENDING_TABS={_PENDING_TABS}"

    def test_sector_self_fetching(self):
        from backtest_engine import _SELF_FETCHING_TABS
        assert 'sector' in _SELF_FETCHING_TABS, "sector tab 应该是 SELF_FETCHING"

    def test_unknown_tab_returns_error(self):
        from backtest_engine import run_tab_backtest
        bad = run_tab_backtest(tab='unknown-tab', max_days=5, top_n=3,
                               capital=10000, use_cache=False)
        assert 'error' in bad and bad.get('error', '').startswith('未知 tab'), \
            f"未知 tab 应返回 error 字段, 实际: {bad}"


# ═══════════════════════════════════════════════════════════════════════
#  9. 持久化与缓存安全
# ═══════════════════════════════════════════════════════════════════════
class TestPersistenceSafety:
    def test_save_weights_atomic(self, monkeypatch, tmp_path):
        weights_file = str(tmp_path / 'weights.json')
        monkeypatch.setattr(wm, '_WEIGHTS_FILE', weights_file)
        wm.save_weights({'seal': 50, 'money': 50, 'sector': 0, 'tech': 0,
                         'history': 0, 'stock_sentiment': 0, 'principal_score': 0})
        leftover = [f for f in os.listdir(str(tmp_path)) if f.endswith('.tmp')]
        assert len(leftover) == 0, f"save_weights 原子写不应留 .tmp: {leftover}"
        loaded = wm.load_weights()
        assert loaded.get('seal') == 50, \
            f"save_weights 后 load_weights 应拿到 seal=50, 实际 {loaded.get('seal')}"

    def test_save_trend_weights_atomic(self, monkeypatch, tmp_path):
        trend_file = str(tmp_path / 'trend.json')
        monkeypatch.setattr(wm, '_TREND_WEIGHTS_FILE', trend_file)
        wm.save_trend_weights({'tech_break': 0.3, 'volume': 0.3, 'ma_align': 0.4})
        leftover = [f for f in os.listdir(str(tmp_path)) if f.endswith('.tmp')]
        assert len(leftover) == 0, f"save_trend_weights 原子写不应留 .tmp: {leftover}"

    def test_daily_get_pkl_cross_day_invalidates(self, monkeypatch, tmp_path):
        cache_dir = str(tmp_path)
        monkeypatch.setattr(cache, '_CACHE_DIR', cache_dir)
        monkeypatch.setattr(cache, '_trading_date', lambda: '2026-08-01')
        old_time = time.time() - 5 * 86400
        test_file = os.path.join(
            cache_dir, f"daily_2026-08-01_audit9_v{cache._CACHE_VER}.pkl")
        with open(test_file, 'wb') as f:
            pickle.dump({'old': 'data'}, f)
        os.utime(test_file, (old_time, old_time))
        result = cache.daily_get_pkl('audit9')
        assert result is None, f"daily_get_pkl 跨日应返 None, 实际: {result}"
        assert not os.path.exists(test_file), "daily_get_pkl 跨日应删旧文件"

    def test_clear_all_removes_old_versions(self, monkeypatch, tmp_path):
        cache_dir = str(tmp_path)
        monkeypatch.setattr(cache, '_CACHE_DIR', cache_dir)
        old_json = os.path.join(
            cache_dir, f"daily_20260101_oldkey_v{cache._CACHE_VER - 1}.json")
        new_json = os.path.join(
            cache_dir, f"daily_20260101_newkey_v{cache._CACHE_VER}.json")
        old_pkl = os.path.join(cache_dir, f"somecache_v{cache._CACHE_VER - 1}.pkl")
        for p in (old_json, new_json, old_pkl):
            with open(p, 'wb') as f:
                pickle.dump({}, f)
        cache.clear_all()
        assert not os.path.exists(old_json), "clear_all 应删旧版 .json"
        assert os.path.exists(new_json), "clear_all 应保留当前版 .json"
        assert not os.path.exists(old_pkl), "clear_all 应删旧版 .pkl"


# ═══════════════════════════════════════════════════════════════════════
#  10. 代码质量不变性
# ═══════════════════════════════════════════════════════════════════════
class TestCodeQualityInvariants:
    def test_interaction_bonus_within_limit(self):
        assert sn.INTERACTION_BONUS <= 5, \
            f"INTERACTION_BONUS 应 <=5, 实际 {sn.INTERACTION_BONUS}"

    def test_buyability_zero(self):
        assert wm.DEFAULT_WEIGHTS.get('buyability', -1) == 0.0, "buyability 应=0(已退场)"

    def test_default_weights_nonnegative(self):
        assert all(v >= 0 for v in wm.DEFAULT_WEIGHTS.values()), \
            "DEFAULT_WEIGHTS 所有值应 >=0"

    def test_rev_weights_nonnegative(self):
        assert all(v >= 0 for v in wm.REV_DEFAULT_WEIGHTS.values()), \
            "REV_DEFAULT_WEIGHTS 所有值应 >=0"


# ═══════════════════════════════════════════════════════════════════════
#  11. 核心函数可调用性 (无网络)
# ═══════════════════════════════════════════════════════════════════════
class TestCoreCallability:
    def test_scanner_filters_callable(self):
        import scanner_filters as sf
        assert callable(sf.filter_non_main_board)
        assert callable(sf.filter_xr_xd_dr)
        assert callable(sf.pre_filter)
        assert callable(sf.filter_by_price)

    def test_ak_utils_callable(self):
        import ak_utils as ak_u
        assert callable(ak_u.safe_ak)

    def test_scanner_scoring_callable(self):
        from scoring.scanner_scoring import (
            score_zhaban_data, score_dtqiaoban_data, score_sector_data,
        )
        assert callable(score_zhaban_data)
        assert callable(score_dtqiaoban_data)
        assert callable(score_sector_data)

    def test_score_new_callable(self):
        assert callable(sn.score_new)

    def test_seal_time_range(self):
        assert 0 <= seal_time_score('100000') <= 10, "seal_time_score(10:00) 应在 0-10"


# ═══════════════════════════════════════════════════════════════════════
#  12. 回测正确性回归 (2026-08-01)
# ═══════════════════════════════════════════════════════════════════════
class TestBacktestCorrectness:
    @staticmethod
    def _allwin():
        from backtest_engine import _aggregate
        return _aggregate([{'net_ret_pct': 2.0, 'pnl': 100},
                           {'net_ret_pct': 3.0, 'pnl': 150}])

    def test_ev_all_win(self):
        r = self._allwin()
        assert abs(r['ev'] - 2.5) < 1e-6, f"全赢样本 EV 应=2.5, 实际 {r['ev']}"

    def test_compound_ret(self):
        r = self._allwin()
        assert abs(r['cumulative_ret'] - 5.06) < 0.01, \
            f"复利累计应≈5.06, 实际 {r['cumulative_ret']}"

    def test_deterministic_fill_reproducible(self):
        from backtest_engine import _deterministic_fill
        assert (_deterministic_fill('000001', '20260701', 0.5)
                == _deterministic_fill('000001', '20260701', 0.5)), "确定性成交应可复现"

    def test_deterministic_fill_boundaries(self):
        from backtest_engine import _deterministic_fill
        assert _deterministic_fill('000001', '20260701', 1.0) is True, "prob=1.0 应恒成交"
        assert _deterministic_fill('000001', '20260701', 0.0) is False, "prob=0.0 应恒不成交"

    def test_historical_ohlcv_spot_disabled(self):
        from backtest_engine import _get_daily_ohlcv_batch
        assert _get_daily_ohlcv_batch('20250101') == {}, \
            "历史日期 OHLCV 批量应返回空(禁止 spot 快照污染)"
        from backtest_engine import _SPOT_DISABLED
        assert _SPOT_DISABLED is True, "spot 批量默认应禁用"

    def test_strict_ohlcv_default(self):
        from backtest_engine import _BACKTEST_STRICT_OHLCV
        assert _BACKTEST_STRICT_OHLCV is True, "strict_ohlcv 默认应开启(构造价跳过)"

    def test_score_new_weights_sum_100(self):
        from scoring.score_new import load_factor_weights
        w0 = load_factor_weights()
        assert abs(sum(w0.values()) - 100) < 1e-6, \
            f"score_new 默认权重和应=100, 实际 {sum(w0.values())}"

    def test_score_new_weight_keys(self):
        from scoring.score_new import load_factor_weights, FACTOR_WEIGHTS
        assert set(load_factor_weights().keys()) == set(FACTOR_WEIGHTS.keys()), \
            "score_new 权重键应与 FACTOR_WEIGHTS 一致"

    def test_load_tab_weights_trend(self):
        from weight_manager import load_tab_weights
        wt = load_tab_weights('trend')
        assert isinstance(wt, dict) and len(wt) >= 5, \
            f"load_tab_weights('trend') 应返回趋势权重: {wt}"

    def test_load_tab_weights_limit_up(self):
        from weight_manager import load_tab_weights
        assert 'seal' in load_tab_weights('limit-up'), \
            "load_tab_weights('limit-up') 应含 seal"

    def test_run_tab_backtest_strict_param(self):
        from backtest_engine import run_tab_backtest as rtb
        assert 'strict_ohlcv' in inspect.signature(rtb).parameters, \
            "run_tab_backtest 应支持 strict_ohlcv 参数"

    def test_engine_submodules_exist(self):
        for sub in ('backtest_tabs.py', 'backtest_metrics.py', 'backtest_pools.py',
                    'backtest_ohlcv.py', 'backtest_scores.py'):
            assert os.path.exists(os.path.join(_PROJECT_ROOT, 'backtest', sub)), \
                f"{sub} 应存在(2026-08-01 拆分)"


if __name__ == '__main__':
    # 兼容旧命令: python utils/test_invariants.py → 全量运行 (含联网项)
    sys.exit(pytest.main([__file__, '-m', 'network or not network']))
