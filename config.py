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

# 创业板/科创板开关 — 默认 False, 保持历史行为
# True: 扫描包含 300/301 (创业板, 20%涨停) 和 688 (科创板, 20%涨停 + 资金门槛 50万)
# 风险: 次日 20% 涨停开盘容易追不上, 流动性差; 优势: 弹性大, 肉厚
INCLUDE_CHINEXT = False

# ═══════════════════════════════════════════
#  缓存配置
# ═══════════════════════════════════════════
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_PROJECT_DIR, "data", "cache")
CACHE_TTL = 7200                # 默认 2 小时 (秒)
CST = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════
#  回测 / 模拟交易参数 (A 股真实费率)
# ═══════════════════════════════════════════
COMMISSION_PCT = 0.01           # 佣金万 1 (单边, 券商最低档)
STAMP_DUTY_PCT = 0.05           # 印花税千 0.5 (卖出单向收取)
TRANSFER_FEE_PCT = 0.001        # 过户费十万分之一 (双向)
# 完整往返成本 = 买入0.011% + 卖出0.061% = 0.072%
COMMISSION_ROUNDTRIP_PCT = 0.072  # 佣金+印花税+过户费 往返
SLIPPAGE_PCT = 0.1              # 滑点千 1 (模拟流动性成本, 单边估算)

# ═══════════════════════════════════════════
#  兼容旧代码的别名 (deprecated, 仅供向后兼容)
# ═══════════════════════════════════════════
_CST = CST  # scanner.py 老代码用 _CST
_CACHE_DIR = CACHE_DIR
_CACHE_TTL = CACHE_TTL
