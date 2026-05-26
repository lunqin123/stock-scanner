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
from fastapi import FastAPI, Query, Request
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
    sentiment_ok = False  # 标记情绪是否成功获取，用于缓存决策
    if res.get("sentiment"):
        sentiment_score, sentiment_level, sentiment_detail = res["sentiment"]
        sentiment_ok = True
    else:
        # 第一次失败，重试一次（同步，避免本次请求缓存错误的"未知"）
        print("  [扫描] ⚠ 市场情绪首次获取失败，重试中...", file=sys.stderr)
        try:
            retry = detect_market_sentiment(today_str)
            if retry:
                sentiment_score, sentiment_level, sentiment_detail = retry
                sentiment_ok = True
                print(f"  [扫描] ✅ 重试成功: {sentiment_level}", file=sys.stderr)
        except Exception as e2:
            print(f"  [扫描] ❌ 重试仍失败: {e2}", file=sys.stderr)

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
        'sentiment_ok': sentiment_ok,
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
        # 仅情绪成功才缓存，避免"未知"被反复命中
        if data.get('sentiment_ok'):
            daily_set("limit_up_cards", result)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "stocks": []})


@app.get("/api/scan/sector/cards")
def api_sector_cards():
    """板块热度 — 结构化数据（增强版：含成分股 + 可跳转）"""
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

    zb_industry_col = None
    if not zhaban_df.empty:
        for c in zhaban_df.columns:
            if '行业' in str(c):
                zb_industry_col = c
                break
    dt_industry_col = None
    if not dieting_df.empty:
        for c in dieting_df.columns:
            if '行业' in str(c):
                dt_industry_col = c
                break

    limit_counts = limit_df[industry_col].value_counts()
    zb_counts = zhaban_df[zb_industry_col].value_counts() if not zhaban_df.empty and zb_industry_col else pd.Series(dtype=int)
    dt_counts = dieting_df[dt_industry_col].value_counts() if not dieting_df.empty and dt_industry_col else pd.Series(dtype=int)

    all_industries = set(limit_counts.index)
    items = []
    for ind in all_industries:
        lc = int(limit_counts.get(ind, 0))
        zc = int(zb_counts.get(ind, 0)) if ind in zb_counts.index else 0
        dc = int(dt_counts.get(ind, 0)) if ind in dt_counts.index else 0
        score = min(12, 4 + lc * 2)
        efficiency = round(lc / (lc + zc) * 100, 1) if (lc + zc) > 0 else 0

        # 收集该板块的涨停成分股
        sector_stocks = limit_df[limit_df[industry_col] == ind]
        stock_list = []
        for _, r in sector_stocks.iterrows():
            stock_list.append({
                'code': str(r.iloc[1]).strip().zfill(6),
                'name': str(r.iloc[2]),
                'turnover': float(r.iloc[8]) if str(r.iloc[8]) != '--' else 0,
                'seal_time': str(int(float(r.iloc[10]))) if pd.notna(r.iloc[10]) and r.iloc[10] != '--' else '0000',
                'seal_fund': float(r.iloc[9]) if str(r.iloc[9]) != '--' else 0,
            })

        items.append({
            'name': str(ind),
            'url': f"https://www.10jqka.com.cn/#/search/{str(ind)}",
            'limit_count': lc,
            'zhaban_count': zc,
            'dieting_count': dc,
            'score': score,
            'efficiency': efficiency,
            'stocks': stock_list[:6],
            'sector_code': '',
        })
    items.sort(key=lambda x: x['score'], reverse=True)
    result = {"ok": True, "items": items[:15]}
    cache_put(key, result)
    return result


