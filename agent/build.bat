@echo off
REM Build the agent into one Windows .exe (no console window):
REM     dist\EmployeeMonitorAgent.exe
REM
REM     build.bat                          uses the "python" on PATH
REM     build.bat C:\path\to\python.exe    uses that Python
REM
REM ONE Python does everything here: installing the libraries, PyInstaller, the
REM build. When "pip" and "pyinstaller" on PATH belonged to two different Pythons,
REM a library installed into one was missing from the .exe built by the other.
REM That is how remote control once dropped out of the built agent with no error.
setlocal
cd /d "%~dp0"
set "PY=%~1"
if "%PY%"=="" set "PY=python"

"%PY%" --version
if errorlevel 1 ( echo Python was not found. Give its path:  build.bat C:\path\to\python.exe & exit /b 1 )

"%PY%" -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.9,<7"
if errorlevel 1 ( echo. & echo Could not install the libraries into this Python. & exit /b 1 )

"%PY%" -m PyInstaller --noconfirm --clean EmployeeMonitorAgent.spec
if errorlevel 1 ( echo. & echo The build failed. & exit /b 1 )

REM The fresh .exe checks itself: every part the agent needs must be inside it.
REM It shows nothing and sends nothing.
set "REPORT=%TEMP%\em-selftest-%RANDOM%.json"
start "" /wait "dist\EmployeeMonitorAgent.exe" --selftest "%REPORT%"
set "RC=%errorlevel%"
if exist "%REPORT%" type "%REPORT%"
del "%REPORT%" >nul 2>&1
if not "%RC%"=="0" ( echo. & echo THE BUILT AGENT FAILED ITS SELF-TEST. Do not install this .exe. & exit /b 1 )
echo.
echo Built and self-tested: %~dp0dist\EmployeeMonitorAgent.exe
endlocal
