@echo off
REM Remove the Employee Monitor agent from this computer. Run as Administrator.
setlocal
net session >nul 2>&1
if errorlevel 1 ( echo Please run this as Administrator. & exit /b 1 )

schtasks /End    /TN "EmployeeMonitorAgent" >nul 2>&1
schtasks /Delete /TN "EmployeeMonitorAgent" /F >nul 2>&1
taskkill /IM EmployeeMonitorAgent.exe /F >nul 2>&1

REM Drop the whole website-block section the agent added to the hosts file.
set "H=%SystemRoot%\System32\drivers\etc\hosts"
powershell -NoProfile -Command "$h=$env:SystemRoot+'\System32\drivers\etc\hosts'; if(Test-Path $h){ $c=Get-Content $h -Raw; $c=[regex]::Replace($c,'(?s)# >>> EmployeeMonitor blocked sites >>>.*?# <<< EmployeeMonitor blocked sites <<<\r?\n?',''); Set-Content -Path $h -Value $c -NoNewline }" >nul 2>&1
ipconfig /flushdns >nul 2>&1

rmdir /S /Q "%ProgramFiles%\EmployeeMonitor" 2>nul
echo Removed. (Per-user config/log under %%APPDATA%%\EmployeeMonitor can be deleted by hand.)
endlocal
