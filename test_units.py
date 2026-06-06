"""单元测试: 评分函数 (P2-3)
- 保护重构: 防止改 score_* 函数时跑挂
- 用 unittest 风格, pytest / python -m unittest 都能跑
"""
import sys
import unittest
import pandas as pd
import numpy as np

sys.path.insert(0, r"C:\Users\16689\Desktop\stock-scanner")

import scanner


class TestSealStrength(unittest.TestCase):
    """score_seal_strength 单元测试 - P0 改过的核心函数"""

    def setUp(self):
        """构造测试 DataFrame: 5 只模拟涨停股"""
        self.df = pd.DataFrame({
            '代码': ['000001', '000002', '000003', '000004', '000005'],
            '名称': ['测试1', '测试2', '测试3', '测试4', '测试5'],
            '首次封板时间': ['093500', '103000', '110000', '140000', '145500'],
            '封板资金': [1e8, 5e7, 0, 3e7, 1e7],
            '炸板次数': [0, 1, 2, 3, 5],
        }, index=[0, 1, 2, 3, 4])

    def test_basic_score_range(self):
        """评分应在 0-28 之间"""
        scores = scanner.score_seal_strength(self.df)
        self.assertEqual(len(scores), 5)
        self.assertTrue((scores >= 0).all(), f"发现负分: {scores}")
        self.assertTrue((scores <= 28).all(), f"发现超 28: {scores}")

    def test_early_seal_higher(self):
        """早盘封板 (09:35) 应高于尾盘封板 (14:55)"""
        scores = scanner.score_seal_strength(self.df)
        self.assertGreater(scores.iloc[0], scores.iloc[4],
                          f"早盘分 {scores.iloc[0]} 应 > 尾盘分 {scores.iloc[4]}")

    def test_zero_seal_fund(self):
        """封单为 0 时应给中位 4 分 (不报错)"""
        scores = scanner.score_seal_strength(self.df)
        self.assertGreater(scores.iloc[2], 0, "封单为 0 但得分 ≤ 0")

    def test_zhaban_penalty(self):
        """炸板次数 5 次应得最低分"""
        scores = scanner.score_seal_strength(self.df)
        self.assertLess(scores.iloc[4], scores.iloc[0],
                       f"炸板 5 次 {scores.iloc[4]} 应 < 0 次 {scores.iloc[0]}")

    def test_gold_bonus_threshold(self):
        """高分(>=20) 应有黄金奖励"""
        scores = scanner.score_seal_strength(self.df)
        # 第 0 行 (09:35 + 1e8) 应得高分 + 黄金奖励
        high_score = scores.iloc[0]
        self.assertGreater(high_score, 20, f"应触发黄金奖励: {high_score}")


class TestVectorizedSealTime(unittest.TestCase):
    """_vectorized_seal_time_score 单元测试"""

    def test_known_timestamps(self):
        """测试已知时间点的分数"""
        s = pd.Series(['093500', '103000', '110000', '140000', '145500'])
        scores = scanner._vectorized_seal_time_score(s)
        # 09:35 = 575 分钟, <= 600 = 12 分
        self.assertEqual(scores.iloc[0], 12.0)
        # 10:30 = 630 分钟, <= 630 = 9 分
        self.assertEqual(scores.iloc[1], 9.0)
        # 11:00 = 660 分钟, <= 690 = 6 分
        self.assertEqual(scores.iloc[2], 6.0)
        # 14:00 = 840 分钟, <= 840 = 2 分
        self.assertEqual(scores.iloc[3], 2.0)
        # 14:55 = 895 分钟, > 840 = 0 分
        self.assertEqual(scores.iloc[4], 0.0)

    def test_invalid_timestamp(self):
        """无效时间戳应得 6 分 (中位)"""
        s = pd.Series(['', 'abc', None])  # 这 3 个都解析失败
        scores = scanner._vectorized_seal_time_score(s)
        # 无效输入应得 6.0 (默认)
        for s_val in scores:
            self.assertEqual(s_val, 6.0)

    def test_out_of_range_timestamp(self):
        """超范围时间戳 (如 '9999' = 99:99) 应得 0 分"""
        s = pd.Series(['9999'])  # 解析成 6039 分钟, > 840
        scores = scanner._vectorized_seal_time_score(s)
        # '9999' 不在合法范围内 → 0 分
        self.assertEqual(scores.iloc[0], 0.0)


class TestSectorHeatScores(unittest.TestCase):
    """get_sector_heat_scores 单元测试"""

    def test_industry_distribution(self):
        """行业分布影响评分"""
        df = pd.DataFrame({
            '所属行业': ['科技', '科技', '科技', '医药', '医药', '消费'],
        }, index=range(6))
        scores = scanner.get_sector_heat_scores(df)
        # 科技 3 只 > 医药 2 只 > 消费 1 只 → 基础分应递增
        sci_score = scores.iloc[0]  # 科技
        med_score = scores.iloc[3]  # 医药
        con_score = scores.iloc[5]  # 消费
        # 基础分: 4 + count * 2 (min 8)
        # 一致性无数据 → 2 分
        # 总分: 8+2=10, 8+2=10, 6+2=8
        self.assertGreaterEqual(sci_score, med_score)
        self.assertGreaterEqual(med_score, con_score)


class TestCacheKey(unittest.TestCase):
    """make_key 单元测试 - P2-2 (在 cache.py 模块)"""

    def setUp(self):
        from cache import make_key
        self.make_key = make_key

    def test_basic_key(self):
        k = self.make_key("app", "test", version=1)
        self.assertIn("v1", k)
        self.assertIn("app", k)
        self.assertIn("test", k)

    def test_with_params(self):
        k1 = self.make_key("app", "test", version=1, principal=20000)
        k2 = self.make_key("app", "test", version=1, principal=30000)
        self.assertNotEqual(k1, k2, "不同 principal 应生成不同 key")

    def test_param_order_stable(self):
        """参数顺序不影响 key (sorted 保证)"""
        k1 = self.make_key("app", "test", a=1, b=2, c=3)
        k2 = self.make_key("app", "test", c=3, a=1, b=2)
        self.assertEqual(k1, k2, "参数顺序不影响 key")


class TestFundFlowCache(unittest.TestCase):
    """_cache_get / _cache_put 单元测试"""

    def test_cache_roundtrip(self):
        """缓存写入 + 读出值一致"""
        test_name = "test_roundtrip_xxx"
        test_data = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        scanner._cache_put(test_name, test_data)
        loaded = scanner._cache_get(test_name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape, (3, 2))
        # 清理
        import os
        path = os.path.join(scanner._CACHE_DIR, f"{test_name}.pkl")
        if os.path.exists(path):
            os.remove(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
