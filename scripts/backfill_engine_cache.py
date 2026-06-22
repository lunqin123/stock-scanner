"""Backfill 2h 引擎缓存到持久化 (一次性脚本)

读取 data/cache/engine_*.pkl, 复制为 data/cache/persistent_engine_*.pkl,
让回测引擎下次跑能秒读不重拉。
"""
import os, pickle, shutil, glob
import pandas as pd

CACHE = r'C:\Users\16689\Desktop\stock-scanner\data\cache'

upgraded = 0
skipped = 0
for src in sorted(glob.glob(os.path.join(CACHE, 'engine_*_v8.pkl'))):
    name = os.path.basename(src)
    if name.startswith('persistent_'):
        skipped += 1
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
