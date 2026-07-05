"""兼容shim - 实际模块已移至 scoring/"""
from scoring.indicators import *  # noqa
import scoring.indicators as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
