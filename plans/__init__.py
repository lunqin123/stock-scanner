#!/usr/bin/env python3
"""
评分方案注册表

A 策略是唯一可用方案 (2026-07-05: 用户反馈其他策略始终不如 A, 移除 B 等).

用法：
    from plans import get_plan
    plan = get_plan()  # A
    result = plan.score(inputs)
"""

import importlib
import sys
import os as _os

_PLANS = {
    "A": "plans.plan_a",
}

_DEFAULT = "A"


def list_plans() -> list[dict]:
    """列出所有可用方案"""
    results = []
    for name, mod_path in _PLANS.items():
        try:
            mod = importlib.import_module(mod_path)
            results.append({
                "name": getattr(mod, "PLAN_NAME", name),
                "description": getattr(mod, "PLAN_DESC", ""),
                "is_default": name == _DEFAULT,
            })
        except Exception as e:
            results.append({"name": name, "description": f"加载失败: {e}", "is_default": False})
    return results


def get_plan(name: str = None):
    """
    获取评分方案模块。

    Args:
        name: 方案名 (如 "A", "B")，为 None 时使用默认方案

    Returns:
        方案模块（有 score() 函数）

    Raises:
        ValueError: 方案不存在
    """
    plan_name = name or _DEFAULT
    mod_path = _PLANS.get(plan_name.upper())
    if mod_path is None:
        available = ", ".join(_PLANS.keys())
        raise ValueError(f"未知方案 '{plan_name}'，可用: {available}")

    # 清除缓存以支持热加载（开发时改 plan 代码无需重启）
    if mod_path in sys.modules:
        del sys.modules[mod_path]
        # 也清除子模块缓存
        to_del = [k for k in sys.modules if k.startswith(mod_path)]
        for k in to_del:
            del sys.modules[k]

    return importlib.import_module(mod_path)


def reload_plans():
    """热重载所有方案（改代码后无需重启服务器）"""
    for mod_path in set(_PLANS.values()):
        if mod_path in sys.modules:
            del sys.modules[mod_path]
    # 清除带子模块的缓存
    keys = list(sys.modules.keys())
    for k in keys:
        for mp in _PLANS.values():
            if k.startswith(mp):
                del sys.modules[k]
