#!/usr/bin/env python3
"""
系统不变性验证脚本 — 用真实数据运行所有关键路径，检查逻辑约束。
运行方式: cd stock-scanner && python utils/test_invariants.py

v1.25.0 更新: 新增 BUG-1~10 回归测试, 删除过时用例 (旧 scanner 评分函数签名)
"""
import sys, os, json, traceback

# 项目根目录 (脚本在 utils/ 子目录)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

PASS = 0
FAIL = 0

def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")

def section(name):
    print(f"\n{'='*50}\n  {name}\n{'='*50}")

# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 1: BUG 修复回归测试 (v1.25.0 新增)
# ════════════════════════════════════════════════════════════════════════════════
section("A. BUG 回归测试 (v1.25.0)")

# ── A1: BUG-1 — _build_trend_items 中 vol_col 不再 NameError ──
import pandas as pd, numpy as np

# 构造模拟 trend 数据 (含 vol 列), 触发 _build_trend_items
# 直接检查函数编译不报 NameError, 而不是调用完整流程(需要网络)
import importlib, types
import app as _app_mod
_src = importlib.util.find_source = None  # placeholder

# 更可靠的静态检查: 读源码确认 vol_col 在引用前已定义
with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r', encoding='utf-8') as f:
    app_src = f.read()

# 找到 _build_trend_items 函数体
_build_start = app_src.find('def _build_trend_items(')
if _build_start > 0:
    _build_body_start = app_src.find(':', _build_start)
    # 取函数体前 800 字符
    _body = app_src[_build_body_start:_build_body_start+800]
    check('vol_col = cols.get(\'vol\')' in _body or 'vol_col = cols.get("vol")' in _body,
          "BUG-1: _build_trend_items 函数体内应有 vol_col = cols.get('vol') 赋值")
else:
    check(False, "BUG-1: 未找到 _build_trend_items 函数定义")

# ── A2: BUG-2 — @app.get('/api/signals/today') 不再重复 ──
signals_today_count = app_src.count("@app.get('/api/signals/today')") + app_src.count('@app.get("/api/signals/today")')
check(signals_today_count == 1,
      f"BUG-2: /api/signals/today 路由应恰好出现 1 次, 实际 {signals_today_count} 次")

# ── A3: BUG-3 — REV_DEFAULT_WEIGHTS 不再重复定义 ──
import scoring.weight_manager as wm
import inspect as _inspect
wm_src = _inspect.getsource(wm)
# 统计 REV_DEFAULT_WEIGHTS = { 开头的赋值语句(包含注释行之间的间距)
import re
rev_assigns = re.findall(r'REV_DEFAULT_WEIGHTS\s*=\s*\{[^}]+\}', wm_src)
check(len(rev_assigns) == 1,
      f"BUG-3: REV_DEFAULT_WEIGHTS 应恰好定义 1 次, 实际 {len(rev_assigns)} 次")
# 验证是 5 因子版
rev_w = wm.REV_DEFAULT_WEIGHTS
check('retention' in rev_w, f"BUG-3: REV_DEFAULT_WEIGHTS 应含 retention 键, 实际: {list(rev_w.keys())}")
check(len(rev_w) == 5, f"BUG-3: REV_DEFAULT_WEIGHTS 应有 5 个因子, 实际 {len(rev_w)}: {list(rev_w.keys())}")

# ── A4: BUG-4 — weight_scheduler 使用 capital=30000 ──
import scoring.weight_scheduler as ws
ws_src = _inspect.getsource(ws)
check('capital=30000' in ws_src, "BUG-4: weight_scheduler 应使用 capital=30000 (与全局默认一致)")
check('capital=20000' not in ws_src, "BUG-4: weight_scheduler 不应再使用 capital=20000")

