"""项目级 pytest 配置: 默认跳过需要联网的集成测试。"""
import pytest


def pytest_collection_modifyitems(config, items):
    """未显式指定 -m network 时, 跳过所有 network 标记的集成测试。"""
    markexpr = (getattr(config.option, 'markexpr', '') or '').lower()
    if 'network' in markexpr:
        return
    skip = pytest.mark.skip(
        reason='需要真实行情数据(联网), 运行: python -m pytest -m network')
    for item in items:
        if 'network' in item.keywords:
            item.add_marker(skip)
