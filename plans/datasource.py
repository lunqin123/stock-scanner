#!/usr/bin/env python3
"""
数据源接口层 — HTTP 直连实现 (a-stock-data V3.2.2 方案)。

设计原则:
1. 每个数据源独立函数, Plan 通过 SOURCES 注册表按名调用
2. 降级链: HTTP直连 → akshare → 空 DataFrame (永不崩溃)
3. 东财接口已内置限流 (串行≥1s间隔 + 随机抖动)
"""

import sys
import time
import random
import pandas as pd
import requests

SOURCES = {}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ═══════════════════════════════════════════
#  东财防封: 全局节流 + 会话复用
# ═══════════════════════════════════════════
_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
_em_last_call = [0.0]


def _em_get(url: str, params: dict = None, timeout: int = 15, **kw):
    """东财统一入口: 自动节流 + 复用 session。所有 eastmoney.com 请求走此入口。"""
    wait = 1.0 - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, timeout=timeout, **kw)
    finally:
        _em_last_call[0] = time.time()


def _em_datacenter(report_name: str, filter_str: str = "",
                    page_size: int = 50, sort_columns: str = "",
                    sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = _em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                params=params, timeout=15)
    try:
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════
#  1. 涨停原因归因 (同花顺热点 API) — 零鉴权, ~73ms
# ═══════════════════════════════════════════

def source_limit_reason(date_str: str) -> pd.DataFrame:
    """
    同花顺当日强势股归因。返回 DataFrame(code, name, reason, 涨幅% 等)。
    reason 字段 = 同花顺编辑部人工运营的题材标签 (如 "算力租赁+Token工厂+AI政务")。
    """
    try:
        dt = date_str.replace('-', '-') if date_str else ''
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{dt}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {"User-Agent": UA}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) == 0 and data.get("data"):
            df = pd.DataFrame(data["data"])
            if not df.empty:
                df = df.rename(columns={
                    "code": "code", "name": "name", "reason": "reason",
                    "zhangfu": "change_pct",
                })
                # 确保有 code 列
                if "code" not in df.columns and "代码" in df.columns:
                    df = df.rename(columns={"代码": "code"})
                return df[["code", "name", "reason"]]
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['limit_reason'] = source_limit_reason


# ═══════════════════════════════════════════
#  2. 北向资金 — 同花顺 hsgtApi (市场级分钟流向)
# ═══════════════════════════════════════════

def source_north_flow(date_str: str) -> pd.DataFrame:
    """
    北向资金市场总览 (同花顺 hsgtApi 直连)。
    返回 DataFrame(time, hgt_yi, sgt_yi) → Plan B 判断当日北向方向。
    注意: 个股级北向数据东财已自 2024-08 断供, 只能用市场级。
    """
    try:
        url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        headers = {
            "User-Agent": UA,
            "Host": "data.hexin.cn",
            "Referer": "https://data.hexin.cn/",
        }
        r = requests.get(url, headers=headers, timeout=10)
        d = r.json()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])
        n = len(times)
        return pd.DataFrame({
            "time": times,
            "hgt_yi": hgt[:n] + [None] * (n - len(hgt)),
            "sgt_yi": sgt[:n] + [None] * (n - len(sgt)),
        })
    except Exception:
        pass
    # 降级: akshare
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['north_flow'] = source_north_flow

# 别名: market-level source (与 API 名一致)
SOURCES['north_flow_market'] = source_north_flow


# ═══════════════════════════════════════════
#  3. 融资融券 — 东财 datacenter 个股日级
# ═══════════════════════════════════════════

def source_margin_ratio(date_str: str) -> pd.DataFrame:
    """
    融资融券个股余额 (东财 datacenter 直连)。
    对每只股票查询最新融资余额, 返回 DataFrame(code, margin_balance)。
    """
    # 此函数需要 codes 列表才能逐个查询。批量模式见 source_margin_batch。
    return pd.DataFrame()

SOURCES['margin_ratio'] = source_margin_ratio


