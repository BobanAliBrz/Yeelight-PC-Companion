@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem LEGACY / DEVELOPMENT MIGRATION HELPER - not part of a normal user
rem installation.
rem
rem Public users get start-at-logon from the Windows installer, which
rem writes a per-user HKCU\...\Run entry (removed again on uninstall)
rem AND migrates the retired elevated startup tasks as part of setup.
rem Do not tell users to run this script.
rem
rem Yeelight PC Companion now runs UNELEVATED. The old design registered a
rem highest-privilege scheduled task named "YeelightPCCompanion" that
rem started the app through run_yeelight_pc_companion.bat at every logon,
rem which meant an elevated tray process and an unnecessary standing
rem privilege grant. Only the OpenRGB integration needs administrator
rem rights, and it still gets them through its own separate,
rem one-time-approved task "YeelightPCCompanion-OpenRGB", which this
rem script never touches.
rem
rem Running this script now:
rem   1. tries to remove the retired elevated startup tasks WITHOUT
rem      elevation (so an upgraded machine does not start an elevated
rem      duplicate at logon),
rem   2. registers the current per-user HKCU Run entry instead.
rem
rem It never creates a scheduled task, so it needs no administrator
rem rights of its own. Be aware of what that means for step 1: the old
rem tasks were created by an ELEVATED installer and may carry a security
rem descriptor that denies an ordinary user the right to delete them. In
rem that case the deletion below fails with "Access is denied" and the
rem task is REPORTED as left in place - this script cannot remove it, and
rem it deliberately does not pretend otherwise. Re-run the installer
rem (which asks once for a narrowly scoped cleanup) or delete the task
rem from Task Scheduler with administrator rights.
rem ---------------------------------------------------------------

rem Fixed legacy names only. YeelightPCCompanion-OpenRGB is a different
rem task owned by the OpenRGB integration and is never touched here.
set "LEGACY_FAILED="

echo Removing the retired elevated startup tasks (if present)...
call :RemoveLegacyTask "YeelightPCCompanion" "YeelightPCCompanion"
call :RemoveLegacyTask "LuminaLightOrchestrator" "the older LuminaLightOrchestrator"

if defined LEGACY_FAILED (
    echo.
    echo NOTE: at least one retired elevated startup task is still present.
    echo       It may be removed only with administrator rights - the old
    echo       tasks were created elevated and their security descriptor can
    echo       deny deletion to an ordinary user. While it remains, signing in
    echo       may start an elevated duplicate of Yeelight PC Companion.
    echo       Run the installer again and accept its one cleanup prompt, or
    echo       delete the task from Task Scheduler using "Run as administrator".
) else (
    echo No retired elevated startup task remains.
)

rem Prefer the packaged build; fall back to the source entry point.
set "TARGET=%~dp0dist\YeelightPCCompanion\YeelightPCCompanion.exe"
if not exist "%TARGET%" set "TARGET=%~dp0yeelight_pc_companion.py"

echo.
echo Registering per-user start-at-logon for:
echo   %TARGET%
echo (No administrator rights required.)

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "YeelightPCCompanion" /t REG_SZ /d "\"%TARGET%\" --tray" /f
if errorlevel 1 (
    echo.
    echo Failed to register the start-at-logon entry.
) else (
    echo.
    echo Done. Yeelight PC Companion will start in the tray when you sign in.
    echo Remove it again with:  reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "YeelightPCCompanion" /f
)
pause
exit /b 0

rem ---------------------------------------------------------------
rem Remove one fixed legacy startup task, unelevated.
rem
rem The exit code is checked rather than assumed: `schtasks /delete` can
rem legitimately fail for three different reasons (the task is absent, the
rem task is running, or access is denied) and this script must not claim a
rem removal that did not happen. A fresh `/query` afterwards decides.
rem ---------------------------------------------------------------
:RemoveLegacyTask
set "TASK_NAME=%~1"
set "TASK_LABEL=%~2"

schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo   %TASK_LABEL% is not present - nothing to remove.
    goto :eof
)

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
if errorlevel 1 (
    echo   Warning: %TASK_LABEL% could NOT be removed without administrator rights.
    set "LEGACY_FAILED=1"
    goto :eof
)

rem Verify instead of trusting the exit code.
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if not errorlevel 1 (
    echo   Warning: %TASK_LABEL% is still present after the deletion attempt.
    set "LEGACY_FAILED=1"
    goto :eof
)

echo   Removed %TASK_LABEL%.
goto :eof