@app.get("/api/scan/trend/cards")
def api_trend_cards():
    """趋势扫描 — 结构化数据（含量价分析、板块、跳转）"""
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
    def _fetch(d):
        return ak.stock_zt_pool_previous_em(date=d)
    prev = pd.DataFrame()
    for attempt in [today, None]:
        try:
            if attempt is None:
                wd = datetime.now().weekday()
                db = 3 if wd == 0 else (2 if wd == 6 else 1)
                attempt = (datetime.now() - timedelta(days=db)).strftime("%Y%m%d")
            prev = _fetch(attempt)
            if not prev.empty: break
        except: continue
    if prev.empty:
        return {"ok": True, "items": []}

    chg_col = prev.columns[3]
    name_col = prev.columns[2]
    code_col = prev.columns[1]
    price_col = prev.columns[4]
    turnover_col = prev.columns[9]
    vol_col = prev.columns[6]
    seal_stat_col = prev.columns[14] if len(prev.columns) > 14 else None
    industry_col = prev.columns[15] if len(prev.columns) > 15 else None

    prev['涨幅'] = prev[chg_col].astype(float)
    trend = prev[(prev['涨幅'] >= 3) & (prev['涨幅'] < 9)].copy()
    if trend.empty:
        return {"ok": True, "items": []}
    trend = trend.sort_values('涨幅', ascending=False).head(10)

    items = []
    for _, row in trend.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        chg = round(float(row['涨幅']), 1)
        price = float(row[price_col])
        turnover = float(row[turnover_col]) if pd.notna(row[turnover_col]) else 0
        volume = float(row[vol_col]) if pd.notna(row[vol_col]) else 0
        industry = str(row[industry_col]) if industry_col and pd.notna(row[industry_col]) else ''
        seal_stat = str(row[seal_stat_col]) if seal_stat_col and pd.notna(row[seal_stat_col]) else ''
        consecutive = 0
        if '/' in seal_stat:
            try: consecutive = int(seal_stat.split('/')[1])
            except: pass

        # 量价分析
        signals = []
        if chg >= 7: signals.append("强势续涨")
        elif chg >= 5: signals.append("量价齐升")
        else: signals.append("温和上涨")
        if turnover > 15: signals.append("高换手活跃")
        elif turnover > 8: signals.append("放量健康")
        elif turnover > 3: signals.append("温和放量")
        else: signals.append("缩量整理")
        if consecutive >= 2: signals.append(f"{consecutive}连板")

        # 策略
        if chg >= 7: advice = "沿5日线持有，破5日线止盈"
        elif chg >= 5: advice = "趋势良好，持有为主"
        elif chg >= 3: advice = "观察持续性，放量可加仓"
        else: advice = "动能偏弱，等待放量确认"

        items.append({
            'code': code,
            'name': str(row[name_col]),
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'change_pct': chg,
            'price': price,
            'turnover': round(turnover, 1),
            'volume': round(volume / 1e8, 2) if volume > 1e8 else round(volume / 1e4, 0),
            'volume_unit': '亿' if volume > 1e8 else '万',
            'industry': industry,
            'consecutive': consecutive,
            'signals': signals,
            'advice': advice,
        })
    result = {"ok": True, "items": items}
    cache_put(key, result)
    return result