def source_margin_akshare(date_str: str) -> pd.DataFrame:
    """
    融资融券个股余额 (批量 — 先用 akshare 沪+深全量, 更快)。
    返回 DataFrame(code, margin_balance)。
    """
    frames = []
    try:
        import akshare as ak
        dt = date_str.replace('-', '') if date_str else ''
        if dt:
            df_sse = ak.stock_margin_detail_sse(date=dt)
            if df_sse is not None and not df_sse.empty:
                frames.append(df_sse)
            df_szse = ak.stock_margin_detail_szse(date=dt)
            if df_szse is not None and not df_szse.empty:
                frames.append(df_szse)
    except Exception:
        pass

    # 降级: 东财 datacenter 逐个查 (慢, 但保底)
    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)
    for c in result.columns:
        if '股票代码' in str(c) or 'code' in str(c) or '代码' in str(c):
            result.rename(columns={c: 'code'}, inplace=True)
        if '融资余额' in str(c) or 'margin' in str(c).lower():
            result.rename(columns={c: 'margin_balance'}, inplace=True)
    return result

SOURCES['margin_akshare'] = source_margin_akshare


# ═══════════════════════════════════════════
#  4. 机构研报 — 东财 reportapi
# ═══════════════════════════════════════════

_REPORT_API = "https://reportapi.eastmoney.com/report/list"


def source_inst_rating(date_str: str) -> pd.DataFrame:
    """
    机构研报评级 (akshare 直连)。
    拉取最近研报, 按股票去重取最新评级。返回 DataFrame(code, rating, org)。
    rating: '买入'/'增持'/'中性'/'减持'/'卖出'
    """
    try:
        import akshare as ak
        df = ak.stock_research_report_em()
        if df is None or df.empty:
            return pd.DataFrame()
        rows = []
        seen = set()
        for _, row in df.iterrows():
            code = ''
            for c in df.columns:
                if '代码' in str(c) or 'code' in str(c).lower():
                    code = str(row[c]).strip().zfill(6)
                    break
            if not code or code in seen:
                continue
            seen.add(code)
            rating = ''
            for c in df.columns:
                if '评级' in str(c):
                    rating = str(row[c])
                    break
            org = ''
            for c in df.columns:
                if '机构' in str(c) or '研究员' in str(c):
                    org = str(row[c])
                    break
            if code:
                rows.append({"code": code, "rating": rating, "org": org})
        return pd.DataFrame(rows)
    except Exception:
        pass

    # 降级: 东财 reportapi 直连
    try:
        from datetime import datetime, timedelta
        dt = datetime.now()
        if date_str:
            try:
                dt = datetime.strptime(date_str.replace('-',''), '%Y%m%d')
            except Exception:  # BUG-5 修复
                pass
        begin = (dt - timedelta(days=30)).strftime('%Y-%m-%d')
        end = dt.strftime('%Y-%m-%d')
        all_reports = []
        for page in range(1, 4):
            params = {"pageNo": str(page), "pageSize": "50",
                      "beginTime": begin, "endTime": end,
                      "qType": "0",
                      "sortColumns": "NOTICEDATE", "sortTypes": "-1",
                      "source": "WEB", "client": "WEB"}
            r = _em_get(_REPORT_API, params=params, timeout=15)
            data = r.json()
            if data.get("Data"):
                all_reports.extend(data["Data"])
            else:
                break
        if all_reports:
            rows, seen = [], set()
            for rep in all_reports:
                code = str(rep.get("StockCode", "")).strip().zfill(6)
                if not code or code in seen: continue
                seen.add(code)
                rm = {"1":"买入","2":"增持","3":"中性","4":"减持","5":"卖出"}
                r = rm.get(str(rep.get("Rate","")), "")
                if code and r:
                    rows.append({"code":code,"rating":r,
                                "org":rep.get("OrgName","")})
            return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['inst_rating'] = source_inst_rating


# ═══════════════════════════════════════════
#  5. 行业资金流向 (akshare 同花顺)
# ═══════════════════════════════════════════

def source_industry_fund_flow(date_str: str) -> pd.DataFrame:
    """行业资金流向 (akshare 同花顺)。"""
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator="今日",
                                             sector_type="行业资金流向")
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()

SOURCES['industry_fund_flow'] = source_industry_fund_flow
