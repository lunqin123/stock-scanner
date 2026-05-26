@echo off
rem ─── 健康检查：如果 Web 服务挂了则自动重启 ───
cd /d "C:\Users\16689\stock-scanner"

rem 检查 http://127.0.0.1:8080/ 是否响应
powershell -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:8080/' -TimeoutSec 5 -UseBasicParsing; if($r.StatusCode -eq 200){exit 0}}catch{}; exit 1" >nul 2>&1

if errorlevel 1 (
    rem 服务未响应，杀掉残留进程后重启
    echo [%date% %time%] ! 健康检查：服务无响应，正在重启... >> server.log
    taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq app.py" 2>nul
    timeout /T 3 /NOBREAK >nul
    start /B pythonw app.py >> server.log 2>&1
    echo [%date% %time%] ! 健康检查：已重启 >> server.log
) else (
    rem 服务正常，静默退出
    exit /B 0
)
