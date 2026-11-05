@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\build-windows.ps1" -Edition cpu -Release %*
if errorlevel 1 (
  echo CPU release build failed. Read the message above.
  pause
  exit /b 1
)
echo CPU installer and portable ZIP: .local-data\windows-x64\release-cpu\
pause