@app.get("/api/scan/zhaban/cards")
def api_zhaban_cards():
    """炸板分析 — 结构化数据（含评分、分析、跳转）"""
    import akshare as ak
    import pandas as pd
    from datetime import date
    import numpy as np
    from scanner import fetch_fund_flow_data, get_money_flow_scores
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

    df = zb.copy()
    # 过滤 ST/688/8xx
    name_col = df.columns[2]
    mask = ~df[name_col].astype(str).str.startswith(('ST', '*ST'), na=False)
    df = df[mask]
    df = df[~df.iloc[:, 1].astype(str).str.startswith(('68', '8'))]

    # 过滤市值/股价
    if '流通市值' in df.columns:
        df = df[df['流通市值'].astype(float) <= 200 * 1e8]
    price_col = df.columns[4]
    df = df[df[price_col].astype(float) <= 60]

    if df.empty:
        return {"ok": True, "items": []}

    # 评分
    seal_time_col = '首次封板时间' if '首次封板时间' in df.columns else df.columns[11]
    seal_fund_col = '封板资金' if '封板资金' in df.columns else df.columns[14]
    zhaban_count_col = '炸板次数' if '炸板次数' in df.columns else df.columns[12]
    turnover_col = '换手率' if '换手率' in df.columns else df.columns[9]
    industry_col = '所属行业' if '所属行业' in df.columns else df.columns[15]

    def _time_score(t):
        t = str(t).strip()
        try:
            if len(t) < 4: return 5
            h, m = int(t[:2]), int(t[2:4])
            minutes = h * 60 + m
            raw = 1.0 - (minutes - 570) / 300.0
            return max(0, min(10, raw * 10))
        except: return 5

    # 资金面
    fund_df, _ = fetch_fund_flow_data()
    money_scores = pd.Series(0.0, index=df.index)
    raw_money = pd.Series(0.0, index=df.index)
    if fund_df is not None:
        money_scores, raw_money = get_money_flow_scores(df, fund_df=fund_df)

    items = []
    for idx in df.index:
        row = df.loc[idx]
        code = str(row.iloc[1]).strip().zfill(6)
        name = str(row.iloc[2])

        seal_time = str(row.get(seal_time_col, ''))[:4]
        seal_fund = float(row.get(seal_fund_col, 0)) if pd.notna(row.get(seal_fund_col, None)) else 0
        zb_times = int(float(row.get(zhaban_count_col, 0))) if pd.notna(row.get(zhaban_count_col, None)) else 0
        turnover = float(row.get(turnover_col, 0)) if pd.notna(row.get(turnover_col, None)) else 0
        industry = str(row.get(industry_col, ''))
        price = float(row.iloc[4])

        # 封板质量 (0-25)
        seal_quality = _time_score(seal_time)
        fund_score = min(10, seal_fund / 1e8 * 2)
        zb_penalty = max(0, 5 - zb_times * 2)
        seal_total = min(25, seal_quality + fund_score + zb_penalty)

        # 资金承接 (0-20)
        money_val = float(raw_money.get(idx, 0))
        money_scaled = min(20, max(0, float(money_scores.get(idx, 0))))
        total_score = seal_total + money_scaled

        # 换手评分 (0-10)
        if turnover > 20: turn_score = 8
        elif turnover > 10: turn_score = 10
        elif turnover > 5: turn_score = 7
        elif turnover > 2: turn_score = 4
        else: turn_score = 2
        total_score += turn_score

        # 板块热度 (0-12)
        sector_score = 6
        total_score += sector_score

        total_score = min(100, total_score)

        # 信号标签
        signals = []
        if seal_time:
            h = int(seal_time[:2])
            if h < 10: signals.append("早盘封板")
            elif h < 11: signals.append("上午封板")
            else: signals.append("午后封板")
        signals.append(f"炸板{zb_times}次")
        if money_val > 1e8: signals.append("资金承接强")
        elif money_val > 1e7: signals.append("有资金承接")
        else: signals.append("资金流出")
        if turnover > 15: signals.append("高换手")
        elif turnover > 8: signals.append("换手适中")

        # 策略
        if total_score >= 70: advice = "反包潜力高，竞价高开放量可参与"
        elif total_score >= 50: advice = "竞价观察，高开放量可博弈反包"
        elif total_score >= 35: advice = "仅观望，需竞价放量确认"
        else: advice = "不建议参与，资金面偏弱"

        items.append({
            'code': code,
            'name': name,
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'score': int(total_score),
            'price': price,
            'seal_time': seal_time,
            'seal_fund': seal_fund,
            'zhaban_times': zb_times,
            'turnover': round(turnover, 1),
            'industry': industry,
            'net_money': round(money_val, 0),
            'signals': signals,
            'advice': advice,
        })

    items.sort(key=lambda x: x['score'], reverse=True)
    result = {"ok": True, "items": items[:10]}
    cache_put(key, result)
    return result


