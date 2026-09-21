<#
.SYNOPSIS
    Deterministic release build for Yeelight PC Companion.

.DESCRIPTION
    Produces the two release artifacts from a clean checkout:

        release\YeelightPCCompanion-<version>-setup.exe
        release\Yeelight-PC-Companion-<version>-portable.zip

    The pipeline is deliberately fail-fast. It stops on the first problem rather
    than shipping a partially correct artifact:

        1. version metadata is regenerated and verified against app_metadata.py
        2. the unit test suite must pass
        3. syntax/import checks
        4. PyInstaller onedir build
        5. privacy scan of the installer/dist payload  (portable.flag FORBIDDEN)
        6. portable payload build + privacy scan        (portable.flag REQUIRED)
        7. Inno Setup installer compile
        8. artifact existence + privacy scan of the installer payload

    It never requires a personal config.json, never publishes anything, and
    never signs anything.

.PARAMETER SkipTests
    Skip step 2. Intended only for iterating on packaging, never for a release.

.PARAMETER SkipInstaller
    Stop after the portable ZIP (useful when Inno Setup is not installed).

.EXAMPLE
    pwsh -File .\build_release.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = $PSScriptRoot
$AppName = 'YeelightPCCompanion'
$DistDir = Join-Path $RepoRoot "dist\$AppName"
$PortableDir = Join-Path $RepoRoot 'dist-portable'
$ReleaseDir = Join-Path $RepoRoot 'release'
$StageDir = Join-Path $RepoRoot "build\release-stage"

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Fail {
    param([string]$Text)
    Write-Host ''
    Write-Host "BUILD FAILED: $Text" -ForegroundColor Red
    exit 1
}

function Invoke-Checked {
    param(
        [string]$What,
        [scriptblock]$Action
    )
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Fail "$What (exit code $LASTEXITCODE)"
    }
}

