"""兼容shim - 实际模块已移至 backtest/"""
from backtest.scanner_backtest import *  # noqa
import backtest.scanner_backtest as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
