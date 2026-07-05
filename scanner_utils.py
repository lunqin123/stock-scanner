"""兼容shim - 实际模块已移至 core/scanner_utils.py"""
from core.scanner_utils import *  # noqa
import core.scanner_utils as _orig
# 显式导出所有下划线符号
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