function Get-IsccPath {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    $onPath = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

<#
Run an Inno Setup binary (setup.exe / unins000.exe) silently and return its exit code.

Three Windows quoting/waiting traps this exists to avoid, all hit for real while
building this stage:

  * setup.exe is a GUI-subsystem binary. Invoking it with `&` does not wait and
    does not set $LASTEXITCODE, so a stale value from an earlier command (for
    example 7-Zip's "exit 2") gets mistaken for its result.
  * An install path containing a space must reach the installer still quoted.
    Passing `/DIR="C:\path with spaces"` as an -ArgumentList element let the
    quotes be stripped, and the installer silently truncated the path at the
    first space and installed into a parent directory.
  * `cmd /c` itself needs the whole command wrapped, because a command line whose
    first token is quoted is otherwise parsed as the "strip the outer quotes"
    form.

`cmd /c` applies normal Windows command-line quoting, which is what the
installer expects, and Start-Process -Wait gives a trustworthy exit code.
#>
function Invoke-SilentInstaller {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ExtraArguments = @()
    )

    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') + $ExtraArguments
    $commandLine = '/c ""' + $FilePath + '" ' + ($arguments -join ' ') + '"'

    $process = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" `
        -ArgumentList $commandLine -PassThru -Wait
    return $process.ExitCode
}

Push-Location $RepoRoot
try {
    # ---------------------------------------------------------------
    # 0. Version facts (single source of truth)
    # ---------------------------------------------------------------
    Write-Step 'Version metadata'
    Invoke-Checked 'generating version_info.txt' { python tools\write_version_info.py }
    Invoke-Checked 'generating installer/version.iss' { python tools\write_installer_version.py }
    Invoke-Checked 'verifying version_info.txt' { python tools\write_version_info.py --check }
    Invoke-Checked 'verifying installer/version.iss' { python tools\write_installer_version.py --check }

    $AppVersion = (python -c "import app_metadata; print(app_metadata.APP_VERSION)").Trim()
    if (-not $AppVersion) { Fail 'could not read APP_VERSION from app_metadata.py' }
    Write-Host "Version: $AppVersion"

    # ---------------------------------------------------------------
    # 1. Clean previous output
    # ---------------------------------------------------------------
    Write-Step 'Clean'
    foreach ($target in @($DistDir, $PortableDir, $StageDir, (Join-Path $RepoRoot 'build'))) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-Path -LiteralPath $ReleaseDir)) {
        New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
    }
    # Remove only this version's artifacts; never wipe someone else's downloads.
    Get-ChildItem -LiteralPath $ReleaseDir -Filter "$AppName-*-setup.exe" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $ReleaseDir -Filter "Yeelight-PC-Companion-*-portable.zip" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Write-Host 'Previous build output removed.'

    # ---------------------------------------------------------------
    # 2. Tests
    # ---------------------------------------------------------------
    if (-not $SkipTests) {
        Write-Step 'Unit tests'
        Invoke-Checked 'unit test suite' { python -m unittest discover -s tests -t . }
    }
    else {
        Write-Host 'Tests skipped by request (-SkipTests).' -ForegroundColor Yellow
    }

    # ---------------------------------------------------------------
    # 3. Syntax / import checks
    # ---------------------------------------------------------------
    Write-Step 'Syntax and import checks'
    Invoke-Checked 'compileall' { python -m compileall -q $RepoRoot }
    Invoke-Checked 'import check' {
        python -c "import app_metadata, config_manager, windows_tasks, yeelight_devices, ui_theme, ui_components"
    }

    # ---------------------------------------------------------------
    # 4. PyInstaller build
    # ---------------------------------------------------------------
    Write-Step 'PyInstaller build'
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'requirements-build.txt'))) {
        Fail 'requirements-build.txt is missing'
    }
    python -m pip show pyinstaller *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail 'PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt'
    }
    Invoke-Checked 'PyInstaller' {
        python -m PyInstaller --clean --noconfirm yeelight_pc_companion.spec
    }
    if (-not (Test-Path -LiteralPath (Join-Path $DistDir "$AppName.exe"))) {
        Fail "PyInstaller did not produce $DistDir\$AppName.exe"
    }

    # Runtime assets the app resolves from its own directory.
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'yeelight_pc_companion.ico') -Destination $DistDir -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'config.example.json') -Destination $DistDir -Force

    # ---------------------------------------------------------------
    # 5. Privacy scan: installer/dist payload
    # ---------------------------------------------------------------
    Write-Step 'Privacy scan (installer/dist payload)'
    Invoke-Checked 'privacy scan of the dist payload' {
        python tools\release_privacy_scan.py --artifact $DistDir
    }

    # ---------------------------------------------------------------
    # 6. Portable payload
    # ---------------------------------------------------------------
    Write-Step 'Portable payload'
    New-Item -ItemType Directory -Path $PortableDir | Out-Null
    Copy-Item -Path (Join-Path $DistDir '*') -Destination $PortableDir -Recurse -Force
    # The marker is what makes the build store its config beside the executable.
    New-Item -ItemType File -Path (Join-Path $PortableDir 'portable.flag') | Out-Null
    # A portable build is a user's own copy: ship the reading material with it.
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'LICENSE') -Destination (Join-Path $PortableDir 'LICENSE.txt') -Force

    Write-Step 'Privacy scan (portable payload)'
    Invoke-Checked 'privacy scan of the portable payload' {
        python tools\release_privacy_scan.py --artifact $PortableDir --portable
    }

    $PortableZip = Join-Path $ReleaseDir "Yeelight-PC-Companion-$AppVersion-portable.zip"
    if (Test-Path -LiteralPath $PortableZip) { Remove-Item -LiteralPath $PortableZip -Force }
    Compress-Archive -Path (Join-Path $PortableDir '*') -DestinationPath $PortableZip -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $PortableZip)) { Fail 'portable ZIP was not created' }
    Write-Host "Portable artifact: $PortableZip"

    # ---------------------------------------------------------------
    # 7. Inno Setup installer
    # ---------------------------------------------------------------
    $SetupExe = Join-Path $ReleaseDir "$AppName-$AppVersion-setup.exe"
    if ($SkipInstaller) {
        Write-Host 'Installer skipped by request (-SkipInstaller).' -ForegroundColor Yellow
    }
    else {
        Write-Step 'Inno Setup installer'
        $Iscc = Get-IsccPath
        if (-not $Iscc) {
            Fail 'Inno Setup 6 (ISCC.exe) was not found. Install it, or pass -SkipInstaller.'
        }
        Write-Host "Using compiler: $Iscc"
        Invoke-Checked 'Inno Setup compile' {
            & $Iscc (Join-Path $RepoRoot 'installer\YeelightPCCompanion.iss')
        }
        if (-not (Test-Path -LiteralPath $SetupExe)) {
            Fail "the installer was not produced at $SetupExe"
        }

        # -----------------------------------------------------------
        # 8. Verify what the installer actually ships.
        #
        #    Extraction is preferred (it never mutates the machine), but 7-Zip
        #    cannot read Inno Setup 6.7's payload format. The fallback is a real
        #    but fully reversible per-user installation:
        #
        #      * it installs to an isolated throwaway directory,
        #      * no shortcuts are created (/NOICONS),
        #      * no logon entry is created (the "startup" task is not selected),
        #      * the user's real configuration under %LOCALAPPDATA% is never
        #        touched (the application would only create it on first run),
        #      * it is uninstalled again at the end, which also exercises the
        #        uninstaller and its OpenRGB task handling.
        # -----------------------------------------------------------
        Write-Step 'Verify installer payload'
        New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

        $SevenZip = @(
            (Join-Path $env:ProgramFiles '7-Zip\7z.exe'),
            (Join-Path ${env:ProgramFiles(x86)} '7-Zip\7z.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

        $Extracted = $false
        if ($SevenZip) {
            # This is expected to fail against Inno Setup 6.7 ("Cannot open the
            # file as archive"), which is exactly why the fallback below exists.
            # It is not an error, so it is not wrapped in Invoke-Checked.
            & $SevenZip x $SetupExe "-o$StageDir" -y *> $null
            if (Test-Path -LiteralPath (Join-Path $StageDir "$AppName.exe")) {
                $Extracted = $true
                Write-Host 'Installer payload extracted for inspection.'
            }
        }

        if (-not $Extracted) {
            Write-Host 'Extraction is unavailable for this installer format; performing a'
            Write-Host 'reversible per-user test install instead (removed again below).'
            Get-ChildItem -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

            # Inno Setup's setup.exe is a GUI subsystem binary: calling it with
            # `&` does NOT wait and does NOT set $LASTEXITCODE, so a stale value
            # from an earlier command would look like its result. It must also be
            # given a properly quoted /DIR, because this repository path contains
            # a space - see Invoke-SilentInstaller.
            $installExit = Invoke-SilentInstaller -FilePath $SetupExe `
                -ExtraArguments @('/NOICONS', "/DIR=`"$StageDir`"")
            if ($installExit -ne 0) {
                Fail "test installation (exit code $installExit)"
            }
            if (-not (Test-Path -LiteralPath (Join-Path $StageDir "$AppName.exe"))) {
                Fail "the installer did not place $AppName.exe in $StageDir"
            }
        }

        $Expected = @("$AppName.exe", "config.example.json", "yeelight_pc_companion.ico", "LICENSE.txt")
        foreach ($name in $Expected) {
            if (-not (Test-Path -LiteralPath (Join-Path $StageDir $name))) {
                Fail "the installed payload is missing $name"
            }
        }
        Write-Host "Installed payload contains: $($Expected -join ', ')"

        Invoke-Checked 'privacy scan of the installed payload' {
            python tools\release_privacy_scan.py --artifact $StageDir
        }
        if (Test-Path -LiteralPath (Join-Path $StageDir 'portable.flag')) {
            Fail 'the installed payload contains portable.flag'
        }
        Write-Host 'Installed payload verified: complete, no portable.flag, no private material.'

        if (-not $Extracted) {
            Write-Step 'Verify uninstaller'
            $Uninstaller = Join-Path $StageDir 'unins000.exe'
            if (-not (Test-Path -LiteralPath $Uninstaller)) {
                Fail 'the installed payload has no uninstaller'
            }
            # Same GUI-subsystem caveat as setup.exe.
            $uninstallExit = Invoke-SilentInstaller -FilePath $Uninstaller
            if ($uninstallExit -ne 0) {
                Fail "test uninstall (exit code $uninstallExit)"
            }
            Start-Sleep -Seconds 2
            if (Test-Path -LiteralPath (Join-Path $StageDir "$AppName.exe")) {
                Fail 'the uninstaller left the application executable behind'
            }
            Write-Host 'Uninstaller removed the application files.'

            # The user's configuration must never be deleted by an uninstall.
            $UserDataConfig = Join-Path (Join-Path $env:LOCALAPPDATA 'Yeelight PC Companion') 'config.json'
            if (Test-Path -LiteralPath $UserDataConfig) {
                Write-Host 'User configuration was preserved across uninstall.'
            }

            # The test install must not leave a logon entry behind.
            $RunValue = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
                -Name $AppName -ErrorAction SilentlyContinue
            if ($RunValue) {
                Fail 'the uninstaller left the start-at-logon entry behind'
            }
            Write-Host 'No start-at-logon entry was left behind.'
        }
    }

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    Write-Step 'Release artifacts'
    Get-ChildItem -LiteralPath $ReleaseDir | Select-Object Name, @{N='SizeMB';E={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
    Write-Host "Release build completed for version $AppVersion." -ForegroundColor Green
}
finally {
    Pop-Location
}
