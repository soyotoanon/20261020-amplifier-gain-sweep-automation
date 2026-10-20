@echo off
setlocal
cd /d "%~dp0"
python hat_gain_sweep.py
if errorlevel 1 (
  echo.
  echo Launch failed. Run install_dependencies.cmd, then try again.
  pause
)
endlocal
