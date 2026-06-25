@echo off
:: Wait 30 seconds for miniQMT to fully initialize after logon
timeout /t 30 /nobreak > nul

cd /d C:\autoTrader\china_server
uvicorn api:app --host 0.0.0.0 --port 8888 >> C:\autoTrader\china_server\server.log 2>&1