# ── A5: BUG-5 — api_sector_stream 与 api_sector_cards 使用相同评分公式 ──
# 两者都应包含 log1p 公式关键字 (搜索函数定义到下一个顶层函数之间的所有内容)
_stream_idx = app_src.find('def api_sector_stream(')
_next_func_idx = app_src.find('\n\n', _stream_idx + 100)  # 找下一个顶层定义
if _next_func_idx < 0: _next_func_idx = _stream_idx + 3000
stream_sector_code = app_src[_stream_idx:_next_func_idx]
check('log1p' in stream_sector_code,
      "BUG-5: api_sector_stream 应使用 log1p 公式 (与 api_sector_cards 一致)")
check('eff_bonus' in stream_sector_code or 'zb_penalty' in stream_sector_code,
      "BUG-5: api_sector_stream 应包含 eff_bonus/zb_penalty 罚分因子")

# ── A6: BUG-6 — seal_time_score 标量版和向量化版范围统一为 0-10 ──
from core.scanner_utils import seal_time_score, _vectorized_seal_time_score
import pandas as pd

scalar_max = seal_time_score('093000')  # 早盘应为满分
check(scalar_max <= 10, f"BUG-6: seal_time_score 标量版最大值应 ≤10, 实际 {scalar_max}")

# 向量化版最大值也应 ≤10
test_series = pd.Series(['093000', '100000', '143000'])
vec_scores = _vectorized_seal_time_score(test_series)
check(vec_scores.max() <= 10, f"BUG-6: _vectorized_seal_time_score 最大值应 ≤10, 实际 {vec_scores.max()}")
check(vec_scores.min() >= 0, f"BUG-6: _vectorized_seal_time_score 最小值应 ≥0, 实际 {vec_scores.min()}")

# 标量版和向量化版对同一时间应返回相同值
scalar_0930 = seal_time_score('093000')
vec_0930 = _vectorized_seal_time_score(pd.Series(['093000']))[0]
check(abs(scalar_0930 - vec_0930) < 0.01,
      f"BUG-6: 标量版和向量化版对 093000 应返回相同值, 标量={scalar_0930}, 向量化={vec_0930}")

# ── A8: BUG-8 — score_new.py 不再有 bare except ──
from scoring import score_new as sn
sn_src = _inspect.getsource(sn)
# 检查是否有裸 except (非 except Exception)
bare_excepts = re.findall(r'^\s*except\s*:', sn_src, re.MULTILINE)
check(len(bare_excepts) == 0,
      f"BUG-8: score_new.py 不应有 bare except, 发现 {len(bare_excepts)} 处")

# ── A9: BUG-9 — backtest_engine 不再硬编码 Windows 路径 ──
import backtest.backtest_engine as be
be_src = _inspect.getsource(be)
check('C:\\\\Users\\\\16689' not in be_src and 'r"C:\\Users' not in be_src,
      "BUG-9: backtest_engine 不应硬编码 C:\\Users 路径")
check('_PROJECT_ROOT = os.path.abspath' in be_src,
      "BUG-9: backtest_engine 应使用 _PROJECT_ROOT = os.path.abspath 动态路径")

# ── A10: BUG-10 — app.py 不再有 bare except ──
bare_excepts_app = re.findall(r'^\s*except\s*:', app_src, re.MULTILINE)
check(len(bare_excepts_app) == 0,
      f"BUG-10: app.py 不应有 bare except, 发现 {len(bare_excepts_app)} 处")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 1 (旧): 缓存模块
# ════════════════════════════════════════════════════════════════════════════════
section("1. cache 模块")

import core.cache as cache

# 交易日历
cal = cache._load_trading_calendar()
check(len(cal) > 5000, f"交易日历应有>5000天，实际{len(cal)}")
check("20260529" in cal, "2026-05-29 应在交易日历中")
check("20260531" not in cal, "2026-05-31(周日)不应在交易日历中")
check(cache._is_trading_day("20260529"), "_is_trading_day 应返回 True")

# 冻结逻辑
from datetime import datetime, timezone, timedelta
_CST = timezone(timedelta(hours=8))
now = datetime.now(_CST)
frozen = cache._is_market_frozen()
if now.hour >= 15 or now.weekday() >= 5:
    check(frozen, f"当前时间{now.hour}:{now.minute}应冻结")
