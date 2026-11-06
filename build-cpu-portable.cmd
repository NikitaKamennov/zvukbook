@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\build-windows.ps1" -Edition cpu -Portable %*
if errorlevel 1 (
  echo CPU portable build failed. Read the message above.
  pause
  exit /b 1
)
echo CPU portable ZIP: .local-data\windows-x64\portable-cpu\Zvukbook-CPU-Portable.zip
pause
