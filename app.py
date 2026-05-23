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
from datetime import date
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from cache import get as cache_get, put as cache_put, daily_get, daily_set

app = FastAPI(title="A股超短线选股扫描器", version="1.0.0")

# ── 挂载静态文件 ──
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")


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

@app.get("/api/scan/limit-up")
def api_scan_limit_up(table: bool = Query(False, description="表格模式")):
    """涨停池扫描"""
    from scanner import fetch_limit_up_pool, pre_filter, score_seal_strength
    from scanner import get_money_flow_scores, get_sector_heat_scores, score_tech_form
    from scanner import format_table_output, format_output

    today = date.today().strftime("%Y%m%d")
    out, err = _capture(lambda: _run_limit_up_scan(today, table))
    # 只有包含 [运行时错误] 才算真正的错误，进度信息是 stderr 正常输出
    has_runtime_error = "[运行时错误]" in err
    if has_runtime_error:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out, "mode": "table" if table else "detail"}


def _scan_limit_up_data(today_str: str):
    """涨停扫描核心逻辑，返回结构化数据用于 JSON 和文本输出"""
    from scanner import (fetch_limit_up_pool, pre_filter, score_seal_strength,
                         get_money_flow_scores, get_sector_heat_scores,
                         score_tech_form, filter_by_price,
                         fetch_fund_flow_data, detect_market_sentiment,
                         analyze_dragon_tiger, score_stock_history, TOP_N)
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
    degraded = fund_df is None

    if not degraded:
        print("  [扫描] 第4步: 股价过滤...", file=sys.stderr)
        filtered = filter_by_price(filtered, fund_df)
        if filtered.empty:
            print("  [扫描] 过滤后为空", file=sys.stderr)
            return None

    print(f"  [扫描] 剩余 {len(filtered)} 只, 第5步: 计算各维度评分...", file=sys.stderr)
    seal_scores = score_seal_strength(filtered)
    if not degraded:
        money_scores, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    else:
        print("  [扫描] ⚠ 资金流不可用，降级评分", file=sys.stderr)
        money_scores = pd.Series(0.0, index=filtered.index)
        raw_money = pd.Series(0.0, index=filtered.index)
    sector_scores = get_sector_heat_scores(filtered, money_series=raw_money if not degraded else None)
    tech_scores = score_tech_form(filtered)

    print("  [扫描] 第6步: 并行获取预测评分...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(detect_market_sentiment, today_str): "sentiment",
            ex.submit(analyze_dragon_tiger, filtered, today_str): "lhb",
            ex.submit(score_stock_history, filtered, today_str): "history",
            ex.submit(score_community, filtered): "community",
        }
        res = {}
        for f in as_completed(futs):
            key = futs[f]
            try:
                res[key] = f.result()
            except Exception:
                pass

    sentiment_score, sentiment_level = 5.0, "未知"
    sentiment_detail = {}
    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]

    lhb_bonus = res.get("lhb")
    lhb_bonus = lhb_bonus[0] if lhb_bonus else pd.Series(0.0, index=filtered.index)

    history_scores = res.get("history")
    history_scores = history_scores[0] if history_scores is not None else pd.Series(2.5, index=filtered.index)

    community_scores = res.get("community")
    if community_scores is None:
        community_scores = pd.Series(3.5, index=filtered.index)

    money_scores = (money_scores + lhb_bonus).clip(upper=20.0)
    sentiment_multiplier = round(0.80 + sentiment_score / 10 * 0.40, 2)

    print("  [扫描] 第7步: 生成评分报告...", file=sys.stderr)
    import weight_manager
    weights = weight_manager.load_weights()
    base_totals = weight_manager.apply_weights(
        seal_scores, money_scores, sector_scores, tech_scores,
        history_scores, community_scores, weights)
    total_scores = base_totals * sentiment_multiplier

    # 取 TOP_N
    top_indices = list(total_scores.sort_values(ascending=False).head(TOP_N).index)

    def _money_str(val):
        try:
            v = float(val)
            if abs(v) >= 1e8: return f"{v/1e8:.2f}亿"
            if abs(v) >= 1e4: return f"{v/1e4:.0f}万"
            return f"{v:.0f}"
        except: return str(val)

    stocks = []
    for rank, idx in enumerate(top_indices, 1):
        row = filtered.loc[idx]
        code = str(row.get('代码', '')).strip().zfill(6)
        name = str(row.get('名称', ''))
        net = float(raw_money.get(idx, 0))
        stocks.append({
            'rank': rank,
            'code': code,
            'name': name,
            'total_score': round(float(total_scores[idx]), 1),
            'base_score': round(float(base_totals[idx]), 1),
            'seal_score': round(float(seal_scores.get(idx, 0)), 1),
            'money_score': round(float(money_scores.get(idx, 0)), 1),
            'sector_score': round(float(sector_scores.get(idx, 0)), 1),
            'tech_score': round(float(tech_scores.get(idx, 0)), 1),
            'history_score': round(float(history_scores.get(idx, 0)), 1),
            'community_score': round(float(community_scores.get(idx, 0)), 1),
            'net_money': net,
            'net_money_str': _money_str(net),
            'turnover': f"{float(row.get('换手率', 0)):.1f}",
            'seal_time': str(row.get('首次封板时间', ''))[:4],
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })

    return {
        'stocks': stocks,
        'df': filtered,
        'seal_scores': seal_scores,
        'money_scores': money_scores,
        'raw_money': raw_money,
        'sector_scores': sector_scores,
        'tech_scores': tech_scores,
        'history_scores': history_scores,
        'community_scores': community_scores,
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_multiplier': sentiment_multiplier,
        'sentiment_detail': sentiment_detail,
        'weights': weights,
        'date': today_str,
    }


