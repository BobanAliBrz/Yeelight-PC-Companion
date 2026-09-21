# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Yeelight PC Companion.
#
# PRIVACY CONTRACT: this spec must never reference the maintainer's (or any
# user's) real config.json. Only the safe, placeholder-only
# config.example.json template may be bundled. A build must be able to
# succeed on a clean checkout that has no personal configuration present.

a = Analysis(
    ["yeelight_pc_companion.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("config.example.json", "."),
        ("yeelight_pc_companion.ico", "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The UI is PyQt6 only; nothing in the project imports tkinter. PyInstaller
    # still bundles the Tcl/Tk runtime and its data files (~7 MB of
    # _tcl_data/_tk_data plus tcl86t.dll, tk86t.dll and _tkinter.pyd) because a
    # hook allows for the possibility. Excluding it keeps the released payload
    # smaller and keeps an unused interpreter out of the shipped bundle.
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YeelightPCCompanion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="yeelight_pc_companion.ico",
    version="version_info.txt",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="YeelightPCCompanion",
)
