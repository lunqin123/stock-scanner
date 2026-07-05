"""兼容shim - 实际模块已移至 core/"""
from core.scanner_filters import *  # noqa
import core.scanner_filters as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