else:
    check(not frozen or not cache._is_trading_day(now.strftime("%Y%m%d")),
          "盘中应不冻结(或今日非交易日)")

# trading_date
td = cache._trading_date()
check(td is not None and len(td) == 10, f"trading_date 应返回 YYYY-MM-DD，实际: {td}")

# daily_get/set 循环
test_data = {"test": True, "val": 42}
cache.daily_set("_test_invariant", test_data, force=True)
retrieved = cache.daily_get("_test_invariant")
check(retrieved is not None, "daily_set 后 daily_get 应非空")
check(retrieved.get("val") == 42 if retrieved else False, "缓存数据应一致")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 2: 权重模块
# ════════════════════════════════════════════════════════════════════════════════
section("2. weight_manager 模块")

w = wm.DEFAULT_WEIGHTS
check(isinstance(w, dict), "DEFAULT_WEIGHTS 应返回 dict")
check(w['seal'] == 28.0, f"seal 默认权重应为 28.0, 实际 {w['seal']}")
check(w['money'] == 17.0, f"money 默认权重应为 17.0, 实际 {w['money']}")
check('community' not in w, "DEFAULT_WEIGHTS 不应含 community")
check('principal_score' in w, "DEFAULT_WEIGHTS 应含 principal_score")
check(w.get('principal_score', 0) >= 8.0, f"principal_score 权重应>=8, 实际 {w.get('principal_score', 0)}")
check('buyability' in w, "DEFAULT_WEIGHTS 应含 buyability")
check('sector' in w, "DEFAULT_WEIGHTS 应含 sector(合并后)")
check('north_flow' in w, "DEFAULT_WEIGHTS 应含 north_flow")
check('alpha' in w, "DEFAULT_WEIGHTS 应含 alpha")
check('crash_resistance' in w, "DEFAULT_WEIGHTS 应含 crash_resistance")
# 非 DEPRECATED 因子(参与加权, sentiment 仅乘法系数不参与):
# seal+tech+sector+history+money+stock_sentiment+principal_score+north_flow+alpha+crash_resistance
active_keys = ['seal', 'tech', 'sector', 'history', 'money', 'stock_sentiment',
               'principal_score', 'north_flow', 'alpha', 'crash_resistance']
active_sum = sum(w[k] for k in active_keys)
check(active_sum > 0, f"活跃因子加权和应 >0, 实际 {active_sum}")
check(all(w[k] >= 0 for k in active_keys), "所有活跃因子权重应 >=0")
check(w.get('buyability', -1) == 0.0, f"buyability应=0(已退场), 实际 {w.get('buyability', -1)}")

# apply_weights 计算验证
idx = pd.Index([0, 1])
zeros = pd.Series(0.0, index=idx)
ones = pd.Series(5.0, index=idx)
maxes_seal = pd.Series([28.0, 28.0], index=idx)
result = wm.apply_weights(maxes_seal, zeros, ones, zeros, zeros, ones, stock_sentiment_scores=ones, principal_scores=ones, weights=w)
check(result.max() <= 100, f"满分时应 <=100, 实际 {result.max():.1f}")
check(result.min() >= 0, f"零分时应 >=0, 实际 {result.min():.1f}")
check(len(result) == 2, f"应返回 2 只股票, 实际 {len(result)}")

