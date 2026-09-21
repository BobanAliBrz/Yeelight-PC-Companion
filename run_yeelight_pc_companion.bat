@echo off
cd /d "%~dp0"
if exist "%~dp0dist\YeelightPCCompanion\YeelightPCCompanion.exe" (
    start "" "%~dp0dist\YeelightPCCompanion\YeelightPCCompanion.exe" --tray
) else (
    start "" pythonw.exe "%~dp0yeelight_pc_companion.py" --tray
)
exit
