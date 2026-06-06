"""统一 logging 配置 (P2-1 阶段1: 仅搭建框架, 不替换现有 print)
未来新代码应用 logger.debug/info/warning/error 替代 print(..., file=sys.stderr)。
现有 print 暂不动 (230 个 print 替换工作量大, 后续 P2-1.2 阶段逐步替换)。
"""
import logging
import sys
import os

# 默认配置: INFO 级别, stderr 输出, 含时间戳和模块名
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LEVEL = os.environ.get("SCANNER_LOG_LEVEL", "INFO").upper()


def setup_logging(level: str = None, log_file: str = None) -> None:
    """统一 logging 配置

    参数:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR), 默认从环境变量读
        log_file: 可选, 写日志到文件 (生产用)
    """
    log_level = (level or DEFAULT_LEVEL)
    numeric_level = getattr(logging, log_level, logging.INFO)

    # 清除已有 handler (避免重复)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    # stderr handler
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    # 可选: 文件 handler
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as e:
            sys.stderr.write(f"[logging_setup] 文件 handler 创建失败: {e}\n")

    root.setLevel(numeric_level)

    # 抑制第三方库冗余日志
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("akshare").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """统一获取 logger (各模块 import 这个用)
    用法:
        from logging_setup import get_logger
        logger = get_logger(__name__)
        logger.info("xxx")
    """
    return logging.getLogger(name)


# 预配置: import 时自动设置
if not logging.getLogger().handlers:
    setup_logging()
