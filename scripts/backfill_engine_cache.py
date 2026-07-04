"""Backfill 2h 引擎缓存到 ``persistent_`` 前缀副本 (一次性脚本, 已废弃保留兼容)

历史: 早期 ``backtest_engine.py`` 通过 ``persistent_put('persistent_engine_xxx', ...)``
写入带 ``persistent_`` 前缀的文件, 后来改成直接 ``persistent_put('engine_xxx', ...)`` —
本脚本作为那段过渡期的桥接工具, 用来 copy ``engine_*`` → ``persistent_engine_*``。

当前 ``scripts/backfill_archive.py`` 已同时接受两种前缀格式, 这个脚本不再有必要
每日运行 — 留着仅供一次性历史回填/紧急情况使用。

⚠️ glob 已升级到通配 ``*engine_*_v*.pkl`` (兼容 v8/v9/v10), 不再硬编码 v8。
"""
import os, pickle, shutil, glob
import pandas as pd
import re

CACHE = r'C:\Users\16689\Desktop\stock-scanner\data\cache'

# 仅匹配 `engine_xxx_<date>_v<N>.pkl` 这一类 (其他 cache 文件如 ``t1_ohlcv_*``
# 不属于此桥接范围), glob + 正则二次过滤防止误伤。
_FILENAME_RE = re.compile(
    r'^(?P<pool>engine_(?:limit_up|trend|reversal|zhaban|dtqiaoban))'
    r'_(?P<date>\d{8})_v(?P<ver>\d+)\.pkl$'
)

upgraded = 0
skipped = 0
for src in sorted(glob.glob(os.path.join(CACHE, '*engine_*_v*.pkl'))):
    name = os.path.basename(src)
    if name.startswith('persistent_'):
        # 已经是 persistent_ 前缀, 跳过
        skipped += 1
        continue
    if not _FILENAME_RE.match(name):
        # 不是 pool cache (例如 ``fund_flow.pkl``)
        continue
    dst_name = 'persistent_' + name
    dst = os.path.join(CACHE, dst_name)
    try:
        d = pickle.load(open(src, 'rb'))
        # 跳过 None marker
        if d is None or (isinstance(d, str) and d == '__NONE__'):
            print(f'  [skip] {name}: NONE marker')
            skipped += 1
            continue
        if hasattr(d, 'empty') and d.empty:
            print(f'  [skip] {name}: empty DataFrame')
            skipped += 1
            continue
        with open(dst, 'wb') as out:
            pickle.dump(d, out)
        size = os.path.getsize(dst)
        rows = len(d) if hasattr(d, '__len__') else '?'
        print(f'  [ok] {name} -> {dst_name} ({size}B, {rows} rows)')
        upgraded += 1
    except Exception as e:
        print(f'  [err] {name}: {e}')

print(f'\n=== Summary: upgraded={upgraded}, skipped={skipped} ===')
