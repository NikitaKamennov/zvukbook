@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\build-windows.ps1" -Release %*
if errorlevel 1 (
  echo Release build failed. Read the message above.
  pause
  exit /b 1
)
echo Installer and portable ZIP: .local-data\windows-x64\release\
pause
