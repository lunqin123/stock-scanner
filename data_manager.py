"""兼容shim - 实际模块已移至 data_layer/"""
from data_layer.data_manager import *  # noqa
import data_layer.data_manager as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
