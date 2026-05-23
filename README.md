# A股超短线选股扫描器

基于 akshare 数据源的多因子超短线选股扫描系统，专攻首板涨停策略。

## 功能

- **涨停池扫描** — 获取当日涨停股池，多维度评分筛选
- **技术指标分析** — 封板质量、板块龙头、量比、K线位置、龙虎榜席位
- **资金流向评分** — 主力资金净流入分析
- **板块热度分析** — 行业/概念板块轮动
- **舆情监测** — 东方财富股吧、雪球热度、新闻情感分析
- **回测系统** — 支持历史数据回测验证

## 快速开始

```bash
pip install -r requirements.txt

# 命令行扫描
python scanner.py

# 启动 Web 界面
python app.py
```

## 项目结构

```
stock-scanner/
├── scanner.py          # 主扫描引擎
├── indicators.py       # 技术指标分析
├── community.py        # 舆情监测
├── data_manager.py     # 数据持久化
├── app.py              # Web 服务（可选）
├── templates/          # HTML 模板
├── data/               # 本地数据存储
└── requirements.txt
```

## 数据源

数据通过 [akshare](https://github.com/akfamily/akshare) 库获取，来源包括东方财富、同花顺等公开财经数据平台。

## 许可

MIT