@app.get("/api/scan/dtqiaoban/cards")
def api_dtqiaoban_cards():
    """跌停翘板 — 结构化数据（含评分、分析、跳转）"""
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

    # 列索引
    code_col = 1; name_col = 2; change_col = 3; price_col = 4
    turnover_col = 9 if len(dt.columns) > 9 else None
    seal_fund_col = 10 if len(dt.columns) > 10 else None
    seal_time_col = 11 if len(dt.columns) > 11 else None
    deal_col = 12 if len(dt.columns) > 12 else None
    cont_col = 13 if len(dt.columns) > 13 else None

    def safe_float(v, default=0):
        try: return float(v) if pd.notna(v) and v != '--' else default
        except: return default

    items = []
    for _, row in dt.iterrows():
        code = str(row.iloc[code_col]).strip().zfill(6)
        name = str(row.iloc[name_col])

        # 过滤 ST/688/8xx
        if name.startswith(('ST', '*ST')) or code.startswith(('68', '8')): continue
        if safe_float(row.iloc[6] if len(row) > 6 else 0) > 200 * 1e8: continue

        deal_val = safe_float(row.iloc[deal_col]) if deal_col else 0
        seal_val = safe_float(row.iloc[seal_fund_col]) if seal_fund_col else 0
        cont_val = int(safe_float(row.iloc[cont_col])) if cont_col else 0
        turn_val = safe_float(row.iloc[turnover_col]) if turnover_col else 0

        # 评分
        total = 0
        signals = []

        # 放量信号 (0-25)
        if deal_val > 5000e4: total += 25; signals.append("巨量翘板")
        elif deal_val > 1000e4: total += 20; signals.append("放量翘板")
        elif deal_val > 100e4: total += 12; signals.append("微量翘板")
        else: total += 5; signals.append("无量跌停")

        # 封单变化 (0-25)
        if seal_val < 100e4: total += 25; signals.append("封单极小")
        elif seal_val < 1000e4: total += 20; signals.append("封单偏小")
        elif seal_val < 5000e4: total += 10; signals.append("封单适中")
        else: total += 3; signals.append("封单巨大")

        # 连续跌停 (0-25)
        if cont_val >= 3: total += 25; signals.append(f"N{cont_val}板超跌")
        elif cont_val == 2: total += 18; signals.append(f"连跌{cont_val}板")
        elif cont_val == 1: total += 10; signals.append("首板跌停")
        else: total += 5

        # 换手 (0-15)
        if turn_val > 10: total += 15; signals.append("高换手承接")
        elif turn_val > 5: total += 10; signals.append("有换手")
        elif turn_val > 1: total += 5; signals.append("少量换手")
        else: total += 2

        # 跌停时间 (0-10)
        st = ''
        if seal_time_col:
            raw_t = row.iloc[seal_time_col]
            if pd.notna(raw_t):
                st = str(raw_t).strip()
                try:
                    h, m = int(st[:2]), int(st[2:4])
                    mins = h * 60 + m
                    if mins >= 840: total += 10; signals.append("尾盘跌停")
                    elif mins >= 600: total += 7; signals.append("午后跌停")
                    elif mins >= 330: total += 3; signals.append("早盘跌停")
                    else: total += 1; signals.append("开盘跌停")
                except: total += 5

        total = min(100, total)

        # 策略建议
        if total >= 70: advice = "竞价关注，放量高开可博弈反抽，目标+3%~+5%"
        elif total >= 50: advice = "竞价观察，需放量确认，否则观望"
        elif total >= 35: advice = "仅观望，需竞价放量确认方向"
        else: advice = "无量封死，不建议参与"

        items.append({
            'code': code,
            'name': name,
            'url': f"https://stockpage.10jqka.com.cn/{code}/",
            'score': total,
            'price': safe_float(row.iloc[price_col]),
            'change': safe_float(row.iloc[change_col]),
            'turnover': round(turn_val, 1),
            'seal_fund': seal_val,
            'consecutive': cont_val,
            'seal_time': st,
            'signals': signals,
            'advice': advice,
        })

    items.sort(key=lambda x: x['score'], reverse=True)
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


