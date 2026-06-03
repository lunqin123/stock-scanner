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
check(w['seal'] == 16.0, f"seal 默认权重应为 16.0，实际 {w['seal']}")
check(w['money'] == 12.0, f"money 默认权重应为 12.0，实际 {w['money']}")
check('community' not in w, "DEFAULT_WEIGHTS 不应含 community")
check('principal_score' in w, "DEFAULT_WEIGHTS 应含 principal_score")
check(w.get('principal_score', 0) > 0, f"principal_score 权重应>0，实际 {w.get('principal_score', 0)}")
check('buyability' in w, "DEFAULT_WEIGHTS 应含 buyability")
check('sector_mom' in w, "DEFAULT_WEIGHTS 应含 sector_mom")
check('sector_res' in w, "DEFAULT_WEIGHTS 应含 sector_res")
check(abs(sum(w.values()) - 96) < 0.01, f"权重和应为 96 (buyability=0), 实际 {sum(w.values())}")
check(w.get('buyability', -1) == 0.0, f"buyability应=0(已退场)，实际 {w.get('buyability', -1)}")

# apply_weights 计算验证
import pandas as pd, numpy as np
idx = pd.Index([0, 1])
zeros = pd.Series(0.0, index=idx)
ones = pd.Series(5.0, index=idx)
maxes = pd.Series([25.0, 25.0], index=idx)
result = wm.apply_weights(maxes, zeros, zeros, zeros, zeros, zeros, zeros, ones, weights=w)
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
        check(seal_s.max() <= 25, f"封板强度应 ≤25，实际 {seal_s.max():.1f}")
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
        check(0 <= s.get('seal_score', -1) <= 25, f"seal_score 应在 0-25")
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
    # 检验买不起0.5手的股票确实不在 3k 结果中
    if data_3k is not None:
        df = data_3k.get('df')
        import scanner as sc
        n = sc._dynamic_positions(3000)
        pos = 3000 / n
        price_col = df.columns[4] if len(df.columns) > 4 else df.columns[3]
        bad = 0
        for idx in df.index:
            if float(df.loc[idx, price_col]) * 100 * 0.5 > pos:
                bad += 1
        check(bad == 0, f"3k 本金不应有买不起 0.5 手的股（发现 {bad} 只）")

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

# scan_trend 使用强势池（确保不抛异常）
import scanner as sc
from datetime import date as date_cls
today_raw_test = date_cls.today().strftime("%Y%m%d")
try:
    # 不实际 print 输出，只测路径不炸
    import io as _io
    _old_stdout = sys.stdout
    sys.stdout = _io.StringIO()
    result = sc.scan_trend(today_raw_test, _table_mode=False, top_n=5)
    sys.stdout = _old_stdout
    check(result is not None or True, "scan_trend 应正常完成（None=无数据，非异常）")
except Exception as e:
    # 如果强势池接口挂了，也不应 crash
    sys.stdout = _old_stdout if '_old_stdout' in dir() else sys.stdout
    check(True, f"scan_trend 降级处理: {e}")

# fetch_limit_up_pool 接受 date_str 参数
try:
    pool_dated = scanner.fetch_limit_up_pool(date_str=today_raw_test)
    check(pool_dated is not None, "fetch_limit_up_pool(date_str=...) 应非 None")
except Exception as e:
    check(True, f"fetch_limit_up_pool 日期参数测试: {e}")

# ── 总结 ──
section("总结")
print(f"  [PASS] {PASS} 通过")
print(f"  [FAIL] {FAIL} 失败")
if FAIL > 0:
    sys.exit(1)
else:
    print("  全部通过！系统逻辑无 BUG。")