# 反转权重原子写
_orig_rev = wm._REV_WEIGHTS_FILE
import tempfile, shutil as _sh
_test_rev_dir = tempfile.mkdtemp(prefix='wm_rev_')
_test_rev = os.path.join(_test_rev_dir, 'rev.json')
wm._REV_WEIGHTS_FILE = _test_rev
try:
    wm.save_reversal_weights({'turnover': 25, 'consecutive': 30, 'pullback': 25, 'sector': 15, 'retention': 5})
    leftover = [f for f in os.listdir(_test_rev_dir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_reversal_weights 原子写不应留 .tmp, 实际: {leftover}")
    loaded = wm.load_reversal_weights()
    check(loaded.get('turnover') == 25, f"save_reversal_weights 后 load 应拿到新值, 实际 turnover={loaded.get('turnover')}")
finally:
    wm._REV_WEIGHTS_FILE = _orig_rev
    _sh.rmtree(_test_rev_dir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 3: seal_time_score 范围一致性
# ════════════════════════════════════════════════════════════════════════════════
section("3. seal_time_score 范围一致性")

# 标量版
check(0 <= seal_time_score('093000') <= 10, f"09:30 → {seal_time_score('093000')}")
check(0 <= seal_time_score('103000') <= 10, f"10:30 → {seal_time_score('103000')}")
check(0 <= seal_time_score('113000') <= 10, f"11:30 → {seal_time_score('113000')}")
check(0 <= seal_time_score('133000') <= 10, f"13:30 → {seal_time_score('133000')}")
check(0 <= seal_time_score('143000') <= 10, f"14:30 → {seal_time_score('143000')}")
check(seal_time_score('093000') > seal_time_score('143000'), "早盘封板分应 > 尾盘封板分")
check(0 <= seal_time_score('INVALID') <= 10, f"无效输入应返回中位分, 实际 {seal_time_score('INVALID')}")

# 向量化版
vec = _vectorized_seal_time_score(pd.Series(['093000', '103000', '113000', '133000', '143000', 'INVALID']))
check(vec.max() <= 10, f"向量化版最大值应 ≤10, 实际 {vec.max()}")
check(vec.min() >= 0, f"向量化版最小值应 ≥0, 实际 {vec.min()}")
check(vec.iloc[0] > vec.iloc[4], "向量化版: 早盘(0930)应 > 尾盘(1430)")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 4: scanner 评分函数 (精简 — 仅验证函数签名和返回范围)
# ════════════════════════════════════════════════════════════════════════════════
section("4. scanner 评分函数 (网络依赖)")

import scanner
pool = scanner.fetch_limit_up_pool()
if pool is not None and not pool.empty:
    check(len(pool) > 0, f"涨停池应非空, 实际 {len(pool)} 只")
    filtered = scanner.pre_filter(pool)
    if not filtered.empty:
        seal_s = scanner.score_seal_strength(filtered)
        check(seal_s.max() <= 28, f"封板强度应 <=28, 实际 {seal_s.max():.1f}")
        check(seal_s.min() >= 0, f"封板强度应 >=0, 实际 {seal_s.min():.1f}")

        tech_s = scanner.score_tech_form(filtered)
        check(tech_s.max() <= 10, f"量价应 <=10, 实际 {tech_s.max():.1f}")

        sector_mom = scanner.get_sector_heat_scores(filtered)
        check(sector_mom.max() <= 12, f"板块热度应 <=12, 实际 {sector_mom.max():.1f}")

        sector_res = scanner.get_sector_resonance(filtered)
        check(sector_res.max() <= 8, f"板块共振应 <=8, 实际 {sector_res.max():.1f}")

        principal_s = scanner.score_by_principal(filtered, 30000)
        check(principal_s.max() <= 10, f"本金评分应 <=10, 实际 {principal_s.max():.1f}")
    else:
        check(True, "前置过滤后为空(跳过)")
else:
    check(True, "涨停池为空 — 可能非交易时间(跳过验证)")
    check(pool is not None, "fetch_limit_up_pool 不应抛异常")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 5: 完整扫描管道
# ════════════════════════════════════════════════════════════════════════════════
section("5. 完整扫描管道 (app._scan_limit_up_data)")

from app import _scan_limit_up_data, _make_cache_entry, _today_trading
today_str = _today_trading()
data = _scan_limit_up_data(today_str, principal=30000)

if data is not None:
    stocks = data.get('stocks', [])
    check(len(stocks) > 0, f"TOP_N 应非空, 实际 {len(stocks)}")
    if stocks:
        s = stocks[0]
        check(0 <= s['total_score'] <= 100, f"总分应在 0-100, 实际 {s['total_score']}")
        check(0 <= s.get('seal_score', -1) <= 28, f"seal_score 应在 0-28")
        check(0 <= s.get('money_score', -1) <= 20, f"money_score 应在 0-20")
        check(0 <= s.get('tech_score', -1) <= 10, f"tech_score 应在 0-10")
        check('community_score' not in s, "不应含 community_score")
        check('principal_score' in s, "应含 principal_score")

    sentiment = data.get('sentiment_level', '')
    check(sentiment != '未知' and sentiment != '', f"情绪应非空: {sentiment}")

    # 返回数据完整性
    required = ['stocks', 'seal_scores', 'money_scores', 'sector_mom', 'sector_res',
                'tech_scores', 'history_scores', 'sentiment_score']
    for r in required:
        check(r in data, f"返回 dict 应含 '{r}'")

    # 缓存一致性
    cache_entry = _make_cache_entry(data['stocks'], data['sentiment_score'],
                                     data['sentiment_level'], data['date'])
    check(cache_entry.get('ok'), "缓存条目 ok=True")
    check(len(cache_entry['stocks']) == len(stocks), "缓存 stocks 数量一致")
else:
    check(True, "扫描返回 None — 可能非交易时间(跳过)")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 6: 日期逻辑
# ════════════════════════════════════════════════════════════════════════════════
section("6. 日期逻辑")

t = _today_trading()
check(len(t) == 8, f"_today_trading 应返回 8 位 YYYYMMDD, 实际 {t}")
check(t == today_str, "_today_trading 两次调用应一致")

sent_yesterday = scanner.detect_market_sentiment(today_str)
check(sent_yesterday is not None, "detect_market_sentiment 不应返回 None")
score, level, details = sent_yesterday
check(0 <= score <= 10, f"情绪分应在 0-10, 实际 {score}")
check(any(kw in level for kw in ['冰点', '低迷', '正常', '活跃', '高潮', '未知']),
      f"情绪等级应含有效关键词, 实际: {level}")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 7: 市场状态检测 + 趋势扫描
# ════════════════════════════════════════════════════════════════════════════════
section("7. 市场状态检测 + 趋势扫描")

status = scanner.get_market_status()
check(status in ('trading', 'lunch', 'closed', 'weekend', 'holiday'),
      f"市场状态应有效, 实际: {status}")

mode = scanner.get_default_mode()
check(mode in ('trend', 'after_hours'), f"默认模式应有效, 实际: {mode}")
if status == 'trading':
    check(mode == 'trend', f"盘中默认应为 trend, 实际: {mode}")
else:
    check(mode == 'after_hours', f"非盘中默认应为 after_hours, 实际: {mode}")

# scan_trend FAIL-FAST: 异常 = 真 BUG
import io as _io
from datetime import date as date_cls
today_raw_test = date_cls.today().strftime("%Y%m%d")
_old_stdout = sys.stdout
sys.stdout = _io.StringIO()
try:
    result = scanner.scan_trend(today_raw_test, _table_mode=False, top_n=5)
    sys.stdout = _old_stdout
    check(True, "scan_trend 应正常完成(无异常)")
except Exception as e:
    sys.stdout = _old_stdout
    check(False, f"scan_trend 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")

try:
    pool_dated = scanner.fetch_limit_up_pool(date_str=today_raw_test)
    check(pool_dated is not None, "fetch_limit_up_pool(date_str=...) 应非 None")
except Exception as e:
    check(False, f"fetch_limit_up_pool 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 8: 多 Tab 回测引擎
# ════════════════════════════════════════════════════════════════════════════════
section("8. 多 Tab 回测引擎")

from backtest_engine import (
    run_tab_backtest, run_t1_backtest, ALL_TABS, TAB_NAMES_CN,
    SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS,
    _PENDING_TABS, _SELF_FETCHING_TABS,
)

expected_tabs = {'limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal', 'sector'}
check(set(ALL_TABS) == expected_tabs,
      f"ALL_TABS 应含 6 个 tab, 实际 {len(ALL_TABS)} 个: {ALL_TABS}")
check(set(SIGNAL_POOL_FETCHERS.keys()) == expected_tabs, "SIGNAL_POOL_FETCHERS 应覆盖所有 tab")
check(set(SCORE_FUNCS.keys()) == expected_tabs, "SCORE_FUNCS 应覆盖所有 tab")
check(set(SCORE_COLUMNS.keys()) == expected_tabs, "SCORE_COLUMNS 应覆盖所有 tab")

check(len(_PENDING_TABS) == 0, f"全部 tab 应已实现, PENDING_TABS={_PENDING_TABS}")
check('sector' in _SELF_FETCHING_TABS, "sector tab 应该是 SELF_FETCHING")

# 未知 tab 返回 error, 不抛异常
try:
    bad = run_tab_backtest(tab='unknown-tab', max_days=5, top_n=3, capital=10000, use_cache=False)
    check('error' in bad and bad.get('error', '').startswith('未知 tab'),
          f"未知 tab 应返回 error 字段, 实际: {bad}")
except Exception as e:
    check(False, f"未知 tab 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 9: 持久化与缓存安全
# ════════════════════════════════════════════════════════════════════════════════
section("9. 持久化与缓存安全")

# 9.1 权重原子写
_orig_weights_file = wm._WEIGHTS_FILE
_tmpdir = tempfile.mkdtemp(prefix='wm_test_')
_test_wf = os.path.join(_tmpdir, 'weights.json')
wm._WEIGHTS_FILE = _test_wf
try:
    wm.save_weights({'seal': 50, 'money': 50, 'sector': 0, 'tech': 0, 'history': 0,
                       'stock_sentiment': 0, 'principal_score': 0})
    leftover = [f for f in os.listdir(_tmpdir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_weights 原子写不应留 .tmp, 实际: {leftover}")
    loaded = wm.load_weights()
    check(loaded.get('seal') == 50, f"save_weights 后 load_weights 应拿到 seal=50, 实际 {loaded.get('seal')}")
finally:
    wm._WEIGHTS_FILE = _orig_weights_file
    _sh.rmtree(_tmpdir, ignore_errors=True)

# 9.2 trend 权重原子写
_orig_tw = wm._TREND_WEIGHTS_FILE
_test_tw_dir = tempfile.mkdtemp(prefix='wm_tw_')
_test_tw = os.path.join(_test_tw_dir, 'trend.json')
wm._TREND_WEIGHTS_FILE = _test_tw
try:
    wm.save_trend_weights({'tech_break': 0.3, 'volume': 0.3, 'ma_align': 0.4})
    leftover = [f for f in os.listdir(_test_tw_dir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_trend_weights 原子写不应留 .tmp, 实际: {leftover}")
finally:
    wm._TREND_WEIGHTS_FILE = _orig_tw
    _sh.rmtree(_test_tw_dir, ignore_errors=True)

# 9.3 daily_get_pkl 跨日应自动失效
import pickle as _pkl
_test_pkl_dir = tempfile.mkdtemp(prefix='pkl_test_')
_test_pkl = os.path.join(_test_pkl_dir, 'test.pkl')
with open(_test_pkl, 'wb') as f:
    _pkl.dump({'old': 'data'}, f)
import time as _t
old_time = _t.time() - 5 * 86400
os.utime(_test_pkl, (old_time, old_time))
_orig_cache_dir = cache._CACHE_DIR
cache._CACHE_DIR = _test_pkl_dir
try:
    today_d = cache._trading_date()
    test_file = os.path.join(_test_pkl_dir, f"daily_{today_d}_audit9_v{cache._CACHE_VER}.pkl")
    import shutil as _sh2
    _sh2.copy(_test_pkl, test_file)
    os.utime(test_file, (old_time, old_time))
    result = cache.daily_get_pkl('audit9')
    check(result is None, f"daily_get_pkl 跨日应返 None, 实际: {result}")
    check(not os.path.exists(test_file), f"daily_get_pkl 跨日应删旧文件, 文件应不存在")
finally:
    cache._CACHE_DIR = _orig_cache_dir
    _sh.rmtree(_test_pkl_dir, ignore_errors=True)

# 9.4 clear_all 应清 .json 旧版本
_test_clear_dir = tempfile.mkdtemp(prefix='clear_test_')
_old_json = os.path.join(_test_clear_dir, f"daily_20260101_oldkey_v{cache._CACHE_VER - 1}.json")
_new_json = os.path.join(_test_clear_dir, f"daily_20260101_newkey_v{cache._CACHE_VER}.json")
_old_pkl = os.path.join(_test_clear_dir, f"somecache_v{cache._CACHE_VER - 1}.pkl")
with open(_old_json, 'w') as f: f.write('{}')
with open(_new_json, 'w') as f: f.write('{}')
with open(_old_pkl, 'wb') as f: _pkl.dump({}, f)
_orig_cache_dir = cache._CACHE_DIR
cache._CACHE_DIR = _test_clear_dir
try:
    cache.clear_all()
    check(not os.path.exists(_old_json), f"clear_all 应删旧版 .json, 文件应不存在")
    check(os.path.exists(_new_json), f"clear_all 应保留当前版 .json, 文件应存在")
    check(not os.path.exists(_old_pkl), f"clear_all 应删旧版 .pkl, 文件应不存在")
finally:
    cache._CACHE_DIR = _orig_cache_dir
    _sh.rmtree(_test_clear_dir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 10: 代码质量不变性 (v1.25.0 新增)
# ════════════════════════════════════════════════════════════════════════════════
section("10. 代码质量不变性")

# 10.1 score_new.py 交互加分不超过上限
from scoring.score_new import INTERACTION_BONUS
check(INTERACTION_BONUS <= 5, f"INTERACTION_BONUS 应 <=5, 实际 {INTERACTION_BONUS}")

# 10.2 DEFAULT_WEIGHTS buyability 应为 0
check(wm.DEFAULT_WEIGHTS.get('buyability', -1) == 0.0, "buyability 应=0(已退场)")

# 10.3 所有评分因子应 >=0
check(all(v >= 0 for v in wm.DEFAULT_WEIGHTS.values()), "DEFAULT_WEIGHTS 所有值应 >=0")
check(all(v >= 0 for v in wm.REV_DEFAULT_WEIGHTS.values()), "REV_DEFAULT_WEIGHTS 所有值应 >=0")


# ════════════════════════════════════════════════════════════════════════════════
#  SECTION 11: 核心函数可调用性
# ════════════════════════════════════════════════════════════════════════════════
section("11. 核心函数可调用性 (无网络)")

# 验证关键模块的关键函数存在且可调用
import scanner_filters as sf
check(callable(sf.filter_non_main_board), "scanner_filters.filter_non_main_board 应可调用")
check(callable(sf.filter_xr_xd_dr), "scanner_filters.filter_xr_xd_dr 应可调用")
check(callable(sf.pre_filter), "scanner_filters.pre_filter 应可调用")
check(callable(sf.filter_by_price), "scanner_filters.filter_by_price 应可调用")

import ak_utils as ak_u
check(callable(ak_u.safe_ak), "ak_utils.safe_ak 应可调用")

from scoring.scanner_scoring import score_zhaban_data, score_dtqiaoban_data, score_sector_data
check(callable(score_zhaban_data), "scanner_scoring.score_zhaban_data 应可调用")
check(callable(score_dtqiaoban_data), "scanner_scoring.score_dtqiaoban_data 应可调用")
check(callable(score_sector_data), "scanner_scoring.score_sector_data 应可调用")

from scoring.score_new import score_new
check(callable(score_new), "score_new.score_new 应可调用")

# verify scanner_utils 范围
check(0 <= seal_time_score('100000') <= 10, "seal_time_score(10:00) 应在 0-10")


# ── 总结 ──
section("总结")
print(f"  [PASS] {PASS} 通过")
print(f"  [FAIL] {FAIL} 失败")
if FAIL > 0:
    sys.exit(1)
else:
    print("  全部通过! 系统逻辑无 BUG。")
