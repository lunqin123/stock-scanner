"""兼容shim - 实际模块已移至 core/"""
from core.cache import *  # noqa
import core.cache as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
