@echo off
:: Start QMT real account terminal
start "" "P:\XUNTOU\金融街证券QMT实盘 - 交易终端\bin.x64\XtItClient.exe"

:: Wait for QMT to initialize
timeout /t 45 /nobreak > nul

:: Config — PENDING_DIR must point to the Syncthing-synced china_pending folder
set QMT_ACCOUNT=66801935
set PENDING_DIR=C:\autoTrader\output\china_pending
set POLL_SECS=15
set LOG_FILE=C:\autoTrader\china_server\executor.log

cd /d C:\autoTrader\china_server
py -3 china_executor.py >> %LOG_FILE% 2>&1
