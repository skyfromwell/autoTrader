@echo off
:: Start XtMiniQMT terminal
start "" "P:\XUNTOU\金融街证券QMT模拟 - 交易终端\bin.x64\XtMiniQMT.exe"

:: Wait 30 seconds for XtMiniQMT to fully initialize and connect
timeout /t 30 /nobreak > nul

:: Start the FastAPI bridge
cd /d C:\autoTrader\china_server
py -3.9 -m uvicorn api:app --host 0.0.0.0 --port 8888 >> C:\autoTrader\china_server\server.log 2>&1
