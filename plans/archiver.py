#!/usr/bin/env python3
"""
Plan 结果日归档 — 每天盘后保存各 Plan 的 score() 输出, 供回测使用。

存储结构:
  daily_data/
    YYYY-MM-DD/
      plan_a.json
      plan_b.json

每个 JSON:
  { plan, date, stocks, factors, sentiment_score, sentiment_level }

用法:
  save_plan_result(date_str, plan_name, result_dict)   # 写入
  load_plan_result(date_str, plan_name) → dict or None  # 读取
  list_plan_results(date_str) → [plan_name, ...]        # 列出某天有哪些 Plan
"""

import os
import json
import sys


def _archive_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'daily_data')


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def save_plan_result(date_str: str, plan_name: str, result_dict: dict):
    """
    保存 Plan 评分结果。result_dict = plan.score() 的返回值。
    date_str: YYYY-MM-DD 或 YYYYMMDD
    """
    # 统一为 YYYY-MM-DD
    date_str = _norm_date(date_str)
    date_dir = os.path.join(_archive_dir(), date_str)
    _ensure_dir(date_dir)

    save_data = {
        'plan': plan_name.lower(),
        'date': date_str,
        'stocks': result_dict.get('stocks', []),
        'sentiment_score': result_dict.get('sentiment_score'),
        'sentiment_level': result_dict.get('sentiment_level'),
        'sentiment_ok': result_dict.get('sentiment_ok'),
        'factors': {},
    }

    # 序列化因子 Series → {index_label: value} dict
    factor_keys = [
        'seal_scores', 'money_scores', 'sector_mom', 'sector_res',
        'tech_scores', 'history_scores', 'buyability_scores',
        'stock_sent_scores', 'principal_scores',
        'north_flow', 'margin_ratio', 'inst_rating', 'limit_reason',
    ]
    for key in factor_keys:
        val = result_dict.get(key)
        if val is None:
            continue
        if hasattr(val, 'to_dict'):
            # pandas Series → {index: value}
            try:
                save_data['factors'][key] = {str(k): float(v) for k, v in val.to_dict().items()}
            except Exception:
                save_data['factors'][key] = {}
        elif isinstance(val, dict):
            save_data['factors'][key] = val

    path = os.path.join(date_dir, f'{plan_name.lower()}.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        print(f"  [归档] Plan {plan_name} → {date_str}/{plan_name.lower()}.json", file=sys.stderr)
    except Exception as e:
        print(f"  [归档] 写入失败 ({plan_name}): {e}", file=sys.stderr)


def load_plan_result(date_str: str, plan_name: str) -> dict | None:
    """读取某天某 Plan 的评分结果。返回完整 dict 或 None。"""
    date_str = _norm_date(date_str)
    path = os.path.join(_archive_dir(), date_str, f'{plan_name.lower()}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"  [归档] 读取失败 ({date_str}/{plan_name}): {e}", file=sys.stderr)
        return None


def list_plan_results(date_str: str) -> list:
    """列出某天有哪些 Plan 的结果"""
    date_str = _norm_date(date_str)
    date_dir = os.path.join(_archive_dir(), date_str)
    if not os.path.isdir(date_dir):
        return []
    return sorted([f.replace('.json', '') for f in os.listdir(date_dir)
                   if f.endswith('.json')])


def _norm_date(date_str: str) -> str:
    """统一日期格式 → YYYY-MM-DD"""
    s = date_str.replace('-', '')
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return date_str
