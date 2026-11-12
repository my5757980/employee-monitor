@echo off
REM Build the agent into one Windows .exe (no console window).
REM Run on a Windows machine with Python + the agent's libraries installed:
REM     pip install -r requirements.txt pyinstaller
REM Output: dist\EmployeeMonitorAgent.exe

cd /d "%~dp0"
REM Make sure every library is present in THIS Python before building, or a feature
REM silently drops out of the .exe. pynput (remote control) was the one that went
REM missing before: without it, Take Control did nothing on the built agent.
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name EmployeeMonitorAgent ^
  --hidden-import mss --hidden-import PIL --hidden-import PIL.ImageTk --hidden-import psutil ^
  --hidden-import tkinter --hidden-import winsound --hidden-import truststore ^
  --collect-submodules pynput ^
  agent.py
echo.
echo Built: %~dp0dist\EmployeeMonitorAgent.exe
