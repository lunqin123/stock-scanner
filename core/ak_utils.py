"""akshare 统一调用工具 - 重试 + 限速 + 错误处理
所有 akshare 调用应通过此模块, 避免散落各文件的裸调。

用法:
    from ak_utils import safe_ak
    df = safe_ak(ak.stock_zt_pool_em, date='20260605', retries=3)
"""
import sys
import time
import functools

# 默认限速: 每次调用后 sleep
DEFAULT_SLEEP = 0.3  # 秒


def safe_ak(func, *args, retries=3, base_sleep=2.0, fallback_sleep=DEFAULT_SLEEP, **kwargs):
    """统一重试 + 限速的 akshare 调用

    参数:
        func: akshare 函数 (e.g. ak.stock_zt_pool_em)
        *args, **kwargs: 传给 func
        retries: 重试次数 (默认 3)
        base_sleep: 重试基础 sleep 秒数 (指数退避: base_sleep * 2^attempt)
        fallback_sleep: 每次成功调用后的限速 sleep (默认 0.3s)

    返回: func 的返回值, 失败抛最后异常
    """
    last_err = None
    for attempt in range(retries):
        try:
            result = func(*args, **kwargs)
            time.sleep(fallback_sleep)  # 限速
            return result
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                # 指数退避: 2s, 4s, 8s
                sleep_time = base_sleep * (2 ** attempt)
                print(f"  [ak_utils] {func.__name__} 失败 (attempt {attempt+1}/{retries}): {e} - 等待 {sleep_time:.0f}s 重试",
                      file=sys.stderr)
                time.sleep(sleep_time)
            else:
                print(f"  [ak_utils] {func.__name__} 最终失败 ({retries} 次): {e}",
                      file=sys.stderr)
    raise last_err


def with_retry(retries=3, base_sleep=2.0, fallback_sleep=DEFAULT_SLEEP):
    """装饰器版: 自动给函数添加 safe_ak 行为
    用法:
        @with_retry(retries=3)
        def my_func():
            return ak.stock_zt_pool_em(date='20260605')
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return safe_ak(func, *args, retries=retries,
                          base_sleep=base_sleep, fallback_sleep=fallback_sleep, **kwargs)
        return wrapper
    return decorator
