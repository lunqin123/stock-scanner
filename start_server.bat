@echo off
cd /d "C:\Users\16689\stock-scanner"
echo [%date% %time%] 启动股票扫描服务... >> server.log
start /B pythonw app.py >> server.log 2>&1
echo 服务已在后台启动，请访问 http://127.0.0.1:8080/
echo 日志: stock-scanner\server.log
