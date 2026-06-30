#!/usr/bin/env python3
"""
A股超短线选股扫描器 — Web 交互界面
FastAPI 后端，封装各扫描功能为 REST API
"""
import io
import os
import sys
import json
import asyncio
import threading
import queue
from contextlib import redirect_stdout, redirect_stderr
from datetime import date, datetime, timezone, timedelta
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from cache import daily_get, daily_set, daily_get_pkl, daily_set_pkl, make_key
from recommendation_tracker import save_recommendations, get_per_tab_stats as _get_tracker_stats
app = FastAPI(title="A股超短线选股扫描器", version="1.0.0")

_CST = timezone(timedelta(hours=8))

def _today_trading() -> str:
    """返回当前交易日 YYYYMMDD，处理凌晨/周末/节假日"""
    from cache import _is_trading_day
    now = datetime.now(_CST)
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        now -= timedelta(hours=12)  # 盘前归到前一天
    # 回退直到找到最近的交易日
    for _ in range(8):
        d_str = now.strftime("%Y%m%d")
        if _is_trading_day(d_str):
            return d_str
        now -= timedelta(days=1)
    # 8天都没交易日（不可能），回退到周五
    while now.weekday() >= 5:
        now -= timedelta(days=1)
    return now.strftime("%Y%m%d")

