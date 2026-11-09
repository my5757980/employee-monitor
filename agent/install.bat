@echo off
REM ===================================================================
REM  Employee Monitor - install the agent on a COMPANY computer.
REM  Run as Administrator.  Usage:
REM      install.bat  https://monitor.yourcompany.com  ENROLLKEY
REM
REM  It copies the agent into Program Files, starts it monitoring at
REM  every logon (on mains OR battery), and runs it now. The employee
REM  sees a monitoring notice on first run - this tool does not hide.
REM ===================================================================
setlocal
set "SERVER=%~1"
set "KEY=%~2"
if "%SERVER%"=="" ( echo Usage: install.bat ^<server-url^> ^<enroll-key^> & exit /b 1 )
if "%KEY%"=="" ( echo Usage: install.bat ^<server-url^> ^<enroll-key^> & exit /b 1 )

net session >nul 2>&1
if errorlevel 1 ( echo Please run this as Administrator. & exit /b 1 )

set "DEST=%ProgramFiles%\EmployeeMonitor"
if not exist "%DEST%" mkdir "%DEST%"

REM Stop a running agent first, or the copy fails with "being used by another
REM process" when re-installing / upgrading on a computer that already has it.
taskkill /F /IM EmployeeMonitorAgent.exe >nul 2>&1
REM give Windows a moment to release the file lock
ping -n 3 127.0.0.1 >nul

copy /Y "%~dp0dist\EmployeeMonitorAgent.exe" "%DEST%\EmployeeMonitorAgent.exe" >nul
if errorlevel 1 ( echo Could not copy the agent. Build it first ^(build.bat^). & exit /b 1 )

REM Run at every logon, in the user's session (needed to see the screen), highest rights.
schtasks /Create /TN "EmployeeMonitorAgent" /RL HIGHEST /SC ONLOGON /F ^
  /TR "\"%DEST%\EmployeeMonitorAgent.exe\" --server %SERVER% --key %KEY%" >nul
if errorlevel 1 ( echo Could not create the startup task. & exit /b 1 )

REM Work on ANY power state. By default a scheduled task refuses to start on
REM battery, stops when a laptop unplugs, and is killed after 72 hours. Clear all
REM of that so the agent behaves the same on mains or battery, with no time limit.
REM (On a desktop with no battery these settings simply have no effect.)
powershell -NoProfile -Command "$t=Get-ScheduledTask -TaskName 'EmployeeMonitorAgent'; $t.Settings.DisallowStartIfOnBatteries=$false; $t.Settings.StopIfGoingOnBatteries=$false; $t.Settings.StartWhenAvailable=$true; $t.Settings.ExecutionTimeLimit='PT0S'; Set-ScheduledTask -TaskName 'EmployeeMonitorAgent' -Settings $t.Settings | Out-Null" >nul 2>&1

REM Start it now, detached, in this interactive desktop. A logon task fired with
REM schtasks /Run does not reliably attach to the session, so launch it directly.
start "" "%DEST%\EmployeeMonitorAgent.exe" --server %SERVER% --key %KEY%
echo Installed. The agent is running now and will start at every logon - on mains or battery.
echo A monitoring notice is shown to the user on first run.
endlocal
