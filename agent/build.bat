@echo off
REM Build the agent into one Windows .exe (no console window).
REM Run on a Windows machine with Python + the agent's libraries installed:
REM     pip install -r requirements.txt pyinstaller
REM Output: dist\EmployeeMonitorAgent.exe

cd /d "%~dp0"
pyinstaller --onefile --noconsole --name EmployeeMonitorAgent ^
  --hidden-import mss --hidden-import PIL --hidden-import psutil ^
  agent.py
echo.
echo Built: %~dp0dist\EmployeeMonitorAgent.exe
