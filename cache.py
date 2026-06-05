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
_CACHE_VER = 7  # v6→v7: 评分重构(seal22+黄金奖励, sector合并, tech简化, principal增强)
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
        with open(os.path.join(_CACHE_DIR, f"{name}_v{_CACHE_VER}.pkl"), 'wb') as f:
            _pickle.dump(data, f)
    except Exception:
        pass


# ─── 每日缓存（日内持久化，避免重复扫描；按日期自动隔离） ───

_CST = timezone(timedelta(hours=8))

def _trading_date() -> str:
    """返回当前交易日的日期：凌晨/周末/节假日归为上一个交易日"""
    now = datetime.now(_CST)
    d = now
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        d = now - timedelta(hours=12)
    for _ in range(10):
        d_str = d.strftime("%Y-%m-%d")
        yyyymmdd = d.strftime("%Y%m%d")
        if d.weekday() < 5 and _is_trading_day(yyyymmdd):
            return d_str
        d -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")  # fallback

def _daily_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"daily_{_trading_date()}_{key}_v{_CACHE_VER}.json")

def daily_get(key: str):
    path = _daily_path(key)
    try:
        if os.path.exists(path):
            now = datetime.now(_CST)
            mtime = os.path.getmtime(path)
            mtime_dt = datetime.fromtimestamp(mtime, _CST)
            # 缓存文件是前一个交易日创建的 → 过期（如周五缓存周一看）
            today_t = _trading_date()
            mtime_file_date = mtime_dt.strftime("%Y-%m-%d")
            if mtime_file_date < today_t:
                os.remove(path)
                return None
            # 今日但开盘前（9:30前）写入的缓存视为过期
            if _is_trading_day(now.strftime("%Y%m%d")) and mtime_file_date == today_t:
                if mtime_dt.hour < 9 or (mtime_dt.hour == 9 and mtime_dt.minute < 30):
                    os.remove(path)
                    return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None

_CALENDAR_CACHE = None
_CALENDAR_MTIME = 0

def _load_trading_calendar():
    """加载 A 股交易日历（缓存7天，含节假日）"""
    global _CALENDAR_CACHE, _CALENDAR_MTIME
    now = time.time()
    if _CALENDAR_CACHE is not None and now - _CALENDAR_MTIME < 604800:
        return _CALENDAR_CACHE  # 7天内不重复拉取
    path = os.path.join(_CACHE_DIR, "trading_calendar.txt")
    dates = set()
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < 604800:
            with open(path, 'r') as f:
                dates = set(line.strip() for line in f)
        else:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                col = df.columns[0]
                for d in df[col]:
                    dates.add(str(d).replace('-', '')[:8])
                os.makedirs(_CACHE_DIR, exist_ok=True)
                with open(path, 'w') as f:
                    for d in sorted(dates):
                        f.write(d + '\n')
    except Exception:
        pass
    _CALENDAR_CACHE = dates
    _CALENDAR_MTIME = now
    return dates


def _is_trading_day(d_str: str) -> bool:
    """判断 YYYYMMDD 是否为 A 股交易日"""
    return d_str in _load_trading_calendar()


def _is_market_frozen() -> bool:
    """盘后 15:00 起冻结每日缓存，节假日同样冻结"""
    now = datetime.now(_CST)
    wd = now.weekday()
    if wd >= 5 or not _is_trading_day(now.strftime("%Y%m%d")):
        return True
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
