"""对比所有 tab 不同 sell_n 的回测结果"""
import json
import subprocess

results = []
for tab in ['limit-up', 'zhaban', 'trend', 'reversal', 'dtqiaoban', 'sector']:
    for sell_n in [2, 3, 4, 5]:
        url = f'http://localhost:8080/api/bt/{tab}/full?days=30&top_n=1&min_score=70&sell_n={sell_n}'
        out = subprocess.check_output(['curl', '-s', url], timeout=120).decode()
        d = json.loads(out)
        bt = d.get('backtest', {})
        s = bt.get('summary', {})
        results.append((tab, sell_n, s.get('trade_count', 0),
                        s.get('win_rate', 0), s.get('total_pnl', 0),
                        s.get('ev', 0), s.get('plr', 0)))

# 打印每个 tab 的 sell_n 对比
print(f'{"tab":<10} {"sell_n":<7} {"#":<3} {"win%":<6} {"pnl":<8} {"ev":<6} {"plr":<5}')
for r in results:
    print(f'{r[0]:<10} {r[1]:<7} {r[2]:<3} {r[3]:<6} {r[4]:<8} {r[5]:<6} {r[6]:<5}')
