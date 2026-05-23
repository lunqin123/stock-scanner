#!/usr/bin/env python3
"""
A股超短线选股扫描器 — Web 交互界面
FastAPI 后端，封装各扫描功能为 REST API
"""
import io
import sys
import json
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="A股超短线选股扫描器", version="1.0.0")

# ── 挂载静态文件 ──
app.mount("/static", StaticFiles(directory="static"), name="static")


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

    pool = fetch_limit_up_pool()
    if pool is None or pool.empty:
        return None

    filtered = pre_filter(pool)
    if filtered.empty:
        return None

    fund_df, _ = fetch_fund_flow_data()
    if fund_df is None:
        return None

    filtered = filter_by_price(filtered, fund_df)
    if filtered.empty:
        return None

    seal_scores = score_seal_strength(filtered)
    money_scores, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    sector_scores = get_sector_heat_scores(filtered, money_series=raw_money)
    tech_scores = score_tech_form(filtered)

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
        'sentiment_score': sentiment_score,
        'sentiment_level': sentiment_level,
        'sentiment_multiplier': sentiment_multiplier,
        'sentiment_detail': sentiment_detail,
        'date': today_str,
    }


def _run_limit_up_scan(today_str: str, table_mode: bool):
    """涨停池扫描 — 文本输出（兼容原接口）"""
    from scanner import (format_table_output, format_output, TOP_N)
    import pandas as pd

    data = _scan_limit_up_data(today_str)
    if data is None:
        print("no_data")
        return

    # 重建 DataFrame 和 Series 用于 format 函数
    stocks = data['stocks']
    if not stocks:
        print("no_data")
        return

    # 重新组织数据回 format 函数需要的格式
    from scanner import fetch_limit_up_pool, pre_filter, filter_by_price, fetch_fund_flow_data
    pool = fetch_limit_up_pool()
    filtered = pre_filter(pool)
    fund_df, _ = fetch_fund_flow_data()
    if fund_df is not None:
        filtered = filter_by_price(filtered, fund_df)

    if filtered.empty:
        print("no_data")
        return

    # 重新计算一遍 Series（轻量，因为有缓存所以很快）
    from scanner import (score_seal_strength, get_money_flow_scores,
                         get_sector_heat_scores, score_tech_form,
                         analyze_dragon_tiger, score_stock_history,
                         detect_market_sentiment)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    seal_scores = score_seal_strength(filtered)
    money_scores, raw_money = get_money_flow_scores(filtered, fund_df=fund_df)
    sector_scores = get_sector_heat_scores(filtered, money_series=raw_money)
    tech_scores = score_tech_form(filtered)

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

    sentiment_score, sentiment_level, sentiment_detail = 5.0, "未知", {}
    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]

    lhb_bonus = res.get("lhb")
    lhb_bonus = lhb_bonus[0] if lhb_bonus else pd.Series(0.0, index=filtered.index)
    history_scores = res.get("history")
    history_scores = history_scores[0] if history_scores else pd.Series(2.5, index=filtered.index)
    community_scores = res.get("community")
    if community_scores is None:
        community_scores = pd.Series(3.5, index=filtered.index)

    money_scores = (money_scores + lhb_bonus).clip(upper=20.0)
    sentiment_multiplier = round(0.80 + sentiment_score / 10 * 0.40, 2)

    import weight_manager
    weights = weight_manager.load_weights()
    fmt = format_table_output if table_mode else format_output
    print(fmt(filtered, money_scores, sector_scores, seal_scores, tech_scores,
              raw_money=raw_money,
              sentiment_score=sentiment_score,
              sentiment_level=sentiment_level,
              sentiment_detail=sentiment_detail,
              history_scores=history_scores,
              community_scores=community_scores,
              sentiment_multiplier=sentiment_multiplier,
              weights=weights))


def score_community(df):
    """简化舆情评分入口"""
    try:
        import community
        return community.score_community(df)
    except Exception:
        import pandas as pd
        return pd.Series(3.5, index=df.index)


@app.get("/api/scan/limit-up/cards")
def api_scan_limit_up_cards():
    """涨停扫描 — 返回结构化 JSON 数据（供卡片视图使用）"""
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    try:
        data = _scan_limit_up_data(today)
        if data is None:
            return {"ok": True, "stocks": [], "sentiment": {}}
        return {
            "ok": True,
            "stocks": data['stocks'],
            "sentiment": {
                "score": data['sentiment_score'],
                "level": data['sentiment_level'],
                "multiplier": data['sentiment_multiplier'],
            },
            "date": data['date'],
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "stocks": []})


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
#  前端页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
