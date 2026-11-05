@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\build-windows.ps1" %*
if errorlevel 1 (
  echo Build failed. Read the message above.
  pause
  exit /b 1
)
echo Build complete. Run .local-data\windows-x64\dist\Zvukbook\Zvukbook.exe
pause