def _run_limit_up_scan(today_str: str, table_mode: bool):
    """涨停池扫描 — 文本输出（复用 _scan_limit_up_data 的结果）"""
    from scanner import format_table_output, format_output

    data = _scan_limit_up_data(today_str)
    if data is None or not data['stocks']:
        print("no_data")
        return

    fmt = format_table_output if table_mode else format_output
    print(fmt(
        data['df'], data['money_scores'], data['sector_scores'],
        data['seal_scores'], data['tech_scores'],
        raw_money=data['raw_money'],
        sentiment_score=data['sentiment_score'],
        sentiment_level=data['sentiment_level'],
        sentiment_detail=data['sentiment_detail'],
        history_scores=data['history_scores'],
        community_scores=data['community_scores'],
        sentiment_multiplier=data['sentiment_multiplier'],
        weights=data['weights'],
    ))


def score_community(df):
    """简化舆情评分入口"""
    try:
        import community
        return community.score_community(df)
    except Exception:
        import pandas as pd
        return pd.Series(3.5, index=df.index)


@app.get("/api/scan/limit-up/cards")
def api_scan_limit_up_cards(refresh: bool = Query(False, description="强制刷新")):
    """涨停扫描 — 返回结构化 JSON 数据（供卡片视图使用）"""
    if not refresh:
        cached = daily_get("limit_up_cards")
        if cached:
            return cached

    from datetime import date
    print("  [涨停卡片] ========= 开始扫描 =========", file=sys.stderr)
    today = date.today().strftime("%Y%m%d")
    try:
        data = _scan_limit_up_data(today)
        if data is None:
            print("  [涨停卡片] 无数据", file=sys.stderr)
            return {"ok": True, "stocks": [], "sentiment": {}}
        print(f"  [涨停卡片] 完成, 共 {len(data['stocks'])} 只", file=sys.stderr)
        result = {
            "ok": True,
            "stocks": data['stocks'],
            "sentiment": {
                "score": data['sentiment_score'],
                "level": data['sentiment_level'],
                "multiplier": data['sentiment_multiplier'],
            },
            "date": data['date'],
        }
        daily_set("limit_up_cards", result)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "stocks": []})


