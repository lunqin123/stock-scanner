"""项目全局常量集中管理 - 避免散落各文件
所有模块应从这里 import, 不再硬编码。
"""
import os
from datetime import timezone, timedelta

# ═══════════════════════════════════════════
#  交易时间常量 (距零点分钟数)
# ═══════════════════════════════════════════
MARKET_OPEN_MINUTES = 570       # 09:30
MORNING_CLOSE_MINUTES = 690     # 11:30
AFTERNOON_OPEN_MINUTES = 780    # 13:00
AFTERNOON_CLOSE_MINUTES = 900   # 15:00
SEAL_TIME_RANGE = 300           # 封板时间归一化范围: 09:30→14:30=300分钟
MAX_LATE_SEAL = "143000"        # 最晚封板时间 (HHMMSS)

# ═══════════════════════════════════════════
#  选股阈值
# ═══════════════════════════════════════════
MAX_MARKET_CAP = 200            # 流通市值上限 (亿)
MAX_PRICE = 60                  # 最高股价 (2万本金单票6000, 最少买100股)
TOP_N = 10                      # 默认输出数量 (CLI + 回测 + cards)

# ═══════════════════════════════════════════
#  缓存配置
# ═══════════════════════════════════════════
CACHE_DIR = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
                        "stock_scanner_cache")
CACHE_TTL = 7200                # 默认 2 小时 (秒)
CST = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════
#  回测 / 模拟交易参数
# ═══════════════════════════════════════════
COMMISSION_PCT = 0.025          # 佣金万 2.5 (单边)
COMMISSION_ROUNDTRIP_PCT = 0.05  # 万 5 (双向)
SLIPPAGE_PCT = 0.1              # 滑点千 1 (单边, 模拟流动性成本)

# ═══════════════════════════════════════════
#  兼容旧代码的别名 (deprecated, 仅供向后兼容)
# ═══════════════════════════════════════════
_CST = CST  # scanner.py 老代码用 _CST
_CACHE_DIR = CACHE_DIR
_CACHE_TTL = CACHE_TTL
