"""清理 t1_ohlcv 中的 __NONE__ 死标记 (临时脚本)

当 akshare 失败时, _get_ohlcv_batch 会写入 'NONE' 标记, 之后永远被当作缺失。
清除这些 NONE 让主循环重新尝试 (或触发 archive fallback)。
"""
import os, glob, pickle
CACHE = r'C:\Users\16689\Desktop\stock-scanner\data\cache'

cleared = 0
for f in glob.glob(os.path.join(CACHE, 't1_ohlcv_*_v8.pkl')):
    try:
        d = pickle.load(open(f, 'rb'))
        if d == '__NONE__':
            os.remove(f)
            cleared += 1
    except Exception:
        pass

print(f'已清理 {cleared} 个 __NONE__ 占位文件')
