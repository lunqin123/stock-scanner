@echo off
cd /d "C:\Users\16689\stock-scanner"

rem ─── 日志轮转：只保留最近 2000 行 ───
if exist server.log (
    powershell -Command "$lines=Get-Content server.log; if($lines.Count -gt 2000){$lines[-2000..-1] | Set-Content server.log}"
)

echo [%date% %time%] ===== 股票扫描服务启动 ===== >> server.log
echo [%date% %time%] 进程启动中... >> server.log
start /B pythonw app.py >> server.log 2>&1
echo [%date% %time%] 服务已在后台启动，请访问 http://127.0.0.1:8080/ >> server.log
echo ======================================== >> server.log
