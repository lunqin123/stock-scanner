"""data_layer/scanner_format.py CLI 文本输出测试。"""
import pandas as pd

import data_layer.scanner_format as fmt


def _df(n=3):
    return pd.DataFrame({
        '代码': ['600000', '000001', '600002'],
        '名称': ['浦发银行', '平安银行', '万科A'],
        '换手率': [25.0, 8.0, 3.0],
        '首次封板时间': ['093000', '103000', '150000'],
        '连板数': [2, 1, 1],
        '所属行业': ['银行', '银行', '地产'],
    })


def _series(df, vals):
    return pd.Series(vals, index=df.index)


def test_format_table_output_basic():
    df = _df()
    seal = _series(df, [20, 12, 5])
    money = _series(df, [15, 10, 4])
    sector = _series(df, [10, 8, 3])
    tech = _series(df, [8, 6, 2])
    hist = _series(df, [5, 4, 2])
    raw_money = {0: 1.5e8, 1: 2e7, 2: 0}
    out = fmt.format_table_output(df, money, sector, seal, tech,
                                  raw_money=raw_money, sentiment_score=5.0,
                                  history_scores=hist)
    assert '600000' in out
    assert '浦发银行' in out
    assert '1.50亿' in out
    assert '2000万' in out
    assert '┼' in out


def test_format_table_output_defaults():
    df = pd.DataFrame({'代码': ['600000'], '名称': ['测试']})
    n = len(df)
    out = fmt.format_table_output(
        df, _series(df, [5] * n), _series(df, [5] * n), _series(df, [5] * n),
        _series(df, [5] * n))
    assert '600000' in out
    assert '净流入' in out


def test_format_output_basic():
    df = _df()
    seal = _series(df, [20, 12, 5])
    money = _series(df, [15, 10, 4])
    sector = _series(df, [10, 8, 3])
    tech = _series(df, [8, 6, 2])
    detail = {'zhaban_rate': 0.1, 'promotion_rate': 0.2,
              'avg_premium': 1.5, 'prev_limit_count': 50}
    out = fmt.format_output(df, money, sector, seal, tech,
                            sentiment_score=5.0,
                            sentiment_level='炸板(高)', sentiment_detail=detail,
                            history_scores=_series(df, [5, 4, 2]))
    assert 'TOP 10 超短线标的' in out
    assert '情绪:炸板(高)' in out
    assert '上交易日涨停50只' in out
    assert '换手过高' in out          # 25% 换手
    assert '封板偏弱' in out          # 封板 5 分
    assert '板块效应强' in out        # 板块热度 10
    assert '连板2' in out
    assert '评分拆解' in out
    assert '仓位建议' in out


def test_format_output_missing_columns():
    df = pd.DataFrame({
        '代码': ['600000'],
        '名称': ['测试'],
        '首次封板时间': ['093000'],
    })
    n = len(df)
    out = fmt.format_output(df, _series(df, [5] * n), _series(df, [5] * n),
                            _series(df, [5] * n), _series(df, [5] * n))
    assert '?' in out
    assert '标准首板标的' in out
