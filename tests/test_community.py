"""signals/community.py 舆情聚合纯逻辑测试 (网络全部打桩)。"""
import pandas as pd
import pytest

import signals.community as cm


def test_build_sentiment_map():
    df = pd.DataFrame({'代码': ['1', '600001'], '名称': ['浦发银行', '测试']})
    guba = {'000001': {'rank': 3}}
    comments = {'000001': {'综合得分': 85, '机构参与度': 0.8}}
    news = {'600001': {'news': [{'title': '利好'}]}}
    smap = cm.build_sentiment_map(df, guba, comments, news)
    assert smap['000001']['guba_rank'] == 3
    assert smap['000001']['comment_score'] == 85
    assert smap['600001']['news'][0]['title'] == '利好'
    assert 'comment_score' not in smap['600001']


def test_format_output_sections():
    smap = {
        '000001': {'name': '浦发银行', 'code': '000001', 'guba_rank': 3,
                   'comment_score': 88, 'news': [{'title': '个股利好新闻'}]},
        '600001': {'name': '测试', 'code': '600001', 'comment_score': 72},
    }
    gl = [{'source': '财联社', 'title': '全球财经要闻标题'}]
    out = cm.format_output(smap, gl)
    assert '财经要闻' in out
    assert '全球财经要闻标题' in out
    assert '个股新闻' in out
    assert '个股利好新闻' in out
    assert '股吧热度' in out
    assert '#3 浦发银行(000001)' in out
    assert '千股千评高分' in out


def test_compute_community_scores():
    df = pd.DataFrame({'代码': ['1', '2', '3', '4', '5', '6']})
    comments = {
        '000001': {'综合得分': 85, '机构参与度': 0.5},   # 3 + 1 = 4
        '000002': {'综合得分': 65, '机构参与度': 0.0},   # 1 + 0 = 1
        '000003': {'综合得分': 'bad', '机构参与度': 0.8},  # 0 + 1.6 = 1.6
        '000004': {'综合得分': 85, '机构参与度': 0.0},   # 3 + 0 = 3
    }
    guba = {
        '000001': {'rank': 5},     # +2
        '000002': {'rank': 20},    # +1.5
        '000003': {'rank': 40},    # +1
        '000004': {'rank': 80},    # +0.5
        '000005': {'rank': 999},   # +0
    }
    out = cm.compute_community_scores(comments, guba, df)
    assert out.loc[0] == 6.0        # 4 + 2
    assert out.loc[1] == 2.5        # 1 + 1.5
    assert out.loc[2] == 2.6        # 1.6 + 1
    assert out.loc[3] == 3.5        # 3 + 0.5
    assert out.loc[4] == 1.0        # 无评论 + 无排名加分
    assert out.loc[5] == 1.0        # 完全无数据


def test_compute_community_scores_clamp():
    df = pd.DataFrame({'代码': ['1']})
    comments = {'000001': {'综合得分': 99, '机构参与度': 1.0}}   # 3 + 2 = 5
    guba = {'000001': {'rank': 1}}                              # +2 → 7 (上限)
    out = cm.compute_community_scores(comments, guba, df)
    assert out.loc[0] == 7.0


def test_score_community(monkeypatch):
    monkeypatch.setattr(cm, 'fetch_guba_rank', lambda: {'000001': {'rank': 1}})
    monkeypatch.setattr(cm, 'fetch_comment_scores',
                        lambda: {'000001': {'综合得分': 90, '机构参与度': 0.2}})
    df = pd.DataFrame({'代码': ['000001']})
    out = cm.score_community(df)
    assert out.loc[0] == 5.4   # 3 + 0.4 + 2


def test_run_returns_output_and_map(monkeypatch):
    monkeypatch.setattr(cm, 'fetch_guba_rank', lambda: {'000001': {'rank': 2}})
    monkeypatch.setattr(cm, 'fetch_comment_scores', lambda: {})
    monkeypatch.setattr(cm, 'fetch_news_for_stocks', lambda df, top_n=10: {})
    monkeypatch.setattr(cm, 'fetch_global_news', lambda: [])
    df = pd.DataFrame({'代码': ['000001'], '名称': ['平安银行']})
    output, smap = cm.run(df)
    assert isinstance(output, str)
    assert smap['000001']['code'] == '000001'


def test_run_failure_returns_empty(monkeypatch):
    def boom(*a, **k): raise RuntimeError('down')
    monkeypatch.setattr(cm, 'fetch_guba_rank', boom)
    monkeypatch.setattr(cm, 'fetch_comment_scores', boom)
    monkeypatch.setattr(cm, 'fetch_news_for_stocks', boom)
    monkeypatch.setattr(cm, 'fetch_global_news', boom)
    output, smap = cm.run(pd.DataFrame({'代码': ['000001']}))
    assert '舆情摘要' in output   # 全部数据缺失时仍输出空模板
    assert set(smap) == {'000001'}   # 每只股票仍会出现在 map 中, 但无舆情字段
    assert smap['000001'] == {'code': '000001', 'name': ''}