@app.get("/api/scan/sector/cards")
def api_sector_cards():
    """板块热度 — 结构化数据"""
    import akshare as ak
    import pandas as pd
    from datetime import date
    print("  [板块卡片] 开始...", file=sys.stderr)
    today = date.today().strftime("%Y%m%d")
    key = f"sector_cards_{today}"
    cached = cache_get(key)
    if cached:
        print("  [板块卡片] 命中缓存", file=sys.stderr)
        return cached
    print("  [板块卡片] 拉取涨停池...", file=sys.stderr)
    try:
        limit_df = ak.stock_zt_pool_em(date=today)
        print(f"  [板块卡片] 涨停 {len(limit_df)} 只, 拉取炸板池...", file=sys.stderr)
        zhaban_df = ak.stock_zt_pool_zbgc_em(date=today)
        print(f"  [板块卡片] 炸板 {len(zhaban_df)} 只, 拉取跌停池...", file=sys.stderr)
        dieting_df = ak.stock_zt_pool_dtgc_em(date=today)
        print(f"  [板块卡片] 跌停 {len(dieting_df)} 只, 计算板块得分...", file=sys.stderr)
    except Exception as e:
        print(f"  [板块卡片] 错误: {e}", file=sys.stderr)
        return JSONResponse({"ok": False, "error": str(e), "items": []})

    industry_col = None
    for c in limit_df.columns:
        if '行业' in str(c):
            industry_col = c
            break
    if industry_col is None:
        return {"ok": True, "items": []}

    limit_counts = limit_df[industry_col].value_counts()
    zb_counts = zhaban_df[industry_col].value_counts() if not zhaban_df.empty else pd.Series(dtype=int)
    dt_counts = dieting_df[industry_col].value_counts() if not dieting_df.empty else pd.Series(dtype=int)

    all_industries = set(limit_counts.index)
    items = []
    for ind in all_industries:
        lc = int(limit_counts.get(ind, 0))
        zc = int(zb_counts.get(ind, 0)) if ind in zb_counts.index else 0
        dc = int(dt_counts.get(ind, 0)) if ind in dt_counts.index else 0
        score = min(12, 4 + lc * 2)
        efficiency = round(lc / (lc + zc) * 100, 1) if (lc + zc) > 0 else 0
        items.append({
            'name': str(ind),
            'limit_count': lc,
            'zhaban_count': zc,
            'dieting_count': dc,
            'score': score,
            'efficiency': efficiency,
        })
    items.sort(key=lambda x: x['score'], reverse=True)
    result = {"ok": True, "items": items[:15]}
    cache_put(key, result)
    return result


@app.get("/api/scan/trend/cards")
def api_trend_cards():
    """趋势扫描 — 结构化数据"""
    import akshare as ak
    import pandas as pd
    from datetime import date, timedelta, datetime
    print("  [趋势卡片] 开始...", file=sys.stderr)
    today = date.today().strftime("%Y%m%d")
    key = f"trend_cards_{today}"
    cached = cache_get(key)
    if cached:
        print("  [趋势卡片] 命中缓存", file=sys.stderr)
        return cached
    print("  [趋势卡片] 拉取昨日涨停数据...", file=sys.stderr)
    try:
        prev = ak.stock_zt_pool_previous_em(date=today)
    except:
        wd = datetime.now().weekday()
        days_back = 3 if wd == 0 else (2 if wd == 6 else 1)
        yesterday = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
        print(f"  [趋势卡片] 今日无数据, 尝试 {yesterday}...", file=sys.stderr)
        try:
            prev = ak.stock_zt_pool_previous_em(date=yesterday)
        except Exception as e:
            print(f"  [趋势卡片] 失败: {e}", file=sys.stderr)
            return {"ok": True, "items": []}

    if prev.empty:
        return {"ok": True, "items": []}

    change_col = prev.columns[3]
    name_col = prev.columns[2]
    code_col = prev.columns[1]

    prev['涨幅'] = prev[change_col].astype(float)
    trend = prev[(prev['涨幅'] >= 3) & (prev['涨幅'] < 9)].copy()
    if trend.empty:
        return {"ok": True, "items": []}

    trend = trend.sort_values('涨幅', ascending=False).head(10)
    items = []
    for _, row in trend.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        items.append({
            'code': code,
            'name': str(row[name_col]),
            'change_pct': round(float(row['涨幅']), 1),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })
    result = {"ok": True, "items": items}
    cache_put(key, result)
    return result


