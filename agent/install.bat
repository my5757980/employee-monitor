@echo off
REM ===================================================================
REM  Employee Monitor - install the agent on a COMPANY computer.
REM  Run as Administrator.  Usage:
REM      install.bat  https://monitor.yourcompany.com  ENROLLKEY
REM
REM  It copies the agent into Program Files, starts it monitoring at
REM  every logon, and runs it now. The employee sees a monitoring
REM  notice on first run - this tool does not hide.
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
copy /Y "%~dp0dist\EmployeeMonitorAgent.exe" "%DEST%\EmployeeMonitorAgent.exe" >nul
if errorlevel 1 ( echo Could not copy the agent. Build it first ^(build.bat^). & exit /b 1 )

REM Run at every logon, in the user's session (needed to see the screen), highest rights.
schtasks /Create /TN "EmployeeMonitorAgent" /RL HIGHEST /SC ONLOGON /F ^
  /TR "\"%DEST%\EmployeeMonitorAgent.exe\" --server %SERVER% --key %KEY%" >nul
if errorlevel 1 ( echo Could not create the startup task. & exit /b 1 )

schtasks /Run /TN "EmployeeMonitorAgent" >nul
echo Installed. The agent is running and will start at every logon.
echo A monitoring notice is shown to the user on first run.
endlocal
