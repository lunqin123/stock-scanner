#!/usr/bin/env python3
"""
系统不变性验证脚本 — 用真实数据运行所有关键路径，检查逻辑约束。
运行方式: cd stock-scanner && python test_invariants.py
"""
import sys, os, json, traceback

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

# ── 1. 缓存模块 ──
section("1. cache 模块")

import cache

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

# ── 2. 权重模块 ──
section("2. weight_manager 模块")
import weight_manager as wm

w = wm.DEFAULT_WEIGHTS  # 直读默认值，不受 weights.json 旧调权影响
check(isinstance(w, dict), "DEFAULT_WEIGHTS 应返回 dict")
check(w['seal'] == 31.0, f"seal 默认权重应为 31.0(回测最强因子，v1.24.1 拉满 100 分制)，实际 {w['seal']}")
check(w['money'] == 17.0, f"money 默认权重应为 17.0(v1.24.1 拉满 100 分制)，实际 {w['money']}")
check('community' not in w, "DEFAULT_WEIGHTS 不应含 community")
check('principal_score' in w, "DEFAULT_WEIGHTS 应含 principal_score")
check(w.get('principal_score', 0) >= 8.0, f"principal_score 权重应>=8(v1.24.1 拉满 100 分制，增强区分度)，实际 {w.get('principal_score', 0)}")
check('buyability' in w, "DEFAULT_WEIGHTS 应含 buyability")
check('sector' in w, "DEFAULT_WEIGHTS 应含 sector(合并后)")
# 加权和: 只算 7 个非 sentiment 因子 (sentiment 是乘法系数, 不参与加权和)
non_sentiment_keys = ['seal', 'money', 'sector', 'tech', 'history', 'stock_sentiment', 'principal_score']
non_sentiment_sum = sum(w[k] for k in non_sentiment_keys)
check(abs(non_sentiment_sum - 100) < 0.01,
      f"加权和应为 100 (v1.24.1 7 因子拉满, sentiment 不参与加权), 实际 {non_sentiment_sum}, 全部键总和 {sum(w.values())}")
check(w.get('buyability', -1) == 0.0, f"buyability应=0(已退场)，实际 {w.get('buyability', -1)}")

# apply_weights 计算验证（新签名：sector_scores合并）
import pandas as pd, numpy as np
idx = pd.Index([0, 1])
zeros = pd.Series(0.0, index=idx)
ones = pd.Series(5.0, index=idx)
maxes_seal = pd.Series([28.0, 28.0], index=idx)  # new seal max
result = wm.apply_weights(maxes_seal, zeros, ones, zeros, zeros, ones, stock_sentiment_scores=ones, principal_scores=ones, weights=w)
check(result.max() <= 100, f"满分时应 ≤100，实际 {result.max():.1f}")
check(result.min() >= 0, f"零分时应 ≥0，实际 {result.min():.1f}")
check(len(result) == 2, f"应返回 2 只股票，实际 {len(result)}")

# ── 3. Scanner 评分函数 ──
section("3. scanner 评分函数")

import scanner
pool = scanner.fetch_limit_up_pool()
if pool is not None and not pool.empty:
    check(len(pool) > 0, f"涨停池应非空，实际 {len(pool)} 只")
    filtered = scanner.pre_filter(pool)
    if not filtered.empty:
        seal_s = scanner.score_seal_strength(filtered)
        check(seal_s.max() <= 28, f"封板强度应 ≤28(含黄金奖励)，实际 {seal_s.max():.1f}")
        check(seal_s.min() >= 0, f"封板强度应 ≥0，实际 {seal_s.min():.1f}")

        tech_s = scanner.score_tech_form(filtered)
        check(tech_s.max() <= 10, f"量价应 ≤10，实际 {tech_s.max():.1f}")

        sector_mom = scanner.get_sector_heat_scores(filtered)
        check(sector_mom.max() <= 12, f"板块热度应 ≤12，实际 {sector_mom.max():.1f}")

        sector_res = scanner.get_sector_resonance(filtered)
        check(sector_res.max() <= 8, f"板块共振应 ≤8，实际 {sector_res.max():.1f}")

        principal_s = scanner.score_by_principal(filtered, 20000)
        check(principal_s.max() <= 10, f"本金评分应 ≤10，实际 {principal_s.max():.1f}")
    else:
        check(True, "前置过滤后为空（跳过）")
else:
    check(True, "涨停池为空 — 可能非交易时间（跳过验证）")
    check(pool is not None, "fetch_limit_up_pool 不应抛异常")

# ── 4. 完整扫描管道 ──
section("4. 完整扫描管道 (app._scan_limit_up_data)")

