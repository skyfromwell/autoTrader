@echo off
:: Start the china_executor.py order watcher.
:: QMT must already be running before launching this.

set QMT_ACCOUNT=66801935
set QMT_PATH=P:\xuntou2\金融街证券QMT实盘 - 交易终端\userdata
set PENDING_DIR=C:\autoTrader\output\china_pending
set POLL_SECS=15
set LOG_FILE=C:\autoTrader\china_server\executor.log

cd /d C:\autoTrader\china_server
python china_executor.py >> %LOG_FILE% 2>&1
