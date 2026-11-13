@echo off
REM Remove the Employee Monitor agent from this computer. Run as Administrator.
setlocal
net session >nul 2>&1
if errorlevel 1 ( echo Please run this as Administrator. & exit /b 1 )

schtasks /End    /TN "EmployeeMonitorAgent" >nul 2>&1
schtasks /Delete /TN "EmployeeMonitorAgent" /F >nul 2>&1
taskkill /IM EmployeeMonitorAgent.exe /T /F >nul 2>&1
REM give Windows a moment to release the files
ping -n 3 127.0.0.1 >nul

REM Drop the whole website-block section the agent added to the hosts file.
powershell -NoProfile -Command "$h=$env:SystemRoot+'\System32\drivers\etc\hosts'; if(Test-Path $h){ $c=Get-Content $h -Raw; $c=[regex]::Replace($c,'(?s)# >>> EmployeeMonitor blocked sites >>>.*?# <<< EmployeeMonitor blocked sites <<<\r?\n?',''); Set-Content -Path $h -Value $c -NoNewline }" >nul 2>&1
ipconfig /flushdns >nul 2>&1

rmdir /S /Q "%ProgramFiles%\EmployeeMonitor" 2>nul
REM This computer's enrolment, kept once for all its users.
rmdir /S /Q "%ProgramData%\EmployeeMonitor" 2>nul

REM What the agent unpacked into each user's TEMP folder and could not remove when
REM Windows ended it at log-off or shut-down. In each such folder the Python DLL is
REM deleted first: Windows refuses that while a program is still running out of the
REM folder, and then nothing else in it is touched.
powershell -NoProfile -Command "Get-ChildItem ($env:SystemDrive+'\Users\*\AppData\Local\Temp\_MEI*') -Directory -ErrorAction SilentlyContinue | Where-Object { (Test-Path (Join-Path $_.FullName 'em_agent.marker')) -or ((Test-Path (Join-Path $_.FullName '_tcl_data')) -and (Test-Path (Join-Path $_.FullName 'psutil')) -and (Test-Path (Join-Path $_.FullName 'PIL')) -and (Test-Path (Join-Path $_.FullName 'certifi'))) } | ForEach-Object { $d=$_.FullName; try { Get-ChildItem (Join-Path $d 'python3*.dll') -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^python3\d+\.dll$' } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop }; Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1

echo Removed. (Each user's own log under %%APPDATA%%\EmployeeMonitor can be deleted by hand.)
endlocal
