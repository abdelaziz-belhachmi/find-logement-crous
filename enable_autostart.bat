@echo off
setlocal
set SERVICE=FindLogement

:: Ask for administrator rights if needed (a UAC prompt will appear)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Enabling automatic start for "%SERVICE%"...
sc config %SERVICE% start= auto
if %errorLevel% neq 0 (
    echo.
    echo ERROR: service "%SERVICE%" not found. Was it installed with NSSM?
    pause
    exit /b 1
)

echo Starting the service now...
sc start %SERVICE% >nul 2>&1

echo.
sc query %SERVICE% | findstr /i "STATE"
echo.
echo Done. "%SERVICE%" will start automatically every time the computer starts.
pause