@app.get("/api/scan/zhaban/cards")
def api_zhaban_cards():
    """炸板分析 — 结构化数据"""
    import akshare as ak
    import pandas as pd
    from datetime import date
    print("  [炸板卡片] 开始...", file=sys.stderr)
    today = date.today().strftime("%Y%m%d")
    key = f"zhaban_cards_{today}"
    cached = cache_get(key)
    if cached:
        print("  [炸板卡片] 命中缓存", file=sys.stderr)
        return cached
    print("  [炸板卡片] 拉取炸板数据...", file=sys.stderr)
    try:
        zb = ak.stock_zt_pool_zbgc_em(date=today)
        print(f"  [炸板卡片] 获取 {len(zb)} 只", file=sys.stderr)
    except Exception as e:
        print(f"  [炸板卡片] 错误: {e}", file=sys.stderr)
        return JSONResponse({"ok": False, "error": str(e), "items": []})
    if zb.empty:
        return {"ok": True, "items": []}

    code_col = zb.columns[1]
    name_col = zb.columns[2]
    seal_col = '首次封板时间' if '首次封板时间' in zb.columns else zb.columns[11]

    items = []
    for _, row in zb.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        seal_time = str(row.get(seal_col, ''))[:4] if seal_col else '?'
        items.append({
            'code': code,
            'name': str(row[name_col]),
            'seal_time': seal_time,
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })
    result = {"ok": True, "items": items[:10]}
    cache_put(key, result)
    return result


@app.get("/api/scan/dtqiaoban/cards")
def api_dtqiaoban_cards():
    """跌停翘板 — 结构化数据"""
    import akshare as ak
    import pandas as pd
    from datetime import date
    print("  [翘板卡片] 开始...", file=sys.stderr)
    today = date.today().strftime("%Y%m%d")
    key = f"dtqiaoban_cards_{today}"
    cached = cache_get(key)
    if cached:
        print("  [翘板卡片] 命中缓存", file=sys.stderr)
        return cached
    print("  [翘板卡片] 拉取跌停数据...", file=sys.stderr)
    try:
        dt = ak.stock_zt_pool_dtgc_em(date=today)
        print(f"  [翘板卡片] 获取 {len(dt)} 只", file=sys.stderr)
    except Exception as e:
        print(f"  [翘板卡片] 错误: {e}", file=sys.stderr)
        return JSONResponse({"ok": False, "error": str(e), "items": []})
    if dt.empty:
        return {"ok": True, "items": []}

    code_col = dt.columns[1]
    name_col = dt.columns[2]

    items = []
    for _, row in dt.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        items.append({
            'code': code,
            'name': str(row[name_col]),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
        })
    result = {"ok": True, "items": items[:10]}
    cache_put(key, result)
    return result


