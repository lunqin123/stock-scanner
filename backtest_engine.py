"""兼容shim - 实际模块已移至 backtest/"""
from backtest.backtest_engine import *  # noqa
import backtest.backtest_engine as _orig
for _k in dir(_orig):
    if _k.startswith('_') and not _k.startswith('__'):
        globals()[_k] = getattr(_orig, _k)
