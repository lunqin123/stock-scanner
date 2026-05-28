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

# 缓存版本号：每次变更数据格式/运算逻辑后 +1
_CACHE_VER = 2
_CACHE_TTL = 7200

# ─── 2小时短期缓存（避免重复拉取慢 API） ───

def get(name):
    path = os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl")
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
    return os.path.join(_CACHE_DIR, f"daily_{_trading_date()}_{key}_v{_CACHE_VER}.json")

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

def _is_market_frozen() -> bool:
    """盘后 15:00 到次日 9:30 之间，以及周末，冻结每日缓存不被覆盖"""
    now = datetime.now(_CST)
    wd = now.weekday()
    if wd >= 5:
        return True  # 周末冻结
    minute = now.hour * 60 + now.minute
    if minute >= 900:  # 15:00 之后
        return True
    return False

def daily_set(key: str, data, force=False):
    # 盘后冻结：已有缓存文件时不覆盖，保留全天数据（除非 force=True）
    try:
        if not force and _is_market_frozen():
            path = _daily_path(key)
            if os.path.exists(path):
                return  # 盘后不覆盖已有缓存
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_daily_path(key), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def clear_all():
    """清除所有缓存（版本号不匹配的文件也会被清理）"""
    import glob
    try:
        kept = 0
        removed = 0
        for f in os.listdir(_CACHE_DIR):
            fp = os.path.join(_CACHE_DIR, f)
            if os.path.isfile(fp) and not f.endswith(".json"):
                # pickle 文件检查版本号
                if f"_v{_CACHE_VER}" not in f:
                    os.remove(fp)
                    removed += 1
                else:
                    kept += 1
        print(f"  [缓存清理] 删除 {removed} 个旧版本, 保留 {kept} 个当前版本", file=__import__('sys').stderr)
    except Exception:
        pass