@app.get("/api/scan/zhaban")
def api_scan_zhaban(table: bool = Query(False)):
    """炸板股反包潜力扫描"""
    from scanner import scan_zhaban
    today = date.today().strftime("%Y%m%d")
    out, err = _capture(scan_zhaban, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/trend")
def api_scan_trend(table: bool = Query(False)):
    """趋势动量股扫描"""
    from scanner import scan_trend
    today = date.today().strftime("%Y%m%d")
    out, err = _capture(scan_trend, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/sector")
def api_scan_sector(table: bool = Query(False)):
    """板块联动强度分析"""
    from scanner import scan_sector
    today = date.today().strftime("%Y%m%d")
    out, err = _capture(scan_sector, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/scan/dtqiaoban")
def api_scan_dtqiaoban(table: bool = Query(False)):
    """跌停翘板信号扫描"""
    from scanner import scan_dtqiaoban
    today = date.today().strftime("%Y%m%d")
    out, err = _capture(scan_dtqiaoban, today, table)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


@app.get("/api/backtest")
def api_backtest():
    """运行滚动回测"""
    from scanner import run_backtest
    out, err = _capture(run_backtest)
    if "[运行时错误]" in err:
        return JSONResponse({"ok": False, "error": err.strip(), "output": out})
    return {"ok": True, "output": out}


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
        today = date.today().strftime("%Y%m%d")
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
    today = date.today().strftime("%Y%m%d")
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
#  市场概览 Dashboard
# ═══════════════════════════════════════════

@app.get("/api/dashboard")
def api_dashboard(refresh: bool = Query(False, description="强制刷新")):
    """市场概览：情绪、涨停数、炸板/跌停汇总、热门板块"""
    # 每日缓存命中且不强制刷新 → 直接返回
    if not refresh:
        cached = daily_get("dashboard")
        if cached:
            return cached

    import akshare as ak
    import pandas as pd
    from scanner import fetch_limit_up_pool, detect_market_sentiment
    today = date.today().strftime("%Y%m%d")
    result = {"ok": True, "date": today}

    # 市场情绪
    try:
        score, level, detail = detect_market_sentiment(today)
        result["sentiment"] = {"score": score, "level": level}
        if detail:
            for k, v in detail.items():
                if k not in ("zhaban_count", "dieting_count"):
                    result[k] = v
    except:
        result["sentiment"] = {"score": 0, "level": "未知"}

    # 涨停池 + 行业分布
    try:
        pool = fetch_limit_up_pool()
        if pool is not None and not pool.empty:
            result["limit_up_count"] = len(pool)
            ind_col = '所属行业' if '所属行业' in pool.columns else pool.columns[15]
            top5 = pool[ind_col].value_counts().head(5)
            result["hot_sectors"] = [
                {"name": str(name), "count": int(cnt)}
                for name, cnt in top5.items()
            ]
        else:
            result["limit_up_count"] = 0
            result["hot_sectors"] = []
    except:
        result["limit_up_count"] = 0
        result["hot_sectors"] = []

    # 炸板/跌停
    for api_name, key in [("stock_zt_pool_zbgc_em", "zhaban_count"),
                           ("stock_zt_pool_dtgc_em", "dieting_count")]:
        try:
            df = getattr(ak, api_name)(date=today)
            result[key] = len(df) if df is not None and not df.empty else 0
        except:
            result[key] = 0

    daily_set("dashboard", result)
    return result


# ═══════════════════════════════════════════
#  流式扫描端点 — SSE 实时进度
# ═══════════════════════════════════════════

@app.get("/api/scan/limit-up/stream")
async def api_scan_limit_up_stream():
    """涨停扫描 — SSE 流式输出实时进度（优先使用每日缓存）"""
    today = date.today().strftime("%Y%m%d")

    async def _generate():
        # 检查每日缓存
        cached = daily_get("limit_up_cards")
        if cached:
            yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
            await asyncio.sleep(0.03)
            yield f"data: {json.dumps({'type':'complete','stocks':cached.get('stocks',[]),'sentiment':cached.get('sentiment',{}),'date':cached.get('date','')})}\n\n"
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
            old_stderr = sys.stderr
            try:
                sys.stderr = _Capture()
                data = _scan_limit_up_data(today)
                result_holder["data"] = data
                # 扫描完成后写入每日缓存
                if data:
                    cache_data = {
                        "ok": True,
                        "stocks": data['stocks'],
                        "sentiment": {
                            "score": data['sentiment_score'],
                            "level": data['sentiment_level'],
                            "multiplier": data['sentiment_multiplier'],
                        },
                        "date": data['date'],
                    }
                    daily_set("limit_up_cards", cache_data)
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                sys.stderr = old_stderr
                q.put(("done", None))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        while True:
            try:
                typ, val = q.get(timeout=0.2)
                if typ == "done":
                    break
                yield f"data: {json.dumps({'type':'progress','text':val})}\n\n"
                await asyncio.sleep(0.03)
            except queue.Empty:
                continue

        data = result_holder["data"]
        if data:
            yield f"data: {json.dumps({'type':'complete','stocks':data['stocks'],'sentiment':{'score':data['sentiment_score'],'level':data['sentiment_level'],'multiplier':data['sentiment_multiplier']},'date':data['date']})}\n\n"
        elif result_holder["error"]:
            yield f"data: {json.dumps({'type':'error','text':result_holder['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type':'error','text':'无数据'})}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════
#  前端页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(_BASE_DIR, "templates/index.html"), "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
