"""兼容shim - 实际模块已移至 signals/"""
from signals.recommendation_tracker import *  # noqa
import signals.recommendation_tracker as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
