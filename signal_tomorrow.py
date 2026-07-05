"""兼容shim - 实际模块已移至 signals/"""
from signals.signal_tomorrow import *  # noqa
import signals.signal_tomorrow as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
