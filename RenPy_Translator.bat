@echo off
cd /d "%~dp0"

rem ---- 1) Python launcher (py.exe) with tkinter ----
py -3 -c "import tkinter" >nul 2>nul
if not errorlevel 1 goto run_launcher

rem ---- 2) python from PATH (the Microsoft Store stub fails this check) ----
python -c "import tkinter" >nul 2>nul
if not errorlevel 1 goto run_path

rem ---- 3) typical install folders ----
call :find_known
if defined PYW goto run_known

rem ---- 4) not found: offer to install Python ----
if exist "%~dp0install_python.ps1" goto have_script
echo Python with tkinter was not found and install_python.ps1 is missing.
echo Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
exit /b 1

:have_script
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_python.ps1"
if errorlevel 2 exit /b 0
if errorlevel 1 goto install_failed
call :find_known
if not defined PYW goto install_failed
start "" "%PYW%" "renpy_translator.pyw"
exit /b 0

:install_failed
echo Python installation failed or was not finished.
echo Install Python manually from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
exit /b 1

:run_launcher
where pyw >nul 2>nul
if not errorlevel 1 (start "" pyw -3 "renpy_translator.pyw") else (start "" py -3 "renpy_translator.pyw")
exit /b 0

:run_path
where pythonw >nul 2>nul
if not errorlevel 1 (start "" pythonw "renpy_translator.pyw") else (start "" python "renpy_translator.pyw")
exit /b 0

:run_known
start "" "%PYW%" "renpy_translator.pyw"
exit /b 0

:find_known
set "PYW="
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" "%%D\python.exe" -c "import tkinter" >nul 2>nul && set "PYW=%%D\pythonw.exe"
for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do if exist "%%D\pythonw.exe" "%%D\python.exe" -c "import tkinter" >nul 2>nul && set "PYW=%%D\pythonw.exe"
exit /b 0