def _fetched_at() -> str:
    """返回当前东八区时间字符串，格式 HH:MM:SS"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")

# ── 挂载静态文件 ──
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 自定义 StaticFiles：强制 no-cache，避免浏览器缓存旧 JS/CSS
class _NoCacheStaticFiles(StaticFiles):
    async def __call__(self, scope, receive, send):
        async def _send(msg):
            if msg["type"] == "http.response.start":
                headers = dict(msg.get("headers", []))
                headers[b"cache-control"] = b"no-cache, no-store, must-revalidate"
                msg["headers"] = list(headers.items())
            await send(msg)
        await super().__call__(scope, receive, _send)

app.mount("/static", _NoCacheStaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")


# ═══════════════════════════════════════════
#  工具：在隔离环境中运行扫描函数并捕获输出
# ═══════════════════════════════════════════

def _capture(fn, *args, **kwargs):
    """运行函数 fn，捕获 stdout/stderr，返回 (输出文本, 错误文本)"""
    f_out = io.StringIO()
    f_err = io.StringIO()
    try:
        with redirect_stdout(f_out), redirect_stderr(f_err):
            fn(*args, **kwargs)
    except Exception as e:
        f_err.write(f"\n[运行时错误] {e}\n")
    return f_out.getvalue(), f_err.getvalue()


# ═══════════════════════════════════════════
#  API 端点
# ═══════════════════════════════════════════

@app.get("/api/plans")
def api_list_plans():
    """列出所有可用评分方案"""
    from plans import list_plans
    return {"ok": True, "plans": list_plans()}


@app.get("/api/scan/limit-up")
def api_scan_limit_up(table: bool = Query(False, description="表格模式")):
    """涨停池扫描"""
    from scanner import fetch_limit_up_pool, pre_filter, score_seal_strength
    from scanner import get_money_flow_scores, get_sector_heat_scores, score_tech_form
    from scanner import format_table_output, format_output

    today = _today_trading()
    out, err = _capture(lambda: _run_limit_up_scan(today, table))
    # 只有包含 [运行时错误] 才算真正的错误，进度信息是 stderr 正常输出
    has_runtime_error = "[运行时错误]" in err
    if has_runtime_error:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out, "mode": "table" if table else "detail"}


def _make_cache_entry(stocks, sentiment_score, sentiment_level, date_str):
    return {"ok": True, "stocks": stocks, "sentiment": {"score": sentiment_score, "level": sentiment_level}, "date": date_str, "fetched_at": _fetched_at()}

# ─── 原始数据缓存（分离「拉取」和「运行」） ───

_RAW_CACHE_VERSION = 7  # v6→v7: 同步 P0/P1/P2 评分逻辑大改 — 旧 raw_scan_data.pkl 里的 stock_sentiment/sector/danger/north_flow 字段都是旧公式算的, 不 bump 的话 daily 缓存命中后会用旧值传给新公式, 产生不一致分数
_RAW_CACHE_PATH = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
                                 "claude_stock_cache", "raw_scan_data.pkl")


def _save_raw_cache(filtered, fund_df, sentiment_score, sentiment_level, sentiment_detail,
                    sentiment_ok, history_scores, lhb_bonus, today_str,
                    **extra):
    """保存原始扫描数据，供「运行」读取。extra 可包含 limit_df, zhaban_df, dieting_df 等。"""
    try:
        import pickle as _pk
        os.makedirs(os.path.dirname(_RAW_CACHE_PATH), exist_ok=True)
        data = {
            'version': _RAW_CACHE_VERSION,
            'filtered': filtered,
            'fund_df': fund_df,
            'sentiment_score': sentiment_score,
            'sentiment_level': sentiment_level,
            'sentiment_detail': sentiment_detail,
            'sentiment_ok': sentiment_ok,
            'history_scores': history_scores,
            'lhb_bonus': lhb_bonus,
            'date': today_str,
            'fetched_at': _fetched_at(),
        }
        data.update(extra)
        with open(_RAW_CACHE_PATH, 'wb') as f:
            _pk.dump(data, f)
        print("  [缓存] 原始数据已保存", file=sys.stderr)
        return True
    except Exception as e:
        print(f"  [缓存] 保存失败: {e}", file=sys.stderr)
        return False

def _load_raw_cache():
    """加载原始扫描数据，供「运行」读取。过期或不存在返回 None。"""
    try:
        import pickle as _pk
        if not os.path.exists(_RAW_CACHE_PATH):
            print("  [缓存] 无原始数据，请先「拉取」", file=sys.stderr)
            return None
        # 缓存有效期：当天有效
        mtime = datetime.fromtimestamp(os.path.getmtime(_RAW_CACHE_PATH), _CST)
        now = datetime.now(_CST)
        if mtime.date() != now.date():
            print("  [缓存] 原始数据已过期（非当天），请重新拉取", file=sys.stderr)
            return None
        with open(_RAW_CACHE_PATH, 'rb') as f:
            data = _pk.load(f)
        # 版本校验：格式不匹配的旧缓存自动失效
        cache_ver = data.get('version', 0)
        if cache_ver < _RAW_CACHE_VERSION:
            print(f"  [缓存] 格式版本不匹配 (缓存v{cache_ver} < 当前v{_RAW_CACHE_VERSION})，自动拉取新数据", file=sys.stderr)
            os.remove(_RAW_CACHE_PATH)
            return None
        print("  [缓存] 原始数据加载成功", file=sys.stderr)
        return data
    except Exception as e:
        print(f"  [缓存] 加载失败: {e}", file=sys.stderr)
        return None


def _principal_filter(df, principal):
    """本金过滤：买不了 0.5 手的排除（不参与评分，直接过滤）"""
    import pandas as pd
    from scanner import _dynamic_positions
    n = _dynamic_positions(principal)
    position_size = principal / n
    price_col = '最新价' if '最新价' in df.columns else (df.columns[4] if len(df.columns) > 4 else df.columns[3])
    mask = pd.Series(True, index=df.index)
    for idx in df.index:
        price = float(df.loc[idx, price_col])
        lots = position_size / (price * 100)
        if lots < 2:  # 至少能买2手
            mask[idx] = False
    excluded = (~mask).sum()
    if excluded > 0:
        print(f"  [扫描] 本金过滤排除 {excluded} 只 (本金{principal}买不了2手)", file=sys.stderr)
    return df[mask]


def _scan_limit_up_data(today_str: str, principal: float = 20000, plan_name: str = None):
    """涨停扫描核心逻辑：拉取数据 + 过滤 + 调用评分方案"""
    from scanner import (fetch_limit_up_pool, pre_filter,
                         filter_by_price, can_buy_filter,
                         fetch_fund_flow_data, detect_market_sentiment,
                         analyze_dragon_tiger, score_stock_history)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from plans import get_plan
    import pandas as pd

    # ── 拉取数据（基础设施，所有plan共享） ──
    print("  [扫描] 第1步: 获取涨停池...", file=sys.stderr)
    pool = fetch_limit_up_pool()
    if pool is None or pool.empty:
        print("  [扫描] 无涨停数据", file=sys.stderr)
        return None

    print(f"  [扫描] 共 {len(pool)} 只, 第2步: 前置过滤...", file=sys.stderr)
    filtered = pre_filter(pool)
    if filtered.empty:
        print("  [扫描] 过滤后为空", file=sys.stderr)
        return None

    print(f"  [扫描] 剩余 {len(filtered)} 只, 第3步: 获取资金流...", file=sys.stderr)
    fund_df, _ = fetch_fund_flow_data()

    if fund_df is not None:
        print("  [扫描] 第4步: 股价过滤...", file=sys.stderr)
        filtered = filter_by_price(filtered, fund_df)
        if filtered.empty:
            print("  [扫描] 过滤后为空", file=sys.stderr)
            return None

    # 保存因子归一化基准集（过滤前，保证归一化不变）
    scoring_base = filtered.copy() if not filtered.empty else filtered

    # 可买到过滤（硬过滤，不改变归一化基准）
    print(f"  [扫描] 第5步: 可买到过滤...", file=sys.stderr)
    filtered = can_buy_filter(filtered)
    if filtered.empty:
        print("  [扫描] 可买到过滤后为空", file=sys.stderr)
        return None

    # 本金过滤（硬过滤，不改变归一化基准）
    filtered = _principal_filter(filtered, principal)
    if filtered.empty:
        print("  [扫描] 本金过滤后为空", file=sys.stderr)
        return None

    # 并行获取预测评分 + 按 Plan 声明拉取扩展数据源
    plan_obj = get_plan(plan_name)
    needed_sources = getattr(plan_obj, 'PLAN_SOURCES', [])
    from plans.datasource import SOURCES as EXT_SOURCES
    n_workers = 3 + len(needed_sources)
    print(f"  [扫描] 第6步: 并行获取预测评分 (Plan {plan_name or 'A'}, {len(needed_sources)} 个扩展源)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=max(4, n_workers)) as ex:
        futs = {
            ex.submit(detect_market_sentiment, today_str): "sentiment",
            ex.submit(analyze_dragon_tiger, filtered, today_str): "lhb",
            ex.submit(score_stock_history, filtered, today_str): "history",
        }
        for src_name in needed_sources:
            if src_name in EXT_SOURCES:
                futs[ex.submit(EXT_SOURCES[src_name], today_str)] = src_name
        res = {}
        for f in as_completed(futs):
            key = futs[f]
            try:
                res[key] = f.result()
            except Exception as e:
                print(f"  [情绪 future] {key} 失败: {e}", file=sys.stderr)

    sentiment_score, sentiment_level = 5.0, "未知"
    sentiment_detail = {}
    sentiment_ok = False
    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]
        sentiment_ok = True
    else:
        print("  [扫描] 市场情绪首次获取失败，重试中...", file=sys.stderr)
        try:
            retry = detect_market_sentiment(today_str)
            if retry:
                sentiment_score, sentiment_level, sentiment_detail = retry
                sentiment_ok = True
                print(f"  [扫描] 重试成功: {sentiment_level}", file=sys.stderr)
        except Exception as e2:
            print(f"  [扫描] 重试仍失败: {e2}", file=sys.stderr)

    lhb_bonus = res.get("lhb")
    lhb_bonus = lhb_bonus[0] if lhb_bonus else pd.Series(0.0, index=filtered.index)

    history_scores = res.get("history")
    history_scores = history_scores[0] if history_scores is not None else pd.Series(2.5, index=filtered.index)

    # 扩展数据源结果: 每个 source 的 DataFrame 直接从 res 提取
    source_data = {}
    for src_name in needed_sources:
        src_result = res.get(src_name)
        if src_result is not None:
            source_data[src_name] = src_result

    # 保存原始数据缓存（供「运行」按钮重跑评分用）
    _save_raw_cache(filtered, fund_df, sentiment_score, sentiment_level,
                    sentiment_detail, sentiment_ok, history_scores,
                    lhb_bonus, today_str, pool=pool, scoring_base=scoring_base,
                    **source_data)

    # ── 调用评分方案（因子在 scoring_base 上计算，输出用 filtered） ──
    print(f"  [扫描] 第7步: 调用评分方案 [{plan_name or '默认'}]...", file=sys.stderr)
    plan_inputs = {
        'filtered': filtered,
        'scoring_base': scoring_base,
        'fund_df': fund_df,
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_detail': sentiment_detail,
        'sentiment_ok': sentiment_ok,
        'history_scores': history_scores,
        'lhb_bonus': lhb_bonus,
        'today_str': today_str,
        'pool': pool,
        'principal': principal,
    }
    plan_inputs.update(source_data)  # DataFrames 直接注入 inputs
    result = plan_obj.score(plan_inputs)

    # 异步归档扫描输入 (供回测引擎历史回放)
    _archive_scan_inputs_async(today_str, fund_df, sentiment_score, sentiment_level,
                               sentiment_detail, sentiment_ok, lhb_bonus, history_scores)

    return result


def _archive_scan_inputs_async(today_str, fund_df, sentiment_score, sentiment_level,
                                sentiment_detail, sentiment_ok, lhb_bonus, history_scores):
    """异步保存扫描输入, 不阻塞主流程"""
    try:
        import threading
        from archiver import save_scan_inputs
        threading.Thread(target=lambda: save_scan_inputs(
            trade_date=today_str,
            fund_df=fund_df,
            sentiment_score=sentiment_score,
            sentiment_level=sentiment_level,
            sentiment_detail=sentiment_detail,
            sentiment_ok=sentiment_ok,
            lhb_bonus=lhb_bonus,
            history_scores=history_scores,
        ), daemon=True).start()
    except Exception:
        pass


def _scan_from_raw_cache(principal: float = 20000, plan_name: str = None):
    """从缓存的原始数据重跑评分逻辑。"""
    import pandas as pd
    raw = _load_raw_cache()
    if raw is None:
        return None

    filtered = raw['filtered']
    fund_df = raw['fund_df']
    sentiment_score = raw['sentiment_score']
    sentiment_level = raw['sentiment_level']
    sentiment_detail = raw['sentiment_detail']
    sentiment_ok = raw['sentiment_ok']
    history_scores = raw['history_scores']
    lhb_bonus = raw['lhb_bonus']

    # 本金过滤（硬过滤）
    filtered = _principal_filter(filtered, principal)
    if filtered.empty:
        print("  [运行] 本金过滤后为空", file=sys.stderr)
        return None

    pool = raw.get('pool')
    if pool is None:
        pool = filtered  # 旧缓存无pool→降级用filtered
    scoring_base = raw.get('scoring_base', filtered)  # 旧缓存无此字段→降级用filtered

    # ── 调用评分方案（因子在 scoring_base 上计算） ──
    from plans import get_plan
    plan = get_plan(plan_name)
    plan_inputs = {
        'filtered': filtered,
        'scoring_base': scoring_base,
        'fund_df': fund_df,
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_detail': sentiment_detail,
        'sentiment_ok': sentiment_ok,
        'history_scores': history_scores,
        'lhb_bonus': lhb_bonus,
        'today_str': raw['date'],
        'pool': pool,
        'principal': principal,
    }
    # 扩展数据源: 从 raw_scan_data.pkl 读取 DataFrame (旧缓存无此字段→None)
    for src_name in ['north_flow', 'margin_ratio', 'inst_rating', 'limit_reason',
                     'margin_akshare', 'north_flow_market', 'industry_fund_flow']:
        val = raw.get(src_name)
        if val is not None:
            plan_inputs[src_name] = val
    result = plan.score(plan_inputs)
    result['_from_cache'] = True

    _archive_scan_inputs_async(raw['date'], fund_df, sentiment_score, sentiment_level,
                               sentiment_detail, sentiment_ok, lhb_bonus, history_scores)

    return result


def _run_limit_up_scan(today_str: str, table_mode: bool):
    """涨停池扫描 — 文本输出"""
    from scanner import format_table_output, format_output
    import pandas as pd

    data = _scan_limit_up_data(today_str)
    if data is None or not data['stocks']:
        print("no_data")
        return

    fmt = format_table_output if table_mode else format_output
    print(fmt(
        data['df'], data['money_scores'], data['sector_mom'],
        data['seal_scores'], data['tech_scores'],
        raw_money=data['raw_money'],
        sentiment_score=data['sentiment_score'],
        sentiment_level=data['sentiment_level'],
        sentiment_detail=data['sentiment_detail'],
        history_scores=data['history_scores'],
        buyability_scores=data['buyability_scores'],
        sector_res_scores=data['sector_res'],
        stock_sentiment_scores=data.get('stock_sent_scores'),
    ))


def _fetch_three_pools(today: str, ak):
    """板块卡片专用: 一次性拉 3 个池 (涨停/炸板/跌停)"""
    print("  [板块卡片] 拉取涨停池...", file=sys.stderr)
    limit_df = ak.stock_zt_pool_em(date=today)
    print(f"  [板块卡片] 涨停 {len(limit_df)} 只, 拉取炸板池...", file=sys.stderr)
    zhaban_df = ak.stock_zt_pool_zbgc_em(date=today)
    print(f"  [板块卡片] 炸板 {len(zhaban_df)} 只, 拉取跌停池...", file=sys.stderr)
    dieting_df = ak.stock_zt_pool_dtgc_em(date=today)
    print(f"  [板块卡片] 跌停 {len(dieting_df)} 只, 计算板块得分...", file=sys.stderr)
    return (limit_df, zhaban_df, dieting_df)


def _cached_pool_loader(cache_key: str, loader, refresh: bool = False):
    """通用 pool -> data 加载器 (缓存 + 异常处理) - P1-3 重构
    返回 (data, from_cache, error_response):
        - data 不为 None: 拉到的数据 (可能来自缓存)
        - from_cache: True 表示从缓存读到的
        - error_response 不为 None: 端点应直接 return 它
    """
    if not refresh:
        cached = daily_get_pkl(cache_key)
        if cached is not None:
            return cached, True, None
    try:
        data = loader()
    except Exception as e:
        print(f"  [{cache_key}] 拉取失败: {e}", file=sys.stderr)
        return None, False, JSONResponse({"ok": False, "error": str(e), "items": []})
    # 空数据
    if data is None:
        return None, False, {"ok": True, "items": []}
    if hasattr(data, 'empty') and data.empty:
        return None, False, {"ok": True, "items": []}
    daily_set_pkl(cache_key, data, force=refresh)
    return data, False, None


@app.get("/api/scan/limit-up/cards")
def api_scan_limit_up_cards(refresh: bool = Query(False, description="强制刷新"),
                              principal: float = Query(20000, description="本金(元)"),
                              plan: str = Query(None, description="评分方案(A/B/...)")):
    """涨停扫描 — 返回结构化 JSON 数据（供卡片视图使用）
    缓存策略: 缓存 _scan_limit_up_data 返回的原始 dict(含 stocks/sentiment),
    每次用最新 _make_cache_entry 重新组装 items。改组装逻辑后直接 reload 即可。
    """
    plan_name = plan or None
    raw_key = make_key("app", "limit_up_raw", principal=int(principal), plan=plan_name or "default")

    print("  [涨停卡片] 开始扫描", file=sys.stderr)
    today = _today_trading()
    data, from_cache, err = _cached_pool_loader(
        raw_key,
        lambda: _scan_limit_up_data(today, principal=principal, plan_name=plan_name),
        refresh
    )
    if err:
        return err
    if data is None or not data.get('stocks'):
        return {"ok": True, "stocks": [], "sentiment": {}}
    print(f"  [涨停卡片] {'缓存命中' if from_cache else '完成'}, 共 {len(data['stocks'])} 只", file=sys.stderr)
    # 始终用最新 _make_cache_entry 重新组装 items
    return _make_cache_entry(data['stocks'], data['sentiment_score'],
                              data['sentiment_level'], data['date'])


@app.get("/api/scan/sector/cards")
def api_sector_cards(refresh: bool = Query(False, description="强制刷新")):
    """板块热度 — 结构化数据（增强版：含成分股 + 可跳转）
    缓存策略: 缓存 3 个原始 df(涨停/炸板/跌停池),items 每次用最新逻辑重算。
    改 items 组装逻辑后直接 reload 即可看到新结果。
    """
    import akshare as ak
    import pandas as pd
    from datetime import date
    print("  [板块卡片] 开始...", file=sys.stderr)
    today = _today_trading()
    raw_key = make_key("app", "sector_raw", date=today)

    # 一次性拉 3 个池 (limit_df / zhaban_df / dieting_df)
    pools, from_cache, err = _cached_pool_loader(
        raw_key,
        lambda: _fetch_three_pools(today, ak),
        refresh
    )
    if err:
        return err
    if pools is None:
        return {"ok": True, "items": []}
    limit_df, zhaban_df, dieting_df = pools

    # 统一板块评分
    from scanner import score_sector_data
    sector_stats = score_sector_data(limit_df, zhaban_df, dieting_df, top_n=15)

    # 行业列名（用于收集成分股）
    ind_col = None
    for c in limit_df.columns:
        if '行业' in str(c): ind_col = c; break

    items = []
    for s in sector_stats:
        lc = s['limit_cnt']; zc = s['zhaban_cnt']; dc = s['dieting_cnt']
        score = min(12, 4 + lc * 2)  # 保持和旧版一致的简分
        eff = s['seal_rate']  # 封板率

        # 收集成分股
        stock_list = []
        if ind_col:
            sector_stocks = limit_df[limit_df[ind_col] == s['industry']]
            for _, r in sector_stocks.head(20).iterrows():
                stock_list.append({
                    'code': str(r.iloc[1]).strip().zfill(6),
                    'name': str(r.iloc[2]),
                    'turnover': float(r.iloc[8]) if str(r.iloc[8]) != '--' else 0,
                    'seal_time': str(int(float(r.iloc[10]))) if pd.notna(r.iloc[10]) and r.iloc[10] != '--' else '0000',
                    'seal_fund': float(r.iloc[9]) if str(r.iloc[9]) != '--' else 0,
                })

        # ── 板块竞价条件 ──
        auction_parts = []
        if score >= 10: auction_parts.append('板块强势看高开')
        elif score >= 6: auction_parts.append('板块活跃可参与')
        else: auction_parts.append('板块弱势谨慎')
        if eff >= 80: auction_parts.append('封板率高>80%')
        elif eff >= 60: auction_parts.append('封板率中等')
        else: auction_parts.append('封板率<60%分歧大')
        if lc >= 3: auction_parts.append(f'{lc}只涨停共振')
        if dc >= 2: auction_parts.append(f'{dc}只跌停分歧大')
        if zc >= 2: auction_parts.append(f'{zc}只炸板分歧')
        # 龙头竞价
        if stock_list and stock_list[0].get('seal_time'):
            st = stock_list[0]['seal_time']
            if len(st) >= 4:
                hh = int(st[:2])
                if hh < 10: auction_parts.append('龙头早盘封可参与')
                elif hh < 13: auction_parts.append('龙头午前封一般')
                else: auction_parts.append('龙头午后封谨慎')
        auction_check = '；'.join(auction_parts)

        items.append({
            'name': s['industry'],
            'url': f"https://www.10jqka.com.cn/#/search/{s['industry']}",
            'limit_count': lc, 'zhaban_count': zc, 'dieting_count': dc,
            'score': score, 'efficiency': s['seal_rate'],
            'stocks': stock_list, 'sector_code': '', 'auction_check': auction_check,
        })
    result = {"ok": True, "items": items, "fetched_at": _fetched_at()}
    # items 始终用最新逻辑重算(raw 已 pkl 缓存,无需 daily_set)
    return result



def _fetch_trend_data(today, principal):
    """趋势扫描 — 拉取数据 + 过滤，返回 (trend_df, cols_dict, zhaban_codes, hot_industries, industry_counts, sector_top_chg)
    cards 和 stream 端点共享，保证数据源完全一致。
    industry_counts: 板块→今日涨停数(给"板块共振N只"标签用)
    sector_top_chg: 板块→板块内今日龙头股涨幅(给"龙头在线/退潮"标签用)
    """
    import akshare as ak
    import pandas as pd
    from datetime import datetime, timedelta
    from scanner import filter_non_main_board

    # _mode_stream_endpoint 用 _Cap 替换 sys.stderr 来捕获进度，
    # akshare 内部可能调用 stderr.fileno()/isatty() 等特殊方法，
    # _Cap 虽然通过 __getattr__ 代理，但为安全起见直接恢复原始 stderr。
    import sys as _sys
    _saved = _sys.stderr
    _sys.stderr = getattr(_sys, '__stderr__', _sys.stderr)

    # 1. 拉取当日强势池 (与回测引擎对齐: stock_zt_pool_strong_em, 66.7%胜率已验证)
    strong = pd.DataFrame()
    try:
        strong = ak.stock_zt_pool_strong_em(date=today)
    except Exception:  # BUG-5 修复: bare except → except Exception
        pass
    if strong.empty:
        _sys.stderr = _saved; return None, None, set(), set(), {}, {}

    # 2. 列索引
    cols = {
        'code': '代码' if '代码' in strong.columns else (strong.columns[1] if len(strong.columns) > 1 else strong.columns[0]),
        'name': '名称' if '名称' in strong.columns else (strong.columns[2] if len(strong.columns) > 2 else strong.columns[1]),
        'chg': '涨跌幅' if '涨跌幅' in strong.columns else (strong.columns[3] if len(strong.columns) > 3 else strong.columns[0]),
        'price': '最新价' if '最新价' in strong.columns else (strong.columns[4] if len(strong.columns) > 4 else strong.columns[0]),
        'vol': strong.columns[6] if len(strong.columns) > 6 else strong.columns[0],
        'turnover': '换手率' if '换手率' in strong.columns else (strong.columns[9] if len(strong.columns) > 9 else None),
        'industry': '所属行业' if '所属行业' in strong.columns else (strong.columns[15] if len(strong.columns) > 15 else None),
    }
    strong['涨幅'] = strong[cols['chg']].astype(float)

    # 3. 用回测引擎的 _score_trend (含过滤+评分, 与66.7%胜率一致)
    from backtest_engine import _score_trend as backtest_score_trend
    strong = backtest_score_trend(strong, today)
    if strong is None or strong.empty:
        _sys.stderr = _saved; return None, None, set(), set(), {}, {}
    prev = strong

    # 4. 风控数据：炸板池 + 今日热门板块（分开 try 防互相影响）
    zhaban_codes = set()
    hot_industries = set()
    industry_counts = {}  # industry -> 今日涨停数
    sector_top_chg = {}   # industry -> 板块内今日龙头涨幅
    try:
        zb_df = ak.stock_zt_pool_zbgc_em(date=today)
        if not zb_df.empty:
            zb_code_col = zb_df.columns[1] if len(zb_df.columns) > 1 else zb_df.columns[0]
            zhaban_codes = set(zb_df[zb_code_col].astype(str).str.zfill(6))
    except Exception as e:
        print(f"  [trend] 炸板池拉取失败: {e}", file=sys.stderr)
    try:
        lt_df = ak.stock_zt_pool_em(date=today)
        if not lt_df.empty:
            ind_col2 = '所属行业' if '所属行业' in lt_df.columns else (lt_df.columns[15] if len(lt_df.columns) > 15 else None)
            if ind_col2:
                vc = lt_df[ind_col2].value_counts()
                industry_counts = vc.to_dict()
                hot_industries = set(vc[vc >= 3].index)
                # 算每个板块今日涨幅 TOP1（龙头）
                chg_col_lt = '涨跌幅' if '涨跌幅' in lt_df.columns else (lt_df.columns[3] if len(lt_df.columns) > 3 else None)
                if chg_col_lt:
                    for ind_name, group in lt_df.groupby(ind_col2):
                        try:
                            sector_top_chg[ind_name] = float(group[chg_col_lt].astype(float).max())
                        except Exception as e_inner:
                            print(f"  [trend] 板块{ind_name}涨幅计算失败: {e_inner}", file=sys.stderr)
    except Exception as e:
        print(f"  [trend] 今日涨停池拉取失败: {e}", file=sys.stderr)

    # 5. 趋势过滤
    df = prev[(prev['涨幅'] >= 2) & (prev['涨幅'] < 9)].copy()
    if df.empty:
        _sys.stderr = _saved; return None, cols, zhaban_codes, hot_industries, industry_counts, sector_top_chg
    df = df.sort_values('涨幅', ascending=False).head(30)  # 扩大到30, 避免低涨幅高分股被挤出候选池
    _sys.stderr = _saved; return df, cols, zhaban_codes, hot_industries, industry_counts, sector_top_chg


def _build_trend_items(trend, cols, zhaban_codes, hot_industries,
                        industry_counts=None, sector_top_chg=None):
    """趋势扫描股票评分 — 共享逻辑，cards 和 stream 端点统一调用
    industry_counts: 板块→今日涨停数 (给"板块共振N只"标签)
    sector_top_chg: 板块→板块内今日龙头涨幅 (给"龙头在线/退潮/本板块龙头"标签)
    """
    import pandas as pd
    code_col, name_col = cols['code'], cols['name']
    price_col, turnover_col = cols['price'], cols['turnover']
    industry_col = cols['industry']
    items = []
    for _, row in trend.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        chg = round(float(row['涨幅']), 1)
        price = float(row[price_col])
        turnover = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
        # 成交额(强势池有'成交额'列)
        amount_col = '成交额' if '成交额' in trend.columns else None
        volume = float(row.get(amount_col, 0) or 0) if amount_col else 0
        industry = str(row[industry_col]) if industry_col and pd.notna(row[industry_col]) else ''

        # 使用回测引擎的 _score_trend 动量评分 (与66.7%胜率一致)
        momentum_score = float(row.get('动量评分', 0))

        risk_tags = []
        if code in zhaban_codes:
            risk_tags.append("⚠️ 上交易日炸板")
        if turnover > 25:
            risk_tags.append("⚠️ 换手过高")

        risk_score = max(0, min(100, momentum_score))
        signals_prefix = "动量趋势"
        signals = [signals_prefix]
        if turnover > 15: signals.append("高换手")
        elif turnover > 8: signals.append("放量健康")

        # ── 量比(近5日涨停日均量对比) ──
        if industry_col or 'code' in cols:
            try:
                from archiver import _calc_volume_5d_avg
                today_str = _today_trading()
                cur_vol = float(row[vol_col]) if vol_col and pd.notna(row.get(vol_col)) else 0
                avg_vol, days = _calc_volume_5d_avg(code, today_str, lookback_days=5)
                if avg_vol and cur_vol > 0 and days >= 1:
                    ratio = cur_vol / avg_vol
                    if ratio >= 2.0:    signals.append(f'放量{ratio:.1f}x')
                    elif ratio >= 1.3:  signals.append(f'温和放量{ratio:.1f}x')
                    elif ratio <= 0.5:  signals.append(f'缩量{ratio:.1f}x')
            except Exception: pass

        # ── 龙头状态(基于今日板块龙头表现) ──
        if sector_top_chg and industry in sector_top_chg:
            top_chg = sector_top_chg[industry]
            if abs(chg - top_chg) < 0.1:    # 这条涨幅 == 龙头涨幅 → 自己就是龙头
                signals.append('板块龙头')
            elif top_chg >= 3:
                signals.append('龙头在线')
            elif top_chg <= -3:
                signals.append('龙头退潮')

        # ── 同板块共振(今日同板块涨停数) ──
        if industry_counts and industry in industry_counts:
            cnt = industry_counts[industry]
            if cnt >= 3:
                signals.append(f'板块共振{cnt}只')

        signals.extend(risk_tags)

        if risk_score >= 60: advice = "趋势健康，持有为主"
        elif risk_score >= 50: advice = "趋势尚可，控制仓位"
        elif risk_score >= 40: advice = "信号偏弱，轻仓或观望"
        else: advice = "多风险信号，不建议持有"

        # ── 竞价条件 ──
        auction_parts = []
        if risk_score >= 60: auction_parts.append('高开1-3%延续')
        elif risk_score >= 50: auction_parts.append('平开或小幅波动')
        else: auction_parts.append('低开1-2%弱势')
        if 5 <= turnover <= 15: auction_parts.append('竞价量>上交易日5%')
        elif turnover > 15: auction_parts.append('竞价量>上交易日8%')
        else: auction_parts.append('竞价量>上交易日3%')
        if hot_industries and industry in hot_industries:
            auction_parts.append('板块跟得上')
        else:
            auction_parts.append('板块退潮谨慎')
        if code in zhaban_codes: auction_parts.append('上交易日炸板不破上交易日最低')
        if turnover > 25: auction_parts.append('换手过高警惕分歧')
        if 2 <= chg <= 4: auction_parts.append('低涨幅延续')
        auction_check = '；'.join(auction_parts)

        items.append({
            'code': code, 'name': str(row[name_col]),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'change_pct': chg, 'price': price, 'turnover': round(turnover, 1),
            'volume': round(volume / 1e8, 2) if volume > 1e8 else round(volume / 1e4, 0) if volume > 0 else 0,
            'volume_unit': '亿' if volume > 1e8 else '万' if volume > 0 else '',
            'industry': industry, 'consecutive': 0,
            'composite_score': risk_score, 'total_score': risk_score,
            'signals': signals, 'advice': advice, 'auction_check': auction_check, 'risk_score': risk_score,
        })

    # 动态门槛: 只显示最高分60%以上的 (权重调整后自动适配)
    if items:
        top_score = max(x['risk_score'] for x in items)
        threshold = max(10, top_score * 0.6)
        items = [x for x in items if x['risk_score'] >= threshold]
    items.sort(key=lambda x: -x['risk_score'])
    return items[:10]


@app.get("/api/scan/trend/cards")
def api_trend_cards(refresh: bool = Query(False, description="强制刷新"),
                    principal: float = Query(20000, description="本金(元)")):
    """趋势扫描 — 结构化数据（含量价分析、板块、跳转）
    缓存策略: 缓存原始数据(akshare df),每次用最新 _build_trend_items 重算。
    改评分逻辑后无需 bump _CACHE_VER,直接 reload 即可看到新结果。
    """
    print("  [趋势卡片] 开始...", file=sys.stderr)
    today = _today_trading()
    # 缓存键含权重哈希 — 权重变了自动刷新
    try:
        from weight_manager import load_trend_weights
        tw = load_trend_weights()
        w_hash = hash(tuple(sorted(tw.items()))) % 10000
    except Exception:
        w_hash = 0
    raw_key = make_key("app", "trend_raw", date=today, principal=int(principal), w=w_hash)

    cached_data, from_cache, err = _cached_pool_loader(
        raw_key,
        lambda: _fetch_trend_data(today, principal),
        refresh
    )
    if err:
        return err
    if cached_data is None:
        return {"ok": True, "items": []}
    (trend, cols, zhaban_codes, hot_industries, industry_counts, sector_top_chg) = cached_data
    if trend is None or trend.empty:
        return {"ok": True, "items": []}

    # 始终用最新 _build_trend_items 逻辑重算 items
    items = _build_trend_items(trend, cols, zhaban_codes, hot_industries,
                                industry_counts=industry_counts, sector_top_chg=sector_top_chg)
    return {"ok": True, "items": items, "fetched_at": _fetched_at()}


@app.get("/api/scan/reversal/cards")
def api_reversal_cards(refresh: bool = Query(False, description="强制刷新")):
    """涨停回调反转扫描 — 上交易日涨停今回调→明日反包潜力"""
    import akshare as ak; import pandas as pd
    from scanner import filter_non_main_board, filter_xr_xd_dr

    today = _today_trading()
    print("  [反转扫描] 开始...", file=sys.stderr)
    try:
        from weight_manager import load_reversal_weights
        rw = load_reversal_weights()
        rev_hash = hash(tuple(sorted(rw.items()))) % 10000
    except Exception:
        rev_hash = 0
    raw_key = make_key("app", "reversal_raw", date=today, w=rev_hash)
    prev, from_cache, err = _cached_pool_loader(
        raw_key,
        lambda: ak.stock_zt_pool_previous_em(date=today),
        refresh
    )
    if err:
        return err
    if prev is None or prev.empty:
        return {"ok": True, "items": []}

    df = filter_non_main_board(prev)
    if df.empty:
        return {"ok": True, "items": []}

    chg_col = '涨跌幅' if '涨跌幅' in df.columns else (df.columns[3] if len(df.columns) > 3 else df.columns[0])
    code_col = '代码' if '代码' in df.columns else (df.columns[1] if len(df.columns) > 1 else df.columns[0])
    name_col = '名称' if '名称' in df.columns else (df.columns[2] if len(df.columns) > 2 else df.columns[1])
    df = filter_xr_xd_dr(df, name_col=name_col)
    if df.empty:
        return {"ok": True, "items": []}
    price_col = df.columns[4] if len(df.columns) > 4 else df.columns[0]
    turnover_col = '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None)
    ind_col = df.columns[15] if len(df.columns) > 15 else None
    seal_stat_col = df.columns[14] if len(df.columns) > 14 else None

    df['今日涨幅'] = df[chg_col].astype(float)
    pullback = df[(df['今日涨幅'] >= -7) & (df['今日涨幅'] <= 1)].copy()
    if pullback.empty:
        return {"ok": True, "items": []}

    # 今日涨停行业
    hot_inds = set()
    try:
        lt = ak.stock_zt_pool_em(date=today)
        if lt is not None and not lt.empty and ind_col:
            lt_ic = '所属行业' if '所属行业' in lt.columns else (lt.columns[15] if len(lt.columns) > 15 else None)
            if lt_ic:
                hot_inds = set(lt[lt_ic].value_counts().head(10).index)
    except Exception as e:
        print(f"  [zhaban] 今日涨停行业拉取失败: {e}", file=sys.stderr)

    # 用回测引擎评分 (P5: 可调权)
    from scanner import _score_reversal as backtest_score_reversal
    try:
        from weight_manager import load_reversal_weights
        rev_w = load_reversal_weights()
    except Exception:
        rev_w = None
    scored = backtest_score_reversal(pullback, today_str=today, weights=rev_w)
    if scored is None or scored.empty:
        return {"ok": True, "items": []}
    pullback = scored

    items = []
    for _, row in pullback.iterrows():
        code = str(row.get('代码', row[code_col])).strip().zfill(6)
        name = str(row.get('名称', row[name_col]))
        chg = round(float(row.get('今日涨幅', row[chg_col])), 1)
        price = float(row.get('最新价', row[price_col]))
        to = float(row.get('换手率', row[turnover_col])) if pd.notna(row.get('换手率', row.get(turnover_col, 0))) else 0
        ind = str(row.get('所属行业', row.get(ind_col, ''))) if ind_col else ''
        lb = 0
        s = float(row.get('反转评分', 0))
        # 建议 (基于回测评分)
        if s >= 80: adv = '⭐ 高换手+多连板，反包潜力大'
        elif s >= 65: adv = '加入自选，竞价确认方向'
        elif s >= 45: adv = '观望，等放量信号'
        else: adv = '暂不参与'

        # 竞价条件
        auction_parts = []
        if s >= 85: auction_parts.append('高开3-5%')
        elif s >= 65: auction_parts.append('高开1-3%')
        else: auction_parts.append('平开或高开1%内')
        if to > 25: auction_parts.append('竞价量>上交易日5%')
        elif to > 15: auction_parts.append('竞价量>上交易日3%')
        elif to > 5: auction_parts.append('竞价量>上交易日2%')
        else: auction_parts.append('竞价量>上交易日1%')
        # 3) 板块联动
        if ind in hot_inds: auction_parts.append('板块今日涨停跟得上')
        else: auction_parts.append('板块离线，谨慎参与')
        # 4) 不破上交易日最低
        auction_parts.append('不破上交易日最低')
        # 5) 连板提示
        if lb >= 2: auction_parts.append(f'{lb}板强势品种')
        # 6) 浅回调加分
        if -3 <= chg <= 0.5: auction_parts.append('浅回调洗盘')

        tags = []
        if lb == 1: tags.append('首板回调')
        elif lb == 2: tags.append('二板回调')
        if -2 <= chg <= 0.5: tags.append('浅回调洗盘')
        if ind in hot_inds: tags.append('板块在线')
        if to >= 5: tags.append('放量承接')

        items.append({
            'code': code, 'name': name, 'url': f'https://stockpage.10jqka.com.cn/{code}/',
            'change_pct': chg, 'price': price, 'turnover': round(to, 1),
            'consecutive': lb, 'industry': ind,
            'score': s, 'composite_score': s, 'total_score': s,
            'signals': tags, 'advice': adv, 'auction_check': '；'.join(auction_parts),
            'risk_score': s,
        })

    items.sort(key=lambda x: -x['risk_score'])
    result = {"ok": True, "items": items[:10], "fetched_at": _fetched_at()}
    return result


def _zhaban_columns(df):
    """检测炸板相关列索引，名称匹配优先 + fallback 长度保护"""
    return {
        'st': '首次封板时间' if '首次封板时间' in df.columns else (df.columns[11] if len(df.columns) > 11 else None),
        'sf': '封板资金' if '封板资金' in df.columns else (df.columns[14] if len(df.columns) > 14 else None),
        'zb': '炸板次数' if '炸板次数' in df.columns else (df.columns[12] if len(df.columns) > 12 else None),
        'to': '换手率' if '换手率' in df.columns else (df.columns[9] if len(df.columns) > 9 else None),
        'ind': '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None),
        'code': '代码' if '代码' in df.columns else (df.columns[1] if len(df.columns) > 1 else df.columns[0]),
        'name': '名称' if '名称' in df.columns else (df.columns[2] if len(df.columns) > 2 else df.columns[1]),
        'price': df.columns[4] if len(df.columns) > 4 else df.columns[0],
    }


@app.get("/api/scan/zhaban/cards")
def api_zhaban_cards(refresh: bool = Query(False, description="强制刷新")):
    """炸板反包 — 结构化数据（含评分、信号分析、策略、跳转）
    缓存策略: 缓存炸板池原始 df,items 每次用最新评分逻辑重算。
    改评分逻辑后直接 reload 即可看到新结果。
    数据源: ak.stock_zt_pool_zbgc_em (炸板池) + score_zhaban_data
    """
    import akshare as ak
    import pandas as pd
    from datetime import date
    from scanner import filter_non_main_board, filter_xr_xd_dr
    print("  [炸板卡片] 开始...", file=sys.stderr)
    today = _today_trading()
    raw_key = make_key("app", "zhaban_raw", date=today)

    zb, from_cache, err = _cached_pool_loader(
        raw_key,
        lambda: ak.stock_zt_pool_zbgc_em(date=today),
        refresh
    )
    if err:
        return err
    if zb is None or (hasattr(zb, 'empty') and zb.empty):
        return {"ok": True, "items": []}

    # 过滤 (与 /api/scan/zhaban 一致: ST/北交/科创/创业板 + 流通市值 + 价格)
    df = zb.copy()
    df = filter_non_main_board(df)
    df = filter_xr_xd_dr(df)
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= 200 * 1e8]
    if len(df.columns) > 4:
        df = df[df.iloc[:, 4].astype(float) <= 60]
    if df.empty:
        return {"ok": True, "items": []}

    # 评分 (炸板反包评分函数, 与回测权重对齐)
    from scanner import score_zhaban_data
    try:
        from weight_manager import _load_tab_weights
        w = _load_tab_weights('zhaban')
    except Exception:
        w = None
    scored = score_zhaban_data(df, today, weights=w)

    # 列识别 (与 /api/scan/zhaban/stream 共享逻辑)
    zc = _zhaban_columns(scored)

    items = []
    for _, row in scored.iterrows():
        code = str(row.get(zc['code'], '') or row.iloc[0]).strip().zfill(6)
        name = str(row.get(zc['name'], '') or row.iloc[0])
        seal_time = str(row.get(zc['st'], ''))[:4]
        total = float(row.get('总分', 0))
        price = float(row.get(zc['price'], 0)) if pd.notna(row.get(zc['price'], None)) else 0
        turnover = round(float(row.get(zc['to'], 0) or 0), 1)
        zb_times = int(float(row.get(zc['zb'], 0))) if pd.notna(row.get(zc['zb'], None)) else 0
        seal_fund = float(row.get(zc['sf'], 0) or 0)
        net = float(row.get('净流入', 0))
        industry = str(row.get(zc['ind'], ''))

        # 信号标签
        sigs = []
        if seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 10: sigs.append('早盘封板')
        elif seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 11: sigs.append('上午封板')
        else: sigs.append('午后封板')
        if zb_times > 0: sigs.append(f'炸板{zb_times}次')
        if net > 1e8: sigs.append('资金承接强')
        elif net > 0: sigs.append('有资金承接')
        else: sigs.append('资金流出')
        if 8 <= turnover <= 20: sigs.append('换手适中')
        elif turnover > 20: sigs.append('高换手')

        # 建议
        if total >= 70: advice = '反包潜力高，竞价高开放量可参与'
        elif total >= 50: advice = '竞价观察，高开放量可博弈反包'
        elif total >= 35: advice = '仅观望，需竞价放量确认'
        else: advice = '不参与'

        # 竞价条件
        auction_parts = []
        if seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 10: auction_parts.append('早盘封可参与')
        elif seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 13: auction_parts.append('午前封一般')
        else: auction_parts.append('午后封谨慎')
        if net > 1e8: auction_parts.append('资金强承接')
        elif net > 0: auction_parts.append('有资金承接')
        else: auction_parts.append('资金流出谨慎')
        if 8 <= turnover <= 20: auction_parts.append('换手适中')
        elif turnover > 20: auction_parts.append('换手偏高')
        if zb_times >= 2: auction_parts.append(f'炸板{zb_times}次分歧')

        items.append({
            'code': code, 'name': name, 'score': round(total), 'price': price,
            'seal_time': seal_time, 'turnover': turnover, 'seal_fund': seal_fund,
            'zhaban_times': zb_times, 'industry': industry, 'net_money': round(net, 0),
            'signals': sigs, 'advice': advice, 'auction_check': '；'.join(auction_parts),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })

    # items 始终用最新逻辑重算(raw 已 pkl 缓存,无需 daily_set)
    return {"ok": True, "items": items[:10], "fetched_at": _fetched_at()}





@app.get("/api/scan/zhaban")
def api_scan_zhaban(table: bool = Query(False)):
    """炸板股反包潜力扫描"""
    from scanner import scan_zhaban
    today = _today_trading()
    out, err = _capture(scan_zhaban, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/trend")
def api_scan_trend(table: bool = Query(False)):
    """趋势动量股扫描"""
    from scanner import scan_trend
    today = _today_trading()
    out, err = _capture(scan_trend, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/sector")
def api_scan_sector(table: bool = Query(False)):
    """板块联动强度分析"""
    from scanner import scan_sector
    today = _today_trading()
    out, err = _capture(scan_sector, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/dtqiaoban")
def api_scan_dtqiaoban(table: bool = Query(False)):
    """跌停翘板信号扫描"""
    from scanner import scan_dtqiaoban
    today = _today_trading()
    out, err = _capture(scan_dtqiaoban, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/dtqiaoban/cards")
def api_dtqiaoban_cards(refresh: bool = Query(False, description="强制刷新")):
    """跌停翘板 — 结构化卡片数据 (与 stream 端点共享逻辑, 走缓存池 + 评分)"""
    today = _today_trading()
    raw_key = make_key("app", "dtqiaoban_raw", date=today)

    def run():
        import akshare as ak
        import pandas as pd
        from scanner import filter_non_main_board, filter_xr_xd_dr, score_dtqiaoban_data
        dt = ak.stock_zt_pool_dtgc_em(date=today)
        if dt.empty: return [], {}
        df = filter_non_main_board(dt)
        df = filter_xr_xd_dr(df)
        if len(df.columns) > 6 and '流通市值' in df.columns:
            df = df[df['流通市值'].astype(float) <= 200 * 1e8]
        elif len(df.columns) > 6:
            df = df[df.iloc[:, 6].astype(float) <= 200 * 1e8]
        if df.empty: return [], {}
        try:
            from weight_manager import _load_tab_weights
            w = _load_tab_weights('dtqiaoban')
        except Exception:
            w = None
        scored = score_dtqiaoban_data(df, weights=w)
        items = []
        for _, row in scored.iterrows():
            code = str(row.iloc[1]).strip().zfill(6)
            total = round(row.get('翘板评分', 0))
            turn_val = float(row.iloc[9]) if len(row) > 9 and pd.notna(row.iloc[9]) else 0
            seal_val = float(row.iloc[10]) if len(row) > 10 and pd.notna(row.iloc[10]) else 0
            cont_val = int(float(row.iloc[13])) if len(row) > 13 and pd.notna(row.iloc[13]) else 0
            st = str(row.iloc[11]) if len(row) > 11 and pd.notna(row.iloc[11]) else ''
            price = float(row.iloc[4]) if pd.notna(row.iloc[4]) else 0
            sigs = []
            if total >= 60: sigs.append('高信号')
            elif total >= 35: sigs.append('中等信号')
            else: sigs.append('弱信号')
            if turn_val > 10: sigs.append('高换手承接')
            if cont_val >= 3: sigs.append(f'N{cont_val}板超跌')
            advice = '竞价观察，放量高开可博弈反抽' if total >= 60 else ('仅观望' if total >= 35 else '不参与')
            items.append({'code': code, 'name': str(row.iloc[2]),
                          'score': total, 'price': price,
                          'change': float(row.iloc[3]) if pd.notna(row.iloc[3]) else -10,
                          'seal_time': st[:4] if len(st) >= 4 else st,
                          'turnover': turn_val, 'seal_fund': seal_val, 'consecutive': cont_val,
                          'signals': sigs, 'advice': advice,
                          'url': f"https://stockpage.10jqka.com.cn/{code}/"})
        return items[:10], {}

    items, meta = run()
    if not items:
        return {"ok": True, "items": [], "fetched_at": _fetched_at()}
    return {"ok": True, "items": items, "fetched_at": _fetched_at()}


@app.get("/api/backtest")
def api_backtest(tab: str = Query('limit-up', description="回测 tab"),
                  days: int = Query(7, description="回测天数 (默认 7)")):
    """运行滚动回测 (P6: 支持多 tab + 自定义天数)"""
    from scanner import run_backtest
    out, err = _capture(run_backtest, tab, days)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out, "tab": tab, "days": days}


# ─── P6: 多 Tab T+1 真实回测 API (结构化 JSON) ───
from backtest_engine import run_tab_backtest, TAB_LIMIT_UP, ALL_TABS


from fastapi import Path as FastAPIPath

@app.get("/api/bt/{tab}")
def api_backtest_tab(tab: str = FastAPIPath(..., pattern=r"^(limit-up|trend|zhaban|dtqiaoban|reversal|sector)$"),
                      days: int = Query(7, description="回测天数 (默认 7)"),
                      top_n: int = Query(3, description="每日 TOP N"),
                      capital: float = Query(30000, description="单笔本金")):
    """P6: 多 Tab T+1 真实回测 (结构化 JSON)

    支持 tab: limit-up / trend / zhaban / dtqiaoban / reversal / sector
    注: 端点改为 /api/bt/{tab} 避开与 /api/backtest/{保留词} 冲突
    """
    try:
        result = run_tab_backtest(tab=tab, max_days=days, top_n=top_n, capital=capital)
        return {
            "ok": True,
            "tab": tab,
            "summary": result.get("summary", {}),
            "summary_30d": result.get("summary_30d", {}),
            "trades": result.get("trades", []),
            "top5": result.get("top5", []),
            "bottom5": result.get("bottom5", []),
            "skipped": result.get("skipped", []),
            "comparison": result.get("comparison", {}),
            "config": result.get("config", {}),
            "generated_at": result.get("generated_at"),
            "error": result.get("error"),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "tab": tab, "error": str(e)[:200]})


@app.get("/api/bt/{tab}/top")
def api_backtest_tab_top(tab: str,
                          days: int = Query(7, description="回测天数")):
    """P6: 多 Tab T+1 回测 TOP5 (快速版, 用于前端卡片展示)"""
    if tab not in ALL_TABS:
        return JSONResponse({"ok": False, "error": f"未知 tab: {tab}"})
    try:
        result = run_tab_backtest(tab=tab, max_days=days, top_n=3, capital=30000)
        top5 = result.get("top5", [])
        bot5 = result.get("bottom5", [])
        return {
            "ok": True,
            "tab": tab,
            "summary": result.get("summary", {}),
            "top5": [
                {"code": t.get("code"), "name": t.get("name"),
                 "score": t.get("score"), "net_ret_pct": t.get("net_ret_pct"),
                 "signal_date": t.get("signal_date")}
                for t in top5
            ],
            "bottom5": [
                {"code": t.get("code"), "name": t.get("name"),
                 "score": t.get("score"), "net_ret_pct": t.get("net_ret_pct"),
                 "signal_date": t.get("signal_date")}
                for t in bot5
            ],
        }
    except Exception as e:
        return JSONResponse({"ok": False, "tab": tab, "error": str(e)[:200]})


@app.get("/api/bt/{tab}/full")
def api_backtest_tab_full(tab: str,
                           days: int = Query(30, description="回测天数"),
                           top_n: int = Query(3, description="每日 TOP N"),
                           min_score: float = Query(50.0, description="最低评分门槛(低于此分不买)"),
                           sell_n: int = Query(3, description="卖出日偏移(2=T+2,3=T+3,4=T+4,5=T+5)"),
                           capital: float = Query(30000, description="单笔本金"),
                           force: bool = Query(False, description="强制重算(跳过缓存)"),
                           strategy: str = Query(None, description="策略过滤器: trend-elite/limit-sweet/limit-prime"),
                           smart_mode: bool = Query(False, description="SmartExit 智能 T+n 卖出 (P2.0)")):
    """P6: 单 tab 完整回测面板 — 一次返回回测+因子权重+调权历史

    cache key 包含 end_date (前一个 completed 交易日)
    → 休盘后整天 end 不变, 命中 cache, 30ms 返
    → 新一天 (新 completed 交易日) cache miss, 重算
    → force=true 跳过缓存强制重算
    → strategy=trend-elite/limit-sweet/limit-prime 启用策略过滤器
    → smart_mode=true 启用 SmartExit 智能 T+n 决策 (多维度信号)
    """
    if tab not in ALL_TABS:
        return JSONResponse({"ok": False, "error": f"未知 tab: {tab}"})
    try:
        result = run_tab_backtest(tab=tab, max_days=days, top_n=top_n, min_score=min_score, sell_n=sell_n,
                                   capital=capital, use_cache=not force, strategy=strategy,
                                   smart_mode=smart_mode)

        # 因子权重 + 调权历史
        try:
            from weight_manager import get_tab_weight_summary
            weights = get_tab_weight_summary(tab)
        except Exception:
            weights = {'factors': [], 'history': []}

        # tab 仓位权重
        try:
            from weight_manager import compute_tab_weights
            all_tw = compute_tab_weights()
            pos_weight = next((w['weight'] for w in all_tw if w['tab'] == tab), 0.5)
            pos_label = next((w['label'] for w in all_tw if w['tab'] == tab), '')
        except Exception:
            pos_weight = 0.5
            pos_label = ''

        # 可用天数
        try:
            from backtest_engine import _detect_available_days
            days_avail = _detect_available_days(tab)
        except Exception:
            days_avail = 0

        return {
            "ok": True, "tab": tab,
            "backtest": {
                "summary": result.get("summary", {}),
                "summary_30d": result.get("summary_30d", {}),
                "trades": result.get("trades", []),
                "top5": result.get("top5", []),
                "bottom5": result.get("bottom5", []),
                "comparison": result.get("comparison", {}),
                "skipped": result.get("skipped", []),
                "config": result.get("config", {}),
            },
            "weights": weights,
            "tab_info": {
                "days_available": days_avail,
                "win_rate": result.get("summary", {}).get("win_rate", 0),
                "ev": result.get("summary", {}).get("ev", 0),
                "position_weight": pos_weight,
                "position_label": pos_label,
            },
        }
    except Exception as e:
        return JSONResponse({"ok": False, "tab": tab, "error": str(e)[:200]})


@app.get("/api/signal/tomorrow")
def api_signal_tomorrow(
    zhaban_top_n: int = Query(3, description="炸板TOP-N"),
    zhaban_min_score: float = Query(50, description="炸板最低评分"),
    zhaban_sell_n: int = Query(5, description="炸板持仓天数"),
    limit_up_top_n: int = Query(3, description="涨停TOP-N"),
    limit_up_min_score: float = Query(38, description="涨停最低评分"),
    trend_top_n: int = Query(1, description="趋势TOP-N"),
    trend_min_score: float = Query(45, description="趋势最低评分"),
):
    """明日买入信号 — 接受回测面板参数覆盖默认值"""
    try:
        from signal_tomorrow import generate_signals
        settings = {
            'zhaban': {'top_n': zhaban_top_n, 'min_score': zhaban_min_score,
                       'sell_n': zhaban_sell_n},
            'limit-up': {'top_n': limit_up_top_n, 'min_score': limit_up_min_score},
            'trend': {'top_n': trend_top_n, 'min_score': trend_min_score},
        }
        result = generate_signals(settings=settings)
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.get("/api/community")
def api_community(top_n: int = Query(10, description="分析前N只股票")):
    """舆情监测 — 股吧/雪球/新闻情感分析"""
    import community
    from scanner import fetch_limit_up_pool, pre_filter

    out_buf = io.StringIO()
    err = ""
    try:
        df = fetch_limit_up_pool()
        if df is None or df.empty:
            return {"ok": True, "output": "今日无涨停数据，无法进行舆情分析", "error": ""}

        txt, smap = community.run(df, top_n)
        out_buf.write(txt if txt.strip() else "暂无舆情数据")
    except Exception as e:
        err = str(e)
        out_buf.write(f"\n[错误] {e}")

    return {"ok": not bool(err), "output": out_buf.getvalue(), "error": err}


@app.get("/api/indicators/cards")
def api_indicators_cards(refresh: bool = Query(False, description="强制刷新")):
    from indicators import run_enhanced, calc_seal_ratio, calc_sector_leadership, calc_sector_leader_label
    from scanner import fetch_limit_up_pool, pre_filter
    from datetime import date
    import pandas as pd
    today = _today_trading()
    df = fetch_limit_up_pool()
    if df is None or df.empty:
        return {"ok": True, "items": []}
    df = pre_filter(df)
    result, details = run_enhanced(df, today_str=today)
    seal_ratios = details.get('seal_ratios', {})
    leadership = details.get('leadership', {})
    vol_ratios = details.get('vol_ratios', {})
    pos_types = details.get('pos_types', {})
    lhb_data = details.get('lhb_data', {})
    items = []
    for idx in df.index:
        code = str(df.loc[idx, df.columns[1]]).strip().zfill(6)
        name = str(df.loc[idx, df.columns[2]])
        sr = float(seal_ratios.get(idx, 0))
        lead = str(leadership.get(idx, ''))
        vol = vol_ratios.get(idx, {}) if isinstance(vol_ratios.get(idx), dict) else {}
        pos = str(pos_types.get(idx, ''))
        lhb = lhb_data.get(code, {})
        sigs = []
        if sr is not None:
            sigs.append(f"封成比{sr:.1f}")
        if lead: sigs.append(lead)
        if pos: sigs.append(f"仓位:{pos}")
        if lhb.get('level') == 'warn': sigs.append("龙虎榜预警")
        money = 0
        money_str = ''
        try:
            money = float(lhb.get('net', 0))
            if abs(money) >= 1e8:
                money_str = f"{money/1e8:.2f}亿"
            elif abs(money) >= 1e4:
                money_str = f"{money/1e4:.0f}万"
            else:
                money_str = str(int(money))
        except Exception:  # BUG-5 修复: bare except → except Exception
            pass
        items.append({
            'code': code,
            'name': name,
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'seal_ratio': round(sr, 2) if sr is not None else None,
            'leadership': lead,
            'vol_ratio': vol,
            'position': pos,
            'lhb_net': money,
            'lhb_net_str': money_str,
            'lhb_level': lhb.get('level', ''),
            'lhb_detail': lhb.get('text', ''),
            'signals': sigs,
        })
    items.sort(key=lambda x: x.get('seal_ratio') or 0, reverse=True)
    return {"ok": True, "items": items[:10], "fetched_at": _fetched_at()}

@app.get("/api/indicators")
def api_indicators():
    """运行增强指标分析（龙虎榜等）"""
    from indicators import run_enhanced
    from scanner import fetch_limit_up_pool, pre_filter
    from datetime import date

    out_buf = io.StringIO()
    try:
        df = fetch_limit_up_pool()
        if df is None or df.empty:
            return {"ok": False, "output": "今日无涨停数据", "error": ""}

        df = pre_filter(df)
        today = _today_trading()
        result, _ = run_enhanced(df, today_str=today)
        out_buf.write(result)
        err = ""
    except Exception as e:
        err = str(e)
        out_buf.write(f"[错误] {e}")

    return {"ok": not bool(err), "output": out_buf.getvalue(), "error": err}


@app.get("/api/sentiment")
def api_sentiment():
    """市场情绪检测"""
    from scanner import detect_market_sentiment
    from datetime import date
    today = _today_trading()
    score, level, details = detect_market_sentiment(today)
    lines = [
        f"市场情绪: {level}",
        f"综合评分: {score}/10",
    ]
    if details and isinstance(details, dict):
        for k, v in details.items():
            lines.append(f"  {k}: {v}")
    output = "\n".join(lines)
    return {"ok": True, "output": output}


# ═══════════════════════════════════════════
#  v2.0: 盘前多空信号 + 北向资金 + 市场状态
# ═══════════════════════════════════════════

@app.get("/api/premarket/signal")
def api_premarket_signal():
    """盘前多空信号聚合 (A50+美股+汇率+流动性)"""
    try:
        from premarket import get_premarket_signal
        signal = get_premarket_signal()
        return {"ok": True, **signal}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/north-flow/realtime")
def api_north_flow_realtime():
    """北向资金实时追踪 (盘中方向+做T建议)"""
    try:
        from north_flow_tracker import get_north_flow_signal
        signal = get_north_flow_signal()
        return {"ok": True, **signal}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/north-flow/history")
def api_north_flow_history(days: int = Query(5, ge=1, le=30)):
    """北向资金历史流向 (近N日)"""
    try:
        from north_flow_tracker import get_north_flow_history
        history = get_north_flow_history(days)
        return {"ok": True, "history": history}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/market/regime")
def api_market_regime():
    """市场状态分类 (北向驱动/游资情绪/机构调仓/量化主导/防御避险)"""
    try:
        from market_regime import classify_regime
        regime = classify_regime()
        return {"ok": True, **regime}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════
#  市场概览 Dashboard
# ═══════════════════════════════════════════

@app.get("/api/dashboard")
def api_dashboard(refresh: bool = Query(False, description="强制刷新")):
    """市场概览：情绪、涨停数、炸板/跌停汇总、热门板块"""
    if not refresh:
        cached = daily_get("dashboard_latest")
        if cached:
            return cached

    import akshare as ak
    import pandas as pd
    from scanner import fetch_limit_up_pool, detect_market_sentiment
    today = _today_trading()
    result = {"ok": True, "date": today}

    # 市场情绪
    try:
        score, level, detail = detect_market_sentiment(today)
        result["sentiment"] = {"score": score, "level": level}
        if detail:
            for k, v in detail.items():
                if k not in ("zhaban_count", "dieting_count"):
                    result[k] = v
    except Exception as _e:
        print(f"  [dashboard] 情绪检测失败: {_e}", file=sys.stderr)
        result["sentiment"] = {"score": 0, "level": "未知"}

    # 涨停池 + 行业分布
    try:
        pool = fetch_limit_up_pool()
        if pool is not None and not pool.empty:
            result["limit_up_count"] = len(pool)
            ind_col = '所属行业' if '所属行业' in pool.columns else (pool.columns[15] if len(pool.columns) > 15 else None)
            top5 = pool[ind_col].value_counts().head(5)
            result["hot_sectors"] = [
                {
                    "name": str(name),
                    "count": int(cnt),
                    "url": f"https://www.10jqka.com.cn/#/search/{str(name)}"
                }
                for name, cnt in top5.items()
            ]
        else:
            result["limit_up_count"] = 0
            result["hot_sectors"] = []
    except Exception as _e:
        print(f"  [dashboard] 涨停池拉取失败: {_e}", file=sys.stderr)
        result["limit_up_count"] = 0
        result["hot_sectors"] = []

    # 炸板/跌停
    for api_name, key in [("stock_zt_pool_zbgc_em", "zhaban_count"),
                           ("stock_zt_pool_dtgc_em", "dieting_count")]:
        try:
            df = getattr(ak, api_name)(date=today)
            result[key] = len(df) if df is not None and not df.empty else 0
        except Exception as _e:
            print(f"  [dashboard] {api_name} 拉取失败: {_e}", file=sys.stderr)
            result[key] = 0

    # v2.0: 盘前信号 + 北向资金 + 市场状态 + 全市场资金流
    try:
        from premarket import get_premarket_signal
        pm = get_premarket_signal()
        result["premarket"] = {
            "direction": pm.get("direction", ""),
            "score": pm.get("score", 5),
            "confidence": pm.get("confidence", "低"),
            "summary": pm.get("summary", ""),
        }
    except Exception as _e:
        print(f"  [dashboard] 盘前信号失败: {_e}", file=sys.stderr)
        result["premarket"] = {"direction": "无数据", "score": 5}

    try:
        from north_flow_tracker import get_north_flow_signal
        nf = get_north_flow_signal()
        result["north_flow"] = {
            "direction": nf.get("direction", ""),
            "cumulative_net": nf.get("cumulative_net", 0),
            "signal": nf.get("signal", "中性"),
        }
    except Exception as _e:
        print(f"  [dashboard] 北向资金失败: {_e}", file=sys.stderr)
        result["north_flow"] = {"direction": "无数据", "cumulative_net": 0}

    # 全市场主力资金净流入 (从同花顺个股资金流聚合)
    try:
        from scanner_data import fetch_fund_flow_data
        fund_df, _ = fetch_fund_flow_data()
        if fund_df is not None and not fund_df.empty and '_net' in fund_df.columns:
            def _parse_net(val):
                s = str(val).replace('--', '0').strip()
                try:
                    if '亿' in s: return float(s.replace('亿', '')) * 1e8
                    if '万' in s: return float(s.replace('万', '')) * 1e4
                    return float(s)
                except (ValueError, TypeError):
                    return 0.0
            total_net = fund_df['_net'].apply(_parse_net).sum()
            result["market_fund_flow"] = {
                "total_net": round(total_net / 1e8, 1),  # 亿
                "direction": "流入" if total_net > 0 else "流出",
            }
        else:
            result["market_fund_flow"] = {"total_net": 0, "direction": "无数据"}
    except Exception as _e:
        print(f"  [dashboard] 全市场资金流失败: {_e}", file=sys.stderr)
        result["market_fund_flow"] = {"total_net": 0, "direction": "无数据"}

    try:
        from market_regime import classify_regime
        regime = classify_regime()
        result["regime"] = {
            "label": regime.get("label", ""),
            "position_advice": regime.get("position_advice", 1.0),
            "summary": regime.get("summary", ""),
        }
    except Exception as _e:
        print(f"  [dashboard] 市场状态失败: {_e}", file=sys.stderr)
        result["regime"] = {"label": "未知", "position_advice": 1.0}

    result["fetched_at"] = _fetched_at()
    daily_set("dashboard_latest", result, force=refresh)
    return result


# ═══════════════════════════════════════════
#  流式扫描端点 — SSE 实时进度
# ═══════════════════════════════════════════

@app.get("/api/scan/limit-up/stream")
async def api_scan_limit_up_stream(refresh: bool = Query(False, description="强制刷新"),
                                    principal: float = Query(20000, description="本金(元)"),
                                    plan: str = Query(None, description="评分方案(A/B/...)")):
    """涨停扫描 — SSE 流式输出实时进度（优先使用每日缓存）"""
    plan_name = plan or None
    today = _today_trading()
    _cache_key = f"limit_up_cards_{int(principal)}_{plan_name or 'default'}"

    async def _generate():
        # 检查每日缓存（refresh 时跳过）
        if not refresh:
            cached = daily_get(_cache_key)
            if cached:
                yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
                await asyncio.sleep(0.03)
                yield f"data: {json.dumps({'type':'complete','fetched_at':cached.get('fetched_at',''),'stocks':cached.get('stocks',[]),'sentiment':cached.get('sentiment',{}),'date':cached.get('date','')})}\n\n"
                return

        q = queue.Queue()
        result_holder = {"data": None, "error": None}

        class _Capture:
            """拦截 print(..., file=stderr) 的每一行，实时推入队列"""
            def __init__(self):
                self._buf = ""
            def write(self, text):
                self._buf += text
                while '\n' in self._buf:
                    idx = self._buf.index('\n')
                    line = self._buf[:idx].strip('\r').strip()
                    self._buf = self._buf[idx+1:]
                    if line and not line.startswith('\r'):
                        q.put(("progress", line))
            def flush(self):
                pass
            def reconfigure(self, **kwargs):
                pass  # 兼容 akshare/scanner 中 sys.stderr.reconfigure() 调用

        def _run():
            cap = _Capture()
            old_stderr = sys.stderr
            old_stdout = sys.stdout
            try:
                sys.stderr = cap
                sys.stdout = cap
                data = _scan_limit_up_data(today, principal=principal, plan_name=plan_name)
                result_holder["data"] = data
                if data and data.get('sentiment_ok'):
                    cache_data = _make_cache_entry(data['stocks'], data['sentiment_score'],
                                                    data['sentiment_level'], data['date'])
                    daily_set(_cache_key, cache_data, force=refresh)
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                sys.stderr = old_stderr
                sys.stdout = old_stdout
                q.put(("done", None))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        print(f"  [SSE] 启动扫描线程 (limit-up-run, principal={principal})", file=sys.stderr)

        while True:
            try:
                typ, val = q.get(timeout=0.2)
                if typ == "done":
                    break
                yield f"data: {json.dumps({'type':'progress','text':val})}\n\n"
                await asyncio.sleep(0.03)
            except queue.Empty:
                continue

        fet = _fetched_at()
        data = result_holder["data"]
        if data:
            # 保存推荐到追踪系统
            try:
                save_recommendations('limit-up', data.get('stocks', []), data.get('date', _today_trading()))
            except Exception:
                pass
            yield f"data: {json.dumps({'type':'complete','fetched_at':fet,'stocks':data['stocks'],'sentiment':{'score':data['sentiment_score'],'level':data['sentiment_level']},'date':data['date']})}\n\n"
        elif result_holder["error"]:
            yield f"data: {json.dumps({'type':'error','text':result_holder['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type':'error','text':'无数据'})}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.get("/api/scan/fetch-all")
async def api_scan_fetch_all(principal: float = Query(20000, description="本金(元)"),
                               plan: str = Query(None, description="评分方案(A/B/...)")):
    """全局「拉取」— 一次性获取所有板块原始数据并缓存（涨停+炸板+跌停+资金流+情绪）"""
    plan_name = plan or None
    import akshare as ak
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed

    today = _today_trading()

    async def _generate():
        q = queue.Queue()
        result = {}

        def _run():
            try:
                q.put(("progress", "📡 拉取涨停池+炸板池+跌停池..."))
                # 并行拉取三个池子
                def _get_limit(): return ak.stock_zt_pool_em(date=today)
                def _get_zhaban(): return ak.stock_zt_pool_zbgc_em(date=today)
                def _get_dieting(): return ak.stock_zt_pool_dtgc_em(date=today)
                pools = {}
                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = {ex.submit(f): k for f, k in [(_get_limit, 'limit'), (_get_zhaban, 'zhaban'), (_get_dieting, 'dieting')]}
                    for f in as_completed(futs):
                        try:
                            r = f.result()
                            pools[futs[f]] = r if (r is not None and not r.empty) else pd.DataFrame()
                        except: pools[futs[f]] = pd.DataFrame()
                limit_df = pools.get('limit', pd.DataFrame())
                zhaban_df = pools.get('zhaban', pd.DataFrame())
                dieting_df = pools.get('dieting', pd.DataFrame())
                result['pools'] = {'limit': limit_df, 'zhaban': zhaban_df, 'dieting': dieting_df}
                q.put(("progress", f"  涨停{len(limit_df)} 炸板{len(zhaban_df)} 跌停{len(dieting_df)} 只"))

                # 拉取涨停扫描完整数据（含资金流 + 情绪等）
                q.put(("progress", "📡 拉取资金流+情绪+龙虎榜..."))
                scan_data = _scan_limit_up_data(today, principal=principal, plan_name=plan_name)
                if scan_data:
                    result['scan'] = scan_data
                    q.put(("progress", f"  涨停扫描完成, {len(scan_data.get('stocks',[]))} 只上榜"))
                else:
                    result['scan'] = None
                    q.put(("progress", "  ⚠ 涨停扫描无数据"))

                result['ok'] = True
                result['date'] = today
                result['fetched_at'] = _fetched_at()
            except Exception as e:
                result['error'] = str(e)
                result['ok'] = False
            finally:
                q.put(("done", None))

        import threading
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        print(f"  [SSE] 启动扫描线程 (fetch-all, principal={principal})", file=sys.stderr)

        while True:
            try:
                typ, val = q.get(timeout=0.2)
                if typ == "done": break
                yield f"data: {json.dumps({'type':'progress','text':val})}\n\n"
                await asyncio.sleep(0.03)
            except queue.Empty:
                continue

        if result.get('ok'):
            scan = result.get('scan', {})
            if scan:
                stocks = scan.get('stocks', [])
                yield f"data: {json.dumps({'type':'complete','fetched_at':result['fetched_at'],'stocks':stocks,'sentiment':{'score':scan.get('sentiment_score',5),'level':scan.get('sentiment_level','未知')},'date':result['date'],'pools':{k:len(v) for k,v in result.get('pools',{}).items()}})}\n\n"
            else:
                yield f"data: {json.dumps({'type':'complete','fetched_at':result['fetched_at'],'stocks':[],'pools':{k:len(v) for k,v in result.get('pools',{}).items()}})}\n\n"
        else:
            yield f"data: {json.dumps({'type':'error','text':result.get('error','未知错误')})}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.get("/api/scan/limit-up/run")
async def api_scan_limit_up_run(principal: float = Query(20000, description="本金(元)"),
                                  plan: str = Query(None, description="评分方案(A/B/...)")):
    """涨停扫描「运行」— 优先从缓存重跑，无缓存则自动拉取"""
    plan_name = plan or None
    today = _today_trading()
    cache_key = f"limit_up_cards_{int(principal)}_{plan_name or 'default'}"
    async def _generate():
        data = _scan_from_raw_cache(principal=principal, plan_name=plan_name)
        if data is None or not data.get('stocks'):
            # 无缓存→自动降级为全量拉取
            yield f"data: {json.dumps({'type':'progress','text':'📡 无缓存，自动拉取数据...'})}\n\n"
            await asyncio.sleep(0.03)
            data = _scan_limit_up_data(today, principal=principal, plan_name=plan_name)
            if data is None or not data.get('stocks'):
                yield f"data: {json.dumps({'type':'error','text':'无涨停数据'})}\n\n"
                return
            fet = _fetched_at()
            yield f"data: {json.dumps({'type':'complete','fetched_at':fet,'stocks':data['stocks'],'sentiment':{'score':data['sentiment_score'],'level':data['sentiment_level']},'date':data['date']})}\n\n"
            if data.get('sentiment_ok'):
                from cache import daily_set
                daily_set(cache_key, _make_cache_entry(data['stocks'], data['sentiment_score'], data['sentiment_level'], data['date']), force=True)
        else:
            fet = _fetched_at()
            yield f"data: {json.dumps({'type':'progress','text':'📊 从缓存重跑评分...'})}\n\n"
            await asyncio.sleep(0.05)
            yield f"data: {json.dumps({'type':'complete','fetched_at':fet,'stocks':data['stocks'],'sentiment':{'score':data['sentiment_score'],'level':data['sentiment_level']},'date':data['date'],'from_cache':True})}\n\n"
            if data.get('sentiment_ok'):
                from cache import daily_set
                daily_set(cache_key, _make_cache_entry(data['stocks'], data['sentiment_score'], data['sentiment_level'], data['date']), force=True)

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════
#  龙虎榜分析 + 舆情监测 — SSE 流式进度
# ═══════════════════════════════════════════

def _stream_scan_generic(run_fn, complete_fn, on_success=None):
    q = queue.Queue()
    result_holder = {"data": None, "error": None}

    class _Capture:
        def __init__(self):
            self._buf = ""
        def write(self, text):
            self._buf += text
            while '\n' in self._buf:
                idx = self._buf.index('\n')
                line = self._buf[:idx].strip('\r').strip()
                self._buf = self._buf[idx+1:]
                if line and not line.startswith('\r'):
                    q.put(("progress", line))
        def flush(self): pass
        def reconfigure(self, **kwargs): pass

    def _run():
        cap = _Capture()
        old_stderr = sys.stderr
        old_stdout = sys.stdout
        try:
            sys.stderr = cap
            sys.stdout = cap
            data = run_fn()
            result_holder["data"] = data
            if data and on_success:
                try: on_success(data)
                except Exception as _sce: print(f"  [stream] on_success 回调异常: {_sce}", file=sys.stderr)
        except Exception as e:
            result_holder["error"] = str(e)
        finally:
            sys.stderr = old_stderr
            q.put(("done", None))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print(f"  [SSE] 启动扫描线程 (stream-generic)", file=sys.stderr)
    while True:
        try:
            typ, val = q.get(timeout=0.2)
            if typ == "done": break
            yield f"data: {json.dumps({'type':'progress','text':val})}\n\n"
        except queue.Empty: continue
    data = result_holder["data"]
    if data:
        yield f"data: {json.dumps({'type':'complete', **complete_fn(data)})}\n\n"
    elif result_holder["error"]:
        yield f"data: {json.dumps({'type':'error','text':result_holder['error']})}\n\n"
    else:
        yield f"data: {json.dumps({'type':'error','text':'无数据'})}\n\n"


def _cached_stream(gen):
    """边 yield 边输出（缓存由 on_success 在线程内完成）"""
    for event in gen:
        yield event


@app.get("/api/indicators/stream")
def api_indicators_stream(refresh: bool = Query(False, description="强制刷新")):
    from datetime import date
    today = _today_trading()
    if not refresh:
        cached = daily_get("indicators")
        if cached:
            async def _cached():
                yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
                yield f"data: {json.dumps({'type':'complete', **cached})}\n\n"
            return StreamingResponse(_cached(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    from indicators import run_enhanced
    from scanner import fetch_limit_up_pool, pre_filter
    def run():
        df = fetch_limit_up_pool()
        if df is None or df.empty: return {"ok": False, "output": "今日无涨停数据"}
        df = pre_filter(df)
        result, _ = run_enhanced(df, today_str=today)
        return {"ok": True, "output": result}
    return StreamingResponse(_cached_stream(_stream_scan_generic(run, lambda d: d, on_success=lambda d: daily_set("indicators", d, force=refresh))), media_type="text/event-stream")


@app.get("/api/community/stream")
def api_community_stream(refresh: bool = Query(False, description="强制刷新")):
    today = _today_trading()
    if not refresh:
        cached = daily_get("community")
        if cached:
            async def _cached():
                yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
                yield f"data: {json.dumps({'type':'complete', **cached})}\n\n"
            return StreamingResponse(_cached(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    import community
    from scanner import fetch_limit_up_pool
    def run():
        df = fetch_limit_up_pool()
        if df is None or df.empty: return {"ok": True, "output": "今日无涨停数据"}
        txt, _ = community.run(df, top_n=10)
        return {"ok": True, "output": txt if txt.strip() else "暂无舆情数据"}
    return StreamingResponse(_cached_stream(_stream_scan_generic(run, lambda d: d, on_success=lambda d: daily_set("community", d, force=refresh))), media_type="text/event-stream")


@app.get("/api/community/cards")
def api_community_cards(refresh: bool = Query(False, description="强制刷新")):
    """舆情监测 — 结构化卡片数据（含评分解析、行情背景）"""
    from datetime import date
    import community as comm
    import pandas as pd
    from scanner import fetch_limit_up_pool
    today = _today_trading()
    df = fetch_limit_up_pool()
    if df is None or df.empty:
        return {"ok": True, "items": []}
    _, smap = comm.run(df, top_n=10)
    # 从涨停池取行情上下文
    ctx = {}
    if not df.empty:
        for _, r in df.iterrows():
            code = str(r.iloc[1]).strip().zfill(6)
            ctx[code] = {
                'ind': str(r.iloc[15]) if len(r) > 15 else '',
                'changes': float(r.iloc[3]) if r.iloc[3] != '--' else 0,
                'turnover': float(r.iloc[8]) if r.iloc[8] != '--' else 0,
                'seal_time': str(int(float(r.iloc[10]))) if pd.notna(r.iloc[10]) and r.iloc[10] != '--' else '',
                'consecutive': int(r.iloc[14]) if pd.notna(r.iloc[14]) and r.iloc[14] != '--' else 0,
            }
    items = []
    for code, info in smap.items():
        news = info.get('news', [])
        c = ctx.get(code, {})
        cs = info.get('comment_score') or 0
        inst = info.get('机构参与度') or 0
        attn = info.get('关注指数') or 0

        # 分析原因标签
        reasons = []
        if c.get('consecutive', 0) >= 2:
            reasons.append(f'{c["consecutive"]}连板')
        if c.get('ind'):
            reasons.append(f'所属{c["ind"]}板块')
        if inst and inst > 0.5:
            reasons.append('机构高参与')
        if cs >= 80:
            reasons.append('基本面优秀')
        elif cs >= 70:
            reasons.append('基本面良好')
        if news:
            reasons.append('近期有催化新闻')
        if attn > 0:
            reasons.append(f'关注度{attn}分')

        items.append({
            'code': code,
            'name': info.get('name', ''),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'comment_score': round(cs, 2),
            'guba_rank': info.get('guba_rank'),
            'institution': round(inst, 2) if inst else None,
            'attention': round(attn, 1) if attn else None,
            'news': [{'title': n.get('title', ''), 'source': n.get('source', ''), 'url': n.get('url', '')}
                     for n in (news or [])[:3]],
            'context': {
                'industry': c.get('ind', ''),
                'consecutive': c.get('consecutive', 0),
                'turnover': c.get('turnover', 0),
                'seal_time': c.get('seal_time', ''),
            },
            'reasons': reasons,
            'score_note': _comment_score_note(cs),
        })
    items.sort(key=lambda x: x.get('comment_score') or 0, reverse=True)
    return {"ok": True, "items": items[:10], "fetched_at": _fetched_at()}

@app.get("/api/sentiment/cards")
def api_sentiment_cards(refresh: bool = Query(False, description="强制刷新")):
    """Market sentiment - structured card data with cache"""
    from scanner import detect_market_sentiment
    from datetime import date
    try:
        today = _today_trading()
        if not refresh:
            cached = daily_get("sentiment_cards")
            if cached:
                return cached
        score, level, details = detect_market_sentiment(today)
        if details is None:
            details = {}
        icons = {"高潮":"🔥","活跃":"⚡","正常":"✅","低迷":"⚠️","冰点":"❄️"}
        icon = icons.get(level, "📊")
        result = {"ok": True, "score": score, "level": level, "icon": icon,
                  "prev_limit_count": details.get("prev_limit_count", 0),
              "today_limit_up": details.get("today_limit_up", 0),
              "today_limit_down": details.get("today_limit_down", 0),
              "today_breadth": details.get("today_breadth", 0),
              "all_up": details.get("all_up", 0),
              "all_down": details.get("all_down", 0),
                  "zhaban_count": details.get("zhaban_count", 0),
                  "dieting_count": details.get("dieting_count", 0),
                  "avg_premium": details.get("avg_premium", 0),
                  "promotion_rate": details.get("promotion_rate", 0),
                  "zhaban_rate": details.get("zhaban_rate", 0),
                  "fetched_at": _fetched_at()}
        daily_set("sentiment_cards", result, force=refresh)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "score": 0, "level": "未知", "icon": "📊",
                "prev_limit_count": 0, "today_limit_up": 0, "today_limit_down": 0, "today_breadth": 0, "all_up": 0, "all_down": 0, "zhaban_count": 0, "dieting_count": 0,
                "avg_premium": 0, "promotion_rate": 0, "zhaban_rate": 0}


@app.get("/api/sentiment/stream")
async def api_sentiment_stream(refresh: bool = Query(False, description="强制刷新")):
    """Market sentiment - SSE streaming with real-time progress"""
    from datetime import date
    today = _today_trading()
    if not refresh:
        cached = daily_get("sentiment_cards")
        if cached:
            async def _cached():
                yield "data: " + json.dumps({"type":"progress","text":"use cache..."}) + chr(10) + chr(10)
                yield "data: " + json.dumps({"type":"complete","ok": True, **cached}) + chr(10) + chr(10)
            return StreamingResponse(_cached(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    from scanner import detect_market_sentiment
    def run():
        score, level, details = detect_market_sentiment(today)
        if details is None:
            details = {}
        return {"ok": True, "score": score, "level": level,
                "prev_limit_count": details.get("prev_limit_count", 0),
              "today_limit_up": details.get("today_limit_up", 0),
              "today_limit_down": details.get("today_limit_down", 0),
              "today_breadth": details.get("today_breadth", 0),
              "all_up": details.get("all_up", 0),
              "all_down": details.get("all_down", 0),
                "zhaban_count": details.get("zhaban_count", 0),
                "dieting_count": details.get("dieting_count", 0),
                "avg_premium": details.get("avg_premium", 0),
                "promotion_rate": details.get("promotion_rate", 0),
                "zhaban_rate": details.get("zhaban_rate", 0)}
    return StreamingResponse(_cached_stream(_stream_scan_generic(run, lambda d: d, on_success=lambda d: daily_set("sentiment_cards", d, force=refresh))), media_type="text/event-stream")


# ═══ 各板块流式端点（炸板/趋势/跌停/板块 — 统一进度条体验） ═══

def _mode_stream_endpoint(run_fn, complete_fn, cache_key, refresh: bool):
    """通用模式流式端点工厂。run_fn 执行扫描，stderr 输出实时推送为进度。"""
    if not refresh:
        cached = daily_get(cache_key)
        if cached:
            async def _cached():
                yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存...'})}\n\n"
                await asyncio.sleep(0.03)
                yield f"data: {json.dumps({'type':'complete', 'items': cached.get('items',[]), 'fetched_at': cached.get('fetched_at','')})}\n\n"
            return StreamingResponse(_cached(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    async def _gen():
        q = queue.Queue()
        result = {}
        # 捕获 stderr 输出作为进度推送
        class _Cap:
            def __init__(self, real_stderr):
                self._b = ""
                self._real = real_stderr
            def write(self, t):
                self._b += t
                while '\n' in self._b:
                    idx = self._b.index('\n'); line = self._b[:idx].strip('\r').strip()
                    self._b = self._b[idx+1:]
                    if line: q.put(("progress", line))
            def flush(self): pass
            def reconfigure(self, **kw): pass
            def __getattr__(self, name):
                # 将不支持的属性代理到真正的 stderr，避免 akshare 等库调用 fileno/isatty 时报错
                return getattr(self._real, name)
        cap = _Cap(sys.stderr)
        def _run():
            import sys
            old = sys.stderr
            try:
                sys.stderr = cap
                items, extra = run_fn()
                result['items'] = items
                result.update(extra or {})
                # 自动保存推荐到追踪系统
                try:
                    tab_name = cache_key.replace('_stream', '').replace('_cards', '')
                    save_recommendations(tab_name, items, _today_trading())
                except Exception:
                    pass
            except Exception as e:
                result['error'] = str(e)
            finally:
                sys.stderr = old
                q.put(("done", None))
        import threading
        threading.Thread(target=_run, daemon=True).start()
        print(f"  [SSE] 启动扫描线程 (mode-stream, cache_key={cache_key})", file=sys.stderr)
        while True:
            try: typ, val = q.get(timeout=0.2)
            except queue.Empty: continue
            if typ == "progress":
                yield f"data: {json.dumps({'type':'progress','text':val})}\n\n"
                await asyncio.sleep(0.03)
            elif typ == "done":
                break
        if result.get('error'):
            yield f"data: {json.dumps({'type':'error','text':result['error']})}\n\n"
        else:
            fet = _fetched_at()
            complete = complete_fn(result, fet)
            if complete:
                from cache import daily_set
                daily_set(cache_key, complete, force=True)
            yield f"data: {json.dumps({'type':'complete', 'items': result.get('items',[]), 'fetched_at': fet})}\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/scan/zhaban/stream")
async def api_zhaban_stream(refresh: bool = Query(False)):
    today = _today_trading()
    def run():
        print("  [炸板] 拉取数据...", file=sys.stderr)
        df = api_zhaban_cards(refresh=True).body  if False else None
        import akshare as ak; import pandas as pd
        zb = ak.stock_zt_pool_zbgc_em(date=today)
        if zb.empty: return [], {}
        df = zb.copy()
        from scanner import filter_non_main_board, filter_xr_xd_dr
        df = filter_non_main_board(df)
        df = filter_xr_xd_dr(df)
        if '流通市值' in df.columns: df = df[df['流通市值'].astype(float) <= 200 * 1e8]
        price_f = df.columns[4] if len(df.columns) > 4 else df.columns[0]; df = df[df[price_f].astype(float) <= 60]
        if df.empty: return [], {}
        print(f"  [炸板] 共 {len(df)} 只, 评分中...", file=sys.stderr)
        from scanner import score_zhaban_data
        try:
            from weight_manager import _load_tab_weights
            w = _load_tab_weights('zhaban')
        except Exception:
            w = None
        scored = score_zhaban_data(df, today, weights=w)
        items = []
        zc = _zhaban_columns(scored)
        for _, row in scored.iterrows():
            code = str(row.get(zc['code'], '') or row.iloc[0]).strip().zfill(6)
            name = str(row.get(zc['name'], '') or row.iloc[0])
            seal_time = str(row.get(zc['st'], ''))[:4]
            total = float(row.get('总分', 0))
            price = float(row.get(zc['price'], 0)) if pd.notna(row.get(zc['price'], None)) else 0
            turnover = round(float(row.get(zc['to'], 0) or 0), 1)
            zb_times = int(float(row.get(zc['zb'], 0))) if pd.notna(row.get(zc['zb'], None)) else 0
            seal_fund = float(row.get(zc['sf'], 0) or 0)
            net = float(row.get('净流入', 0))
            industry = str(row.get(zc['ind'], ''))
            # 信号标签
            sigs = []
            if seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 10: sigs.append('早盘封板')
            elif seal_time and len(seal_time) >= 4 and int(seal_time[:2]) < 11: sigs.append('上午封板')
            else: sigs.append('午后封板')
            if zb_times > 0: sigs.append(f'炸板{zb_times}次')
            if net > 1e8: sigs.append('资金承接强')
            elif net > 0: sigs.append('有资金承接')
            else: sigs.append('资金流出')
            if 8 <= turnover <= 20: sigs.append('换手适中')
            elif turnover > 20: sigs.append('高换手')
            # 建议
            if total >= 70: advice = '反包潜力高，竞价高开放量可参与'
            elif total >= 50: advice = '竞价观察，高开放量可博弈反包'
            elif total >= 35: advice = '仅观望，需竞价放量确认'
            else: advice = '不参与'
            items.append({'code': code, 'name': name, 'score': round(total), 'price': price,
                          'seal_time': seal_time, 'turnover': turnover, 'seal_fund': seal_fund,
                          'zhaban_times': zb_times, 'industry': industry, 'net_money': round(net, 0),
                          'signals': sigs, 'advice': advice,
                          'url': f"https://stockpage.10jqka.com.cn/{code}/"})
        return items[:10], {}
    return _mode_stream_endpoint(run, lambda r, fet: {'items': r.get('items',[]), 'fetched_at': fet}, 'zhaban_stream', refresh)


@app.get("/api/scan/trend/stream")
async def api_trend_stream(refresh: bool = Query(False),
                            principal: float = Query(20000, description="本金(元)")):
    def run():
        print("  [趋势] 拉取数据...", file=sys.stderr)
        # 直接调 cards 端点保证数据源唯一，消除与刷新的差异
        result = api_trend_cards(refresh=True, principal=principal)
        items = result.get('items', []) if result else []
        return items, {}
    return _mode_stream_endpoint(run, lambda r, fet: {'items': r.get('items',[]), 'fetched_at': fet}, 'trend_stream', refresh)


@app.get("/api/scan/dtqiaoban/stream")
async def api_dtqiaoban_stream(refresh: bool = Query(False)):
    today = _today_trading()
    def run():
        print("  [翘板] 拉取跌停数据...", file=sys.stderr)
        import akshare as ak; import pandas as pd
        dt = ak.stock_zt_pool_dtgc_em(date=today)
        if dt.empty: return [], {}
        print(f"  [翘板] 共 {len(dt)} 只, 评分中...", file=sys.stderr)
        from scanner import filter_non_main_board, filter_xr_xd_dr, score_dtqiaoban_data
        df = filter_non_main_board(dt)
        df = filter_xr_xd_dr(df)
        if len(df.columns) > 6 and '流通市值' in df.columns:
            df = df[df['流通市值'].astype(float) <= 200 * 1e8]
        elif len(df.columns) > 6:
            df = df[df.iloc[:, 6].astype(float) <= 200 * 1e8]
        if df.empty: return [], {}
        try:
            from weight_manager import _load_tab_weights
            w = _load_tab_weights('dtqiaoban')
        except Exception:
            w = None
        scored = score_dtqiaoban_data(df, weights=w)
        items = []
        for _, row in scored.iterrows():
            code = str(row.iloc[1]).strip().zfill(6)
            total = round(row.get('翘板评分', 0))
            turn_val = float(row.iloc[9]) if len(row) > 9 and pd.notna(row.iloc[9]) else 0
            seal_val = float(row.iloc[10]) if len(row) > 10 and pd.notna(row.iloc[10]) else 0
            cont_val = int(float(row.iloc[13])) if len(row) > 13 and pd.notna(row.iloc[13]) else 0
            st = str(row.iloc[11]) if len(row) > 11 and pd.notna(row.iloc[11]) else ''
            price = float(row.iloc[4]) if pd.notna(row.iloc[4]) else 0
            sigs = []
            if total >= 60: sigs.append('高信号')
            elif total >= 35: sigs.append('中等信号')
            else: sigs.append('弱信号')
            if turn_val > 10: sigs.append('高换手承接')
            if cont_val >= 3: sigs.append(f'N{cont_val}板超跌')
            advice = '竞价观察，放量高开可博弈反抽' if total >= 60 else ('仅观望' if total >= 35 else '不参与')
            items.append({'code': code, 'name': str(row.iloc[2]),
                          'score': total, 'price': price,
                          'change': float(row.iloc[3]) if pd.notna(row.iloc[3]) else -10,
                          'seal_time': st[:4] if len(st) >= 4 else st,
                          'turnover': turn_val, 'seal_fund': seal_val, 'consecutive': cont_val,
                          'signals': sigs, 'advice': advice,
                          'url': f"https://stockpage.10jqka.com.cn/{code}/"})
        return items[:10], {}
    return _mode_stream_endpoint(run, lambda r, fet: {'items': r.get('items',[]), 'fetched_at': fet}, 'dtqiaoban_stream', refresh)


@app.get("/api/scan/sector/stream")
async def api_sector_stream(refresh: bool = Query(False)):
    today = _today_trading()
    def run():
        print("  [板块] 拉取涨停+炸板+跌停池...", file=sys.stderr)
        from scanner import score_sector_data
        import akshare as ak; import pandas as pd
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pools = {}
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(ak.stock_zt_pool_em, date=today): 'limit',
                    ex.submit(ak.stock_zt_pool_zbgc_em, date=today): 'zhaban',
                    ex.submit(ak.stock_zt_pool_dtgc_em, date=today): 'dieting'}
            for f in as_completed(futs):
                try:
                    r = f.result()
                    pools[futs[f]] = r if (r is not None and not r.empty) else pd.DataFrame()
                except: pools[futs[f]] = pd.DataFrame()
        print(f"  [板块] 涨停{len(pools.get('limit',pd.DataFrame()))} 炸板{len(pools.get('zhaban',pd.DataFrame()))} 跌停{len(pools.get('dieting',pd.DataFrame()))}, 计算中...", file=sys.stderr)
        stats = score_sector_data(pools.get('limit',pd.DataFrame()), pools.get('zhaban',pd.DataFrame()), pools.get('dieting',pd.DataFrame()), top_n=15)
        items = []
        for s in stats:
            items.append({'name': s['industry'], 'limit_count': s['limit_cnt'],
                          'zhaban_count': s['zhaban_cnt'], 'dieting_count': s['dieting_cnt'],
                          'score': min(12, 4 + s['limit_cnt'] * 2), 'efficiency': s['seal_rate'],
                          'url': f"https://www.10jqka.com.cn/#/search/{s['industry']}"})
        return items, {}
    return _mode_stream_endpoint(run, lambda r, fet: {'items': r.get('items',[]), 'fetched_at': fet}, 'sector_stream', refresh)


def _comment_score_note(score):
    if score >= 85: return '🔥 综合评分极高，基本面+市场情绪共振'
    if score >= 80: return '✅ 评分优秀，基本面扎实，机构关注度高'
    if score >= 70: return '📊 评分良好，多数维度表现稳定'
    if score >= 60: return '📌 评分一般，部分维度有待改善'
    if score >= 50: return '⚠️ 评分偏低，需关注基本面变化'
    return '❌ 评分较差，谨慎对待'  


# ═══════════════════════════════════════════
#  市场状态检测
# ═══════════════════════════════════════════

def get_market_status():
    """返回当前市场状态: 'trading' / 'closed' / 'lunch' / 'weekend' / 'holiday'"""
    from cache import _is_trading_day
    now = datetime.now(_CST)
    if not _is_trading_day(now.strftime("%Y%m%d")):
        return "holiday" if now.weekday() < 5 else "weekend"
    minute_of_day = now.hour * 60 + now.minute
    if (570 <= minute_of_day < 690) or (780 <= minute_of_day < 900):
        return "trading"
    if (690 <= minute_of_day < 780):
        return "lunch"
    return "closed"


_CLOSE_CACHE_KEY = "limit_up_cards_20000_default"

def _schedule_close_scan():
    """盘后 15:05 自动触发一次全量扫描，写入冻结缓存（默认本金 2 万）

    每天调度一次，收盘后自动归档数据供回测使用。
    """
    import threading
    from cache import daily_get, _is_trading_day
    from datetime import timedelta
    now = datetime.now(_CST)
    if not _is_trading_day(now.strftime("%Y%m%d")):
        # 非交易日，调度到明天的同一时间检查
        tomorrow = now + timedelta(days=1)
        delay = (tomorrow.replace(hour=15, minute=5, second=0, microsecond=0) - now).total_seconds()
        threading.Timer(max(60, delay), _schedule_close_scan).start()
        return
    target = now.replace(hour=15, minute=5, second=0, microsecond=0)
    if now >= target:
        has_cache = daily_get(_CLOSE_CACHE_KEY) is not None
        if has_cache:
            # 已有缓存，调度到明天
            tomorrow = now + timedelta(days=1)
            delay = (tomorrow.replace(hour=15, minute=5, second=0, microsecond=0) - now).total_seconds()
            threading.Timer(max(60, delay), _schedule_close_scan).start()
            return
        print("  [收盘扫描] 15:05 已过且无缓存，60秒后启动补扫", file=sys.stderr)
        threading.Timer(60, lambda: _run_close_scan(principal=20000)).start()
        return
    delay = (target - now).total_seconds()
    threading.Timer(delay, lambda: _run_close_scan(principal=20000)).start()
    print(f"  [收盘扫描] 已调度，将在 {delay/60:.0f} 分钟后执行", file=sys.stderr)

def _run_close_scan(principal=20000):
    """执行收盘扫描，强制写入每日缓存"""
    from cache import daily_set
    from datetime import date
    from scanner import fetch_limit_up_pool
    print(f"  [收盘扫描] ============ 开始 (本金{principal}元) =============", file=sys.stderr)
    today = _today_trading()
    try:
        data = _scan_limit_up_data(today, principal=principal)
        if data is None or not data['stocks']:
            print("  [收盘扫描] 无数据，10分钟后重试", file=sys.stderr)
            threading.Timer(600, lambda: _run_close_scan(principal=principal)).start()
            return
        cache_data = _make_cache_entry(data['stocks'], data['sentiment_score'],
                                        data['sentiment_level'], data['date'])
        if data.get('sentiment_ok'):
            daily_set(_CLOSE_CACHE_KEY, cache_data, force=True)
            # 同步缓存市场概览（force=True 绕过盘后冻结）
            _cache_dashboard_snapshot(today, data['sentiment_score'], data['sentiment_level'],
                                      len(data['stocks']), data['df'])
            print(f"  [收盘扫描] ✅ 完成，{len(data['stocks'])} 只标的已缓存", file=sys.stderr)
            # 触发每日数据归档（后台线程，不阻塞）
            try:
                import archiver
                threading.Thread(target=lambda: archiver.archive_day_t(today), daemon=True).start()
                print("  [归档] SQLite 已触发", file=sys.stderr)
            except Exception as e:
                print(f"  [归档] SQLite 失败: {e}", file=sys.stderr)
            # Plan 结果归档 (daily_data/日期/plan_*.json, 供回测系统使用)
            try:
                from plans.archiver import save_plan_result
                plan_name_arch = 'A'
                threading.Thread(
                    target=lambda: save_plan_result(today, plan_name_arch, data),
                    daemon=True
                ).start()
                print(f"  [归档] Plan {plan_name_arch} 结果已触发保存", file=sys.stderr)
            except Exception as e:
                print(f"  [归档] Plan结果 失败: {e}", file=sys.stderr)
            # 触发 T+1 真实回测 (后台, 不阻塞, 慢 ~2 分钟)
            try:
                threading.Thread(
                    target=lambda: _run_t1_backtest_cached(max_days=30, top_n=3, capital=20000, force=True),
                    daemon=True
                ).start()
                print("  [T+1 回测] 已触发后台运行 (30 天 / TOP 3)", file=sys.stderr)
            except Exception as e:
                print(f"  [T+1 回测] 启动失败: {e}", file=sys.stderr)
            # 触发盘后自动调权 (后台, 不阻塞, 慢 ~30-60s, 互斥锁防盘中冲撞)
            try:
                import weight_scheduler
                threading.Thread(
                    target=weight_scheduler.run_after_hours_weight_adjust,
                    kwargs={'force': False},
                    daemon=True
                ).start()
                print("  [调权调度] 已触发盘后自动调权 (plan_a + trend, 互斥锁防盘中拉数据冲撞)", file=sys.stderr)
            except Exception as e:
                print(f"  [调权调度] 启动失败: {e}", file=sys.stderr)
        else:
            print("  [收盘扫描] 情绪数据异常，10分钟后重试", file=sys.stderr)
            threading.Timer(600, lambda: _run_close_scan(principal=principal)).start()
    except Exception as e:
        print(f"  [收盘扫描] 失败: {e}，10分钟后重试", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        threading.Timer(600, lambda: _run_close_scan(principal=principal)).start()


def _cache_dashboard_snapshot(today_str, sentiment_score, sentiment_level, limit_up_count, df):
    """收盘时缓存市场概览快照（force 绕过冻结）"""
    import pandas as pd
    result = {"ok": True, "date": today_str, "fetched_at": _fetched_at(),
              "sentiment": {"score": sentiment_score, "level": sentiment_level},
              "limit_up_count": limit_up_count}
    if df is not None and not df.empty:
        ind_col = '所属行业' if '所属行业' in df.columns else (df.columns[15] if len(df.columns) > 15 else None)
        if ind_col:
            top5 = df[ind_col].value_counts().head(5)
            result["hot_sectors"] = [{"name": str(n), "count": int(c), "url": f"https://www.10jqka.com.cn/#/search/{str(n)}"}
                                     for n, c in top5.items()]
        else:
            result["hot_sectors"] = []
    else:
        result["hot_sectors"] = []
    daily_set("dashboard_latest", result, force=True)


@app.get("/api/version")
def api_version():
    """返回当前版本号和更新日志"""
    vpath = os.path.join(_BASE_DIR, "version.json")
    try:
        with open(vpath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"version": "unknown", "changes": []}


@app.get("/api/market-status")
@app.get("/api/health")
def api_health():
    """系统健康检查：akshare连通性 + 缓存磁盘 + 交易日历"""
    checks = {}
    # 1. akshare连通性
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        checks["akshare"] = "ok" if cal is not None and not cal.empty else "empty"
    except Exception as e:
        checks["akshare"] = f"fail: {e}"

    # 2. 缓存目录可写
    try:
        test_path = os.path.join(os.environ.get("TMP", "/tmp"), "claude_stock_cache", ".healthcheck")
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, 'w') as f: f.write('ok')
        os.remove(test_path)
        checks["cache_disk"] = "ok"
    except Exception as e:
        checks["cache_disk"] = f"fail: {e}"

    # 3. 交易日历
    try:
        from cache import _load_trading_calendar
        cal = _load_trading_calendar()
        checks["calendar"] = f"ok ({len(cal)} days)" if len(cal) > 5000 else f"degraded ({len(cal)})"
    except Exception as e:
        checks["calendar"] = f"fail: {e}"

    all_ok = all(not v.startswith("fail") for v in checks.values())
    return {"ok": all_ok, "checks": checks}


@app.get("/api/market-status")
def api_market_status():
    status = get_market_status()
    return {"ok": True, "status": status}


@app.get("/api/weights/status")
def api_weights_status():
    """盘后调权状态 — 供前端面板展示"上次调权时间 / 调权中"等

    盘中永远显示"never_run"或"stale", 用户看到"调权中"是异常
    """
    import weight_scheduler
    return {
        "ok": True,
        "weight_adjust": weight_scheduler.get_weight_adjust_status(),
    }


@app.post("/api/weights/run")
def api_weights_run(force: bool = Query(False, description="强制调权, 跳过市场状态检查")):
    """手动触发调权 (CLI/调试用)

    force=True: 跳过盘中检查 (供用户在盘中手动触发, 不推荐)
    force=False: 仅盘后触发
    """
    import weight_scheduler
    # 后台跑, 不阻塞 API
    threading.Thread(
        target=weight_scheduler.run_after_hours_weight_adjust,
        kwargs={'force': force},
        daemon=True
    ).start()
    return {"ok": True, "msg": f"调权已在后台启动 (force={force})"}


@app.get("/api/backtest/dashboard")
def api_backtest_dashboard():
    """回测效果追踪面板 — 返回权重历史、因子相关性、模拟收益"""
    import weight_manager as wm
    rolling = wm._load_rolling_data()
    current_w = wm.load_weights()
    default_w = wm.DEFAULT_WEIGHTS

    # 当前 vs 默认权重对比
    weight_comparison = []
    for k in wm.BACKTEST_FACTORS:
        weight_comparison.append({
            'factor': k, 'current': round(current_w.get(k, default_w[k]), 1),
            'default': default_w[k],
            'delta': round(current_w.get(k, default_w[k]) - default_w[k], 2)
        })

    # 相关性历史（近30天）
    corr_history = []
    for entry in rolling[-30:]:
        day_corrs = entry.get('correlations', {})
        day_corrs['date'] = entry['date']
        corr_history.append(day_corrs)

    # 调权历史（从 weights.json 的备份中恢复，简化为展示当前状态）
    days_with_data = len(rolling)
    ready = days_with_data >= 2

    return {
        'ok': True,
        'weights': weight_comparison,
        'corr_history': corr_history,
        'days_with_data': days_with_data,
        'ready': ready,
        'backtest_factors': wm.BACKTEST_FACTORS,
    }


# ═══════════════════════════════════════════
#  T+1 真实回测面板 (A 股 T+1 规则)
# ═══════════════════════════════════════════

def _run_t1_backtest_cached(max_days=30, top_n=3, capital=30000, force=False):
    """跑 T+1 真实回测, 带参数化缓存 (避免每次请求都拉 30 天数据)"""
    from cache import daily_get_pkl, daily_set_pkl, make_key
    cache_key = make_key("t1", "outer", max_days=max_days, top_n=top_n, capital=int(capital))
    if not force:
        cached = daily_get_pkl(cache_key)
        if cached is not None:
            return cached
    try:
        from t1_real_backtest import run_t1_backtest
        result = run_t1_backtest(max_days=max_days, top_n=top_n, capital=capital)
        daily_set_pkl(cache_key, result, force=force)
        return result
    except Exception as e:
        return {'error': f'T+1 回测失败: {str(e)[:200]}', 'summary': {}, 'trades': []}



# ═══════════════════════════════════════════
#  GitHub Webhook — 自动部署
# ═══════════════════════════════════════════

import hashlib
import hmac
import subprocess

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")


@app.post("/webhook")
async def webhook(request: Request):
    """GitHub webhook: 收到 push 事件后自动拉取代码并重启服务"""
    # 验证签名（如果设置了 secret）
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if WEBHOOK_SECRET != "changeme" and sig:
        expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return JSONResponse({"ok": False, "error": "signature mismatch"}, status_code=403)

    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return {"ok": True, "event": event, "action": "ignored"}

    # 后台拉取并重启（git pull HTTPS 可能挂起，改用 curl 下载压缩包）
    def _deploy():
        import urllib.request, zipfile, io, shutil
        tmp_zip = "/tmp/stock_scanner_deploy.zip"
        tmp_dir = "/tmp/stock_scanner_deploy"
        try:
            urllib.request.urlretrieve(
                "https://api.github.com/repos/lunqin123/stock-scanner/zipball/master",
                tmp_zip)
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                zf.extractall(tmp_dir)
            dirs = os.listdir(tmp_dir)
            if dirs:
                src = os.path.join(tmp_dir, dirs[0])
                for item in os.listdir(src):
                    s = os.path.join(src, item)
                    d = os.path.join("/home/ubuntu/stock-scanner", item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
            os.remove(tmp_zip)
            # 同步 git HEAD 指针到 origin/master，让 worktree 跟 HEAD 一致
            # （webhook 只覆盖文件不动 git，会导致后续 SSH git pull 失败）
            subprocess.run(
                ["git", "update-ref", "HEAD", "origin/master"],
                cwd="/home/ubuntu/stock-scanner",
                capture_output=True, timeout=10)
            subprocess.run(["sudo", "systemctl", "restart", "stock-scanner"], capture_output=True, timeout=30)
        except Exception as e:
            print(f"[Webhook] 部署失败: {e}", file=sys.stderr)

    threading.Thread(target=_deploy, daemon=True).start()
    return {"ok": True, "event": "push", "action": "deploying"}


@app.get("/version.json")
def api_version():
    """版本信息（前端更新日志弹窗）"""
    import json as _json
    path = os.path.join(_BASE_DIR, "version.json")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ═══════════════════════════════════════════
#  前端页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(_BASE_DIR, "templates/index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    # 用服务器启动时间戳替换所有 ?v=x.y.z，彻底破浏览器缓存
    # （之前字面量 '?v=1.22.0' 跟 HTML 实际版本号 '?v=1.22.1' / '?v=1.18.5' 不匹配，导致替换不生效，disk cache 一直命中）
    import time as _time, re as _re
    _ts = str(int(_time.time()))
    html = _re.sub(r'\?v=\d+(?:\.\d+){0,3}', f'?v={_ts}', html)
    # 注入版本号到页面，方便确认是否最新
    import json as _json
    try:
        with open(os.path.join(_BASE_DIR, "version.json"), "r", encoding="utf-8") as _vf:
            _ver = _json.load(_vf)
            _ver_str = f'v{_ver["version"]} ({_ver["date"]})'
            html = html.replace('<span id="version-label">版本</span>', f'<span id="version-label">{_ver_str}</span>')
    except Exception as _ve: print(f"  [app] version.json 加载失败: {_ve}", file=sys.stderr)
    # 请求级随机数，每次刷新页面都不一样，彻底破一切缓存
    import random as _random
    _nonce = str(_random.randint(100000, 999999))
    html = html.replace('</head>', f'<meta name="x-nonce" content="{_nonce}"></head>')
    # 注入缓存的排行榜数据（QQ 浏览器等无法依赖 localStorage）
    # 自动找最新缓存（不硬编码本金），避免与"运行"按钮的排行榜不一致
    import json as _json, glob as _glob
    cached = None
    pattern = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
                           "claude_stock_cache", "daily_*_limit_up_cards_*_v*.json")
    candidates = sorted(_glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for path in candidates:
        try:
            with open(path, 'r', encoding='utf-8') as _f:
                data = _json.loads(_f.read())
            if data.get('stocks'):
                cached = data
                break
        except Exception:
            continue
    if cached and cached.get('stocks'):
        inject = '<script>window._CACHED_RANKING = ' + _json.dumps(cached, ensure_ascii=False) + ';</script>'
        html = html.replace('</head>', inject + '</head>')
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ═══════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════

def _cleanup_old_cache(days=30):
    """删除超过 N 天的旧缓存文件"""
    import glob, time
    now = time.time()
    dirs = [os.path.join(os.environ.get("TMP", "/tmp"), d)
            for d in ("claude_stock_cache", "stock_scanner_cache")]
    cleaned = 0
    for d in dirs:
        if not os.path.exists(d): continue
        for f in glob.glob(os.path.join(d, "*")):
            try:
                if now - os.path.getmtime(f) > days * 86400:
                    os.remove(f)
                    cleaned += 1
            except OSError as e:
                print(f"  [启动清理] 删除 {f} 失败: {e}", file=sys.stderr)
    if cleaned > 0:
        print(f"  [启动清理] 删除 {cleaned} 个超过{days}天的旧缓存文件", file=sys.stderr)

@app.on_event("startup")
def _on_startup():
    """应用启动时：清理旧缓存 + 调度盘后自动扫描"""
    _cleanup_old_cache()
    _schedule_close_scan()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
