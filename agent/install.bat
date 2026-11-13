@echo off
REM ===================================================================
REM  Employee Monitor - install the agent on a COMPANY computer.
REM  Run as Administrator.  Usage:
REM      install.bat  https://monitor.yourcompany.com  INSTALLKEY
REM
REM  The key adds this computer to the dashboard, once. To update the
REM  agent on a computer that is already there, leave the key out:
REM      install.bat  https://monitor.yourcompany.com
REM
REM  It copies the agent into Program Files, enrols the computer, and
REM  makes the agent start at every log-on, for EVERY user of this
REM  computer, on mains or battery. Each user sees a monitoring notice
REM  on first run - this tool does not hide.
REM ===================================================================
setlocal
set "SERVER=%~1"
set "KEY=%~2"
if "%SERVER%"=="" ( echo Usage: install.bat ^<server-url^> [install-key] & exit /b 1 )

net session >nul 2>&1
if errorlevel 1 ( echo Please run this as Administrator. & exit /b 1 )

set "SRC=%~dp0dist\EmployeeMonitorAgent.exe"
if not exist "%SRC%" set "SRC=%~dp0EmployeeMonitorAgent.exe"
if not exist "%SRC%" ( echo EmployeeMonitorAgent.exe was not found beside this file or in dist\. Build it first with build.bat. & exit /b 1 )

set "DEST=%ProgramFiles%\EmployeeMonitor"
if not exist "%DEST%" mkdir "%DEST%"

REM Stop a running agent first, or the copy fails with "being used by another
REM process" when re-installing / upgrading on a computer that already has it.
schtasks /End /TN "EmployeeMonitorAgent" >nul 2>&1
taskkill /F /T /IM EmployeeMonitorAgent.exe >nul 2>&1
REM give Windows a moment to release the file lock
ping -n 3 127.0.0.1 >nul

copy /Y "%SRC%" "%DEST%\EmployeeMonitorAgent.exe" >nul
if errorlevel 1 ( echo Could not copy the agent into "%DEST%". & exit /b 1 )

REM The agent enrols this computer and makes its own start-up task: for every
REM user of the computer, on any power state, with no time limit. It has no
REM console window, so it writes what happened to a small log, shown here.
set "LOG=%ProgramData%\EmployeeMonitor\install.log"
del "%LOG%" >nul 2>&1
set "KEYARG="
if not "%KEY%"=="" set "KEYARG=--key %KEY%"
start "" /wait "%DEST%\EmployeeMonitorAgent.exe" --install --server "%SERVER%" %KEYARG%
set "RC=%errorlevel%"
if exist "%LOG%" type "%LOG%"
if not exist "%LOG%" echo The agent did not report back. Is it being blocked by an antivirus program?
if not "%RC%"=="0" ( echo. & echo NOT installed - see the lines above. & exit /b 1 )

REM Start it now, in this desktop. From the next log-on the task does it.
start "" "%DEST%\EmployeeMonitorAgent.exe" --server "%SERVER%" %KEYARG%
echo.
echo Installed. The agent is running now and starts at every log-on, for every user, on mains or battery.
echo Each user of this computer sees the monitoring notice once.
endlocal