@app.get("/api/indicators/cards")
def api_indicators_cards():
    from indicators import run_enhanced, calc_seal_ratio, calc_sector_leadership, calc_sector_leader_label
    from scanner import fetch_limit_up_pool, pre_filter
    from datetime import date
    import pandas as pd
    today = date.today().strftime("%Y%m%d")
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
        except: pass
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
    return {"ok": True, "items": items[:10]}

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
            cap = _Capture()
            old_stderr = sys.stderr
            old_stdout = sys.stdout
            try:
                sys.stderr = cap
                sys.stdout = cap
                data = _scan_limit_up_data(today)
                result_holder["data"] = data
                if data and data.get('sentiment_ok'):
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
                sys.stdout = old_stdout
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
                except: pass
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
def api_indicators_stream():
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    cached = daily_get("indicators")
    if cached:
        async def _cached():
            yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
            yield f"data: {json.dumps({'type':'complete', **cached})}\n\n"
        return StreamingResponse(_cached(), media_type="text/event-stream")
    from indicators import run_enhanced
    from scanner import fetch_limit_up_pool, pre_filter
    def run():
        df = fetch_limit_up_pool()
        if df is None or df.empty: return {"ok": False, "output": "今日无涨停数据"}
        df = pre_filter(df)
        result, _ = run_enhanced(df, today_str=today)
        return {"ok": True, "output": result}
    return StreamingResponse(_cached_stream(_stream_scan_generic(run, lambda d: d, on_success=lambda d: daily_set("indicators", d))), media_type="text/event-stream")


@app.get("/api/community/stream")
def api_community_stream():
    today = date.today().strftime("%Y%m%d")
    cached = daily_get("community")
    if cached:
        async def _cached():
            yield f"data: {json.dumps({'type':'progress','text':'📦 使用缓存数据...'})}\n\n"
            yield f"data: {json.dumps({'type':'complete', **cached})}\n\n"
        return StreamingResponse(_cached(), media_type="text/event-stream")
    import community
    from scanner import fetch_limit_up_pool
    def run():
        df = fetch_limit_up_pool()
        if df is None or df.empty: return {"ok": True, "output": "今日无涨停数据"}
        txt, _ = community.run(df, top_n=10)
        return {"ok": True, "output": txt if txt.strip() else "暂无舆情数据"}
    return StreamingResponse(_cached_stream(_stream_scan_generic(run, lambda d: d, on_success=lambda d: daily_set("community", d))), media_type="text/event-stream")


@app.get("/api/community/cards")
def api_community_cards():
    """舆情监测 — 结构化卡片数据（含评分解析、行情背景）"""
    from datetime import date
    import community as comm
    import pandas as pd
    from scanner import fetch_limit_up_pool
    today = date.today().strftime("%Y%m%d")
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
    return {"ok": True, "items": items[:10]}


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

from datetime import datetime, timedelta, timezone

_MARKET_CST = timezone(timedelta(hours=8))

def get_market_status():
    """返回当前市场状态: 'trading' / 'closed' / 'weekend'"""
    now = datetime.now(_MARKET_CST)
    wd = now.weekday()
    if wd >= 5:
        return "weekend"
    minute_of_day = now.hour * 60 + now.minute
    # 交易时段 9:30-11:30, 13:00-15:00
    if (570 <= minute_of_day < 690) or (780 <= minute_of_day < 900):
        return "trading"
    return "closed"


@app.get("/api/market-status")
def api_market_status():
    status = get_market_status()
    return {"ok": True, "status": status}


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

    # 后台拉取并重启
    def _deploy():
        try:
            os.chdir("/home/ubuntu/stock-scanner")
            subprocess.run(["git", "pull"], capture_output=True, timeout=30)
            subprocess.run(["sudo", "systemctl", "restart", "stock-scanner"], capture_output=True, timeout=30)
        except Exception as e:
            print(f"[Webhook] 部署失败: {e}", file=sys.stderr)

    threading.Thread(target=_deploy, daemon=True).start()
    return {"ok": True, "event": "push", "action": "deploying"}


# ═══════════════════════════════════════════
#  前端页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(_BASE_DIR, "templates/index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ═══════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
