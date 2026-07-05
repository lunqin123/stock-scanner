"""兼容shim - 实际模块已移至 data_layer/"""
from data_layer.scanner_scans import *  # noqa
import data_layer.scanner_scans as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