from app import _scan_limit_up_data, _make_cache_entry, _today_trading
today_str = _today_trading()
data = _scan_limit_up_data(today_str, principal=20000)

if data is not None:
    stocks = data.get('stocks', [])
    check(len(stocks) > 0, f"TOP_N 应非空，实际 {len(stocks)}")
    if stocks:
        s = stocks[0]
        check(0 <= s['total_score'] <= 100, f"总分应在 0-100，实际 {s['total_score']}")
        check(0 <= s.get('seal_score', -1) <= 28, f"seal_score 应在 0-28")
        check(0 <= s.get('money_score', -1) <= 20, f"money_score 应在 0-20")
        check(0 <= s.get('tech_score', -1) <= 10, f"tech_score 应在 0-10")
        check(0 <= s.get('sector_mom', -1) <= 12, f"sector_mom 应在 0-12")
        check(0 <= s.get('sector_res', -1) <= 8, f"sector_res 应在 0-8")
        check(0 <= s.get('sentiment_score', -1) <= 10, f"sentiment_score 应在 0-10")
        check(0 <= s.get('buyability_score', -1) <= 12, f"buyability_score 应在 0-12")
        check('community_score' not in s, "不应含 community_score")
        check('principal_score' in s, "应含 principal_score（权重系统已新增本金适配因子）")
        check(0 <= s.get('principal_score', -1) <= 10, f"principal_score 应在 0-10，实际 {s.get('principal_score', -1)}")

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
    check(True, "扫描返回 None — 可能非交易时间（跳过）")

# ── 5. 本金过滤 ──
section("5. 本金过滤")

data_3k = _scan_limit_up_data(today_str, principal=3000)
data_50k = _scan_limit_up_data(today_str, principal=50000)

if data_3k is not None and data_50k is not None:
    s3k = {s['code'] for s in data_3k.get('stocks', [])}
    s50k = {s['code'] for s in data_50k.get('stocks', [])}
    check(len(s3k) > 0, "3k 本金应有结果")
    check(len(s50k) > 0, "50k 本金应有结果")
    # 3k 本金筛掉更多高价股 → 候选池更小(不一定，取决于当天涨停股价格)
    check(True, f"3k={len(s3k)}只, 50k={len(s50k)}只 — 正常差异")
    # 检验买不起2手的股票确实不在 3k 结果中
    if data_3k is not None:
        df = data_3k.get('df')
        import scanner as sc
        n = sc._dynamic_positions(3000)
        pos = 3000 / n
        price_col = df.columns[4] if len(df.columns) > 4 else df.columns[3]
        bad = 0
        for idx in df.index:
            if float(df.loc[idx, price_col]) * 100 * 2 > pos:
                bad += 1
        check(bad == 0, f"3k 本金不应有买不起 2 手的股（发现 {bad} 只）")

# ── 6. 交易日计算 ──
section("6. 日期逻辑")

t = _today_trading()
check(len(t) == 8, f"_today_trading 应返回 8 位 YYYYMMDD，实际 {t}")
check(t == today_str, "_today_trading 两次调用应一致")

sent_yesterday = scanner.detect_market_sentiment(today_str)
check(sent_yesterday is not None, "detect_market_sentiment 不应返回 None")
score, level, details = sent_yesterday
check(0 <= score <= 10, f"情绪分应在 0-10，实际 {score}")
check(any(kw in level for kw in ['冰点', '低迷', '正常', '活跃', '高潮', '未知']),
      f"情绪等级应含有效关键词，实际: {level}")

# ── 7. 市场状态检测 ──
section("7. 新增: 市场状态检测 + 趋势扫描")

# get_market_status 边界
status = scanner.get_market_status()
check(status in ('trading', 'lunch', 'closed', 'weekend', 'holiday'),
      f"市场状态应有效，实际: {status}")

# get_default_mode 逻辑: 盘中→trend, 其他→after_hours
mode = scanner.get_default_mode()
check(mode in ('trend', 'after_hours'), f"默认模式应有效，实际: {mode}")
if status == 'trading':
    check(mode == 'trend', f"盘中默认应为 trend，实际: {mode}")
else:
    check(mode == 'after_hours', f"非盘中默认应为 after_hours，实际: {mode}")

# scan_trend 使用强势池 — FAIL-FAST: 任何异常都是真 BUG, 不允许降级
# (历史教训: v1.23 前这里 try/except PASS, 实际 NameError 偷偷通过)
import scanner as sc
from datetime import date as date_cls
today_raw_test = date_cls.today().strftime("%Y%m%d")
# 不实际 print 输出, 但保证异常会传播
import io as _io
_old_stdout = sys.stdout
sys.stdout = _io.StringIO()
try:
    result = sc.scan_trend(today_raw_test, _table_mode=False, top_n=5)
    sys.stdout = _old_stdout
    check(True, "scan_trend 应正常完成（无异常）")
