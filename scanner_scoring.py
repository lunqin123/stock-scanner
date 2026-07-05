"""兼容shim - 实际模块已移至 scoring/"""
from scoring.scanner_scoring import *  # noqa
import scoring.scanner_scoring as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
