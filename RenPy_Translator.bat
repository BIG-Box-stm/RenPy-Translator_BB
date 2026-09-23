@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "renpy_translator.pyw"
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "renpy_translator.pyw"
    exit /b
)

echo Python не найден на этом компьютере.
echo Установите Python с сайта https://www.python.org/downloads/
echo При установке обязательно поставьте галочку "Add python.exe to PATH".
pause
