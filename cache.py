#!/usr/bin/env python3
"""
缓存工具模块：2小时短期缓存 + 每日收盘缓存
"""
import os
import json
import time
import pickle as _pickle
from datetime import date, datetime, timezone, timedelta

_CACHE_DIR = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "claude_stock_cache")
_CACHE_TTL = 7200

# ─── 2小时短期缓存（避免重复拉取慢 API） ───

def get(name):
    path = os.path.join(_CACHE_DIR, f"{name}.pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _CACHE_TTL:
            with open(path, 'rb') as f:
                return _pickle.load(f)
    except Exception:
        pass
    return None

def put(name, data):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CACHE_DIR, f"{name}.pkl"), 'wb') as f:
            _pickle.dump(data, f)
    except Exception:
        pass


# ─── 每日缓存（日内持久化，避免重复扫描；按日期自动隔离） ───

_CST = timezone(timedelta(hours=8))

def _trading_date() -> str:
    """返回当前交易日的日期：凌晨0点到9:30开盘前归为上一个交易日"""
    now = datetime.now(_CST)
    if now.weekday() >= 5:
        # 周末：归到上周五
        days_to_friday = now.weekday() - 4
        return (now - timedelta(days=days_to_friday)).strftime("%Y-%m-%d")
    # 工作日的 0:00-9:30 之间，归为上一个交易日
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < market_open:
        yesterday = now - timedelta(days=1)
        while yesterday.weekday() >= 5:
            yesterday -= timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")

def _daily_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"daily_{_trading_date()}_{key}.json")

def daily_get(key: str):
    path = _daily_path(key)
    try:
        if os.path.exists(path):
            # 检查缓存是否过期：工作日开盘前（<9:30）写入的缓存视为过期
            now = datetime.now(_CST)
            if now.weekday() < 5:
                mtime = os.path.getmtime(path)
                mtime_dt = datetime.fromtimestamp(mtime, _CST)
                if mtime_dt.hour < 9 or (mtime_dt.hour == 9 and mtime_dt.minute < 30):
                    os.remove(path)
                    return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None

def daily_set(key: str, data):
    # 文件名含日期，自然隔离：当日首次写入后，刷新直接命中缓存
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_daily_path(key), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass
