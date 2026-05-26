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

def _daily_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"daily_{date.today().isoformat()}_{key}.json")

def daily_get(key: str):
    path = _daily_path(key)
    try:
        if os.path.exists(path):
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