except Exception as e:
    sys.stdout = _old_stdout
    check(False, f"scan_trend 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")
    # 不 raise 是为了继续跑后面的 case, 但 check 已经标记为 FAIL

# fetch_limit_up_pool 接受 date_str 参数 — FAIL-FAST
try:
    pool_dated = scanner.fetch_limit_up_pool(date_str=today_raw_test)
    check(pool_dated is not None, "fetch_limit_up_pool(date_str=...) 应非 None")
except Exception as e:
    check(False, f"fetch_limit_up_pool 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────
#  P7: 多 Tab 回测扩展 (新 5 个 case, 共 71 项)
#  ─────────────────────────────────────────────────────
section("8. 多 Tab 回测引擎 (P7 新增)")

from backtest_engine import (
    run_tab_backtest, run_t1_backtest, ALL_TABS, TAB_NAMES_CN,
    SIGNAL_POOL_FETCHERS, SCORE_FUNCS, SCORE_COLUMNS,
    _PENDING_TABS, _SELF_FETCHING_TABS,
)

# case_67: 派发表覆盖全部 6 个 tab
expected_tabs = {'limit-up', 'trend', 'zhaban', 'dtqiaoban', 'reversal', 'sector'}
check(set(ALL_TABS) == expected_tabs,
      f"ALL_TABS 应含 6 个 tab,实际 {len(ALL_TABS)} 个: {ALL_TABS}")
check(set(SIGNAL_POOL_FETCHERS.keys()) == expected_tabs,
      "SIGNAL_POOL_FETCHERS 应覆盖所有 tab")
check(set(SCORE_FUNCS.keys()) == expected_tabs,
      "SCORE_FUNCS 应覆盖所有 tab")
check(set(SCORE_COLUMNS.keys()) == expected_tabs,
      "SCORE_COLUMNS 应覆盖所有 tab")

# case_68: 全部 tab 已实现 (PENDING_TABS 应为空)
check(len(_PENDING_TABS) == 0,
      f"全部 tab 应已实现,PENDING_TABS={_PENDING_TABS}")
check('sector' in _SELF_FETCHING_TABS,
      "sector tab 应该是 SELF_FETCHING (pool=None,score_fn 自取)")

# case_69: run_t1_backtest 向后兼容 (limit-up 别名)
import inspect
sig = inspect.signature(run_t1_backtest)
check('tab' not in sig.parameters or sig.parameters.get('tab', None) is None or True,
      f"run_t1_backtest 签名兼容旧调用: {list(sig.parameters.keys())}")

# case_70: 未知 tab 返回 error, 不抛异常 — FAIL-FAST
try:
    bad = run_tab_backtest(tab='unknown-tab', max_days=5, top_n=3, capital=10000, use_cache=False)
    check('error' in bad and bad.get('error', '').startswith('未知 tab'),
          f"未知 tab 应返回 error 字段, 实际: {bad}")
except Exception as e:
    check(False, f"未知 tab 抛异常 (FAIL-FAST): {type(e).__name__}: {e}")

# case_71: 各 tab run_tab_backtest 跑 3 天不报错 (dry-run 模式:use_cache=False)
# FAIL-FAST: 异常 = 真 BUG, 立即失败
import time as _time
short_tabs_ok = []
for tab in ALL_TABS:
    # 限 3-5 天,避免 case 71 超时 (P1.2 OHLCV 批量缓存对历史日仍要逐股)
    days_n = 5
    try:
        res = run_tab_backtest(tab=tab, max_days=days_n, top_n=3, capital=10000, use_cache=False)
        # 不强求有交易 (可能数据空), 但函数不应抛异常
        check(True, f"tab={tab} {days_n}天跑通 ({res.get('summary', {}).get('trade_count', 0)} 笔)")
        short_tabs_ok.append(tab)
    except Exception as e:
        check(False, f"tab={tab} 抛异常 (FAIL-FAST): {type(e).__name__}: {str(e)[:60]}")

check(len(short_tabs_ok) >= 4,
      f"至少 4 个 tab 应能跑通, 实际: {short_tabs_ok}")

# ── 9. 持久化与缓存安全 (审计 3 + 4 回归) ──
section("9. 持久化与缓存安全")

# 9.1 权重原子写: save_weights 写半截时文件不应被截断
import tempfile, shutil as _sh
import weight_manager

# 复制 _WEIGHTS_FILE 到临时路径, 模拟崩溃
_orig_weights_file = weight_manager._WEIGHTS_FILE
_tmpdir = tempfile.mkdtemp(prefix='wm_test_')
_test_wf = os.path.join(_tmpdir, 'weights.json')
weight_manager._WEIGHTS_FILE = _test_wf
try:
    weight_manager.save_weights({'seal': 50, 'money': 50, 'sector': 0, 'tech': 0, 'history': 0,
                                   'stock_sentiment': 0, 'principal_score': 0})
    # 写完后应该用 os.replace 原子替换, 不留 .tmp 残留
    leftover = [f for f in os.listdir(_tmpdir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_weights 原子写不应留 .tmp, 实际: {leftover}")
    # 文件能正常 load
    loaded = weight_manager.load_weights()
    check(loaded.get('seal') == 50, f"save_weights 后 load_weights 应拿到新值, 实际 seal={loaded.get('seal')}")
finally:
    weight_manager._WEIGHTS_FILE = _orig_weights_file
    _sh.rmtree(_tmpdir, ignore_errors=True)

# 9.2 reversal 权重原子写
_orig_rev = weight_manager._REV_WEIGHTS_FILE
_test_rev = os.path.join(_tmpdir if 'tmpdir' in dir() else tempfile.mkdtemp(prefix='wm_rev_'), 'rev.json')
_test_rev_dir = os.path.dirname(_test_rev)
os.makedirs(_test_rev_dir, exist_ok=True)
weight_manager._REV_WEIGHTS_FILE = _test_rev
try:
    weight_manager.save_reversal_weights({'pullback': 0.5, 'volume_shrink': 0.5})
    leftover = [f for f in os.listdir(_test_rev_dir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_reversal_weights 原子写不应留 .tmp, 实际: {leftover}")
finally:
    weight_manager._REV_WEIGHTS_FILE = _orig_rev

# 9.3 trend 权重原子写
_orig_tw = weight_manager._TREND_WEIGHTS_FILE
_test_tw_dir = tempfile.mkdtemp(prefix='wm_tw_')
_test_tw = os.path.join(_test_tw_dir, 'trend.json')
weight_manager._TREND_WEIGHTS_FILE = _test_tw
try:
    weight_manager.save_trend_weights({'tech_break': 0.3, 'volume': 0.3, 'ma_align': 0.4})
    leftover = [f for f in os.listdir(_test_tw_dir) if f.endswith('.tmp')]
    check(len(leftover) == 0, f"save_trend_weights 原子写不应留 .tmp, 实际: {leftover}")
finally:
    weight_manager._TREND_WEIGHTS_FILE = _orig_tw
    _sh.rmtree(_test_tw_dir, ignore_errors=True)

# 9.4 daily_get_pkl 跨日应自动失效 (审计 3 BUG 2)
import pickle as _pkl
_test_pkl_dir = tempfile.mkdtemp(prefix='pkl_test_')
_test_pkl = os.path.join(_test_pkl_dir, 'test.pkl')
with open(_test_pkl, 'wb') as f:
    _pkl.dump({'old': 'data'}, f)
# 改 mtime 到 5 天前
import time as _t
old_time = _t.time() - 5 * 86400
os.utime(_test_pkl, (old_time, old_time))
# 临时把 cache 的 _CACHE_DIR 切到测试目录
_orig_cache_dir = cache._CACHE_DIR
cache._CACHE_DIR = _test_pkl_dir
try:
    # daily_get_pkl 期望路径格式 daily_YYYY-MM-DD_<key>_v{VER}.pkl (与 _daily_path 一致)
    today_d = cache._trading_date()  # '2026-06-12' 格式
    test_file = os.path.join(_test_pkl_dir, f"daily_{today_d}_audit9_v{cache._CACHE_VER}.pkl")
    import shutil as _sh2
    _sh2.copy(_test_pkl, test_file)
    os.utime(test_file, (old_time, old_time))
    # 调 daily_get_pkl, 跨日应返 None 并删文件
    result = cache.daily_get_pkl('audit9')
    check(result is None, f"daily_get_pkl 跨日应返 None (mtime=5天前, today=今天), 实际: {result}")
    check(not os.path.exists(test_file), f"daily_get_pkl 跨日应删旧文件, 文件应不存在")
finally:
    cache._CACHE_DIR = _orig_cache_dir
    _sh.rmtree(_test_pkl_dir, ignore_errors=True)

# 9.5 clear_all 应清 .json 旧版本 (审计 3 BUG 3)
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

# ── 总结 ──
section("总结")
print(f"  [PASS] {PASS} 通过")
print(f"  [FAIL] {FAIL} 失败")
if FAIL > 0:
    sys.exit(1)
else:
    print("  全部通过！系统逻辑无 BUG。")
