@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem DEVELOPMENT BUILD HELPER (source checkout only).
rem
rem This is not part of the user installation path. End users install
rem Yeelight PC Companion with the Windows installer produced by
rem build_release.ps1; that needs no Python, no BAT file and no manual
rem configuration step.
rem
rem A first-run wizard collects the location, devices and integrations on
rem first launch and saves them under %LOCALAPPDATA%\Yeelight PC
rem Companion. No configuration file has to be created by hand, and this
rem build never requires a personal config.json to exist.
rem
rem For the full deterministic release pipeline (tests -> PyInstaller ->
rem privacy scans -> portable ZIP -> installer) use build_release.ps1.
rem ---------------------------------------------------------------

set "APP_NAME=YeelightPCCompanion"
set "DIST_DIR=%~dp0dist\%APP_NAME%"

python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller from requirements-build.txt...
    python -m pip install -r requirements-build.txt
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

rem Keep the generated Windows version metadata in step with app_metadata.py.
python tools\write_version_info.py
if errorlevel 1 (
    echo Failed to generate version_info.txt.
    pause
    exit /b 1
)

echo Building %APP_NAME%.exe...
python -m PyInstaller --clean --noconfirm yeelight_pc_companion.spec
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------
rem Ship the runtime assets the app expects to find NEXT TO the
rem executable. PyInstaller (onedir) puts the spec's `datas` inside
rem `_internal\`, but the app resolves its directory via
rem `os.path.dirname(sys.executable)` (see get_app_dir), so the tray
rem icon and the configuration template must also sit in the app root.
rem
rem config.example.json is shipped as documentation/template data only.
rem It is NOT required: the first-run wizard creates the real
rem configuration under %LOCALAPPDATA%.
rem ---------------------------------------------------------------
copy /y "%~dp0yeelight_pc_companion.ico" "%DIST_DIR%\yeelight_pc_companion.ico" >nul
copy /y "%~dp0config.example.json" "%DIST_DIR%\config.example.json" >nul

rem ---------------------------------------------------------------
rem PRIVACY GUARD
rem This build never requires and never copies a personal config.json.
rem A normal (non-portable) build must also never contain portable.flag,
rem which would silently switch the app to in-folder configuration.
rem ---------------------------------------------------------------
if exist "%DIST_DIR%\config.json" (
    echo.
    echo PRIVACY ERROR: a personal config.json was found in the build output:
    echo   %DIST_DIR%\config.json
    echo This must never happen. Aborting.
    pause
    exit /b 1
)
if exist "%DIST_DIR%\portable.flag" (
    echo.
    echo PRIVACY ERROR: portable.flag was found in a normal build output:
    echo   %DIST_DIR%\portable.flag
    echo Aborting.
    pause
    exit /b 1
)

rem Full scanner: forbidden filenames, the portable marker direction, and (only
rem when a private-value source is configured) the maintainer's exact values.
rem See tools\release_privacy_scan.py - no private value is stored in the repo.
python tools\release_privacy_scan.py --artifact "%DIST_DIR%"
if errorlevel 1 (
    echo.
    echo PRIVACY ERROR: the privacy scanner rejected the build output.
    pause
    exit /b 1
)

echo.
echo Build complete:
echo %DIST_DIR%\%APP_NAME%.exe
echo.
echo Verified: no personal config.json and no portable.flag in the output.
echo First run shows a setup wizard; no configuration file has to be created by hand.
echo run_yeelight_pc_companion.bat will use this exe automatically.
pause
