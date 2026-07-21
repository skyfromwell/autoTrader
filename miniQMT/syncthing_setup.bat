@echo off
:: Syncthing setup for China QMT machine.
:: Run once as Administrator to install Syncthing and print the device ID.
:: After running, send the Device ID back so Mac can pair with it.

set ST_DIR=C:\autoTrader\syncthing
set ST_EXE=%ST_DIR%\syncthing.exe
set PENDING_DIR=C:\autoTrader\output\china_pending

:: Create dirs
if not exist "%ST_DIR%"      mkdir "%ST_DIR%"
if not exist "%PENDING_DIR%" mkdir "%PENDING_DIR%"

:: Download Syncthing if not present
if not exist "%ST_EXE%" (
    echo Downloading Syncthing...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/syncthing/syncthing/releases/download/v1.29.4/syncthing-windows-amd64-v1.29.4.zip' -OutFile '%ST_DIR%\st.zip'"
    powershell -Command "Expand-Archive -Path '%ST_DIR%\st.zip' -DestinationPath '%ST_DIR%\tmp' -Force"
    powershell -Command "Copy-Item '%ST_DIR%\tmp\syncthing-windows-amd64-v1.29.4\syncthing.exe' '%ST_EXE%'"
    rmdir /s /q "%ST_DIR%\tmp"
    del "%ST_DIR%\st.zip"
    echo Syncthing downloaded.
)

:: Generate config (first run creates key + device ID)
echo Generating Syncthing config...
"%ST_EXE%" generate --home="%ST_DIR%\config" 2>nul

:: Print device ID
echo.
echo ============================================================
echo  DEVICE ID (send this to Mac to complete pairing):
"%ST_EXE%" show-device-id --home="%ST_DIR%\config"
echo ============================================================
echo.

:: Install as Windows service (auto-start on boot)
echo Installing Syncthing as Windows service...
"%ST_EXE%" --home="%ST_DIR%\config" install-service 2>nul || echo (service install skipped - may need Admin)

:: Start Syncthing now (GUI on http://localhost:8384)
echo Starting Syncthing...
start "" "%ST_EXE%" --home="%ST_DIR%\config" --no-browser

echo.
echo Done. Syncthing is running.
echo GUI: http://localhost:8384
echo Pending orders will appear in: %PENDING_DIR%
echo.
pause
