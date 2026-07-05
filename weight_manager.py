"""兼容shim - 实际模块已移至 scoring/"""
from scoring.weight_manager import *  # noqa
import scoring.weight_manager as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
