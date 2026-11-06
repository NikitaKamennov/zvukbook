@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\build-windows.ps1" -Edition full -Portable %*
if errorlevel 1 (
  echo Full portable build failed. Read the message above.
  pause
  exit /b 1
)
echo Full portable ZIP: .local-data\windows-x64\portable\Zvukbook-Portable.zip
pause
