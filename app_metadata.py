"""Single source of truth for the application's release identity.

Everything that needs a product name or a version number derives it from this
module instead of repeating a literal:

* ``version_info.txt`` (the PyInstaller/Windows executable metadata) is
  *generated* from :func:`version_info_text` by ``tools/write_version_info.py``;
* the Inno Setup installer reads its version from the small include generated
  by ``tools/write_installer_version.py`` (``installer/version.iss``);
* the release build and the artifact naming use :data:`APP_VERSION`;
* the UI exposes :data:`APP_VERSION` through the About text.

Keeping it here means a release bump is a one-line change. The module depends on
nothing but the standard library and is intentionally tiny so that build tooling
can import it without importing the GUI application.

Do not add a second copy of the version number anywhere else. In particular the
installer must never carry a hard-coded fallback version: a missing include is a
build error, not a reason to guess.
"""

from __future__ import annotations

# ---------------------------------------------------------
# Product identity
# ---------------------------------------------------------
APP_NAME = "Yeelight PC Companion"
APP_INTERNAL_NAME = "YeelightPCCompanion"
APP_EXE_NAME = f"{APP_INTERNAL_NAME}.exe"
APP_PUBLISHER = "Yeelight PC Companion Contributors"
APP_DESCRIPTION = "Yeelight PC Companion"
APP_COPYRIGHT = "Copyright (C) 2026 Yeelight PC Companion Contributors"
APP_LICENSE = "GPL-3.0-only"
APP_URL = "https://github.com/BobanAliBrz/Yeelight-PC-Companion"

# ---------------------------------------------------------
# Version
# ---------------------------------------------------------
# Semantic version: (major, minor, patch). This is the only place the released
# version is written down. v1.0.0 is the first public release; v1.0.1 is the
# OpenRGB Windows-service conflict bugfix.
VERSION = (1, 0, 1)

#: ``"1.0.1"`` — the human-facing version.
APP_VERSION = ".".join(str(part) for part in VERSION)

#: ``(1, 0, 1, 0)`` — the fixed-point version Windows file metadata requires.
VERSION_QUAD = VERSION + (0,)

#: ``"1.0.1.0"`` — dotted form of :data:`VERSION_QUAD`.
APP_VERSION_QUAD = ".".join(str(part) for part in VERSION_QUAD)


def version_info_text() -> str:
    """Render the PyInstaller ``version_info.txt`` for :data:`VERSION`."""
    return f"""# UTF-8
#
# GENERATED FILE - do not edit by hand.
# Source of truth: app_metadata.py (VERSION = {VERSION!r})
# Regenerate with: python tools/write_version_info.py
#
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_QUAD!r},
    prodvers={VERSION_QUAD!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        "040904B0",
        [
          StringStruct("CompanyName", "{APP_PUBLISHER}"),
          StringStruct("FileDescription", "{APP_DESCRIPTION}"),
          StringStruct("FileVersion", "{APP_VERSION}"),
          StringStruct("InternalName", "{APP_INTERNAL_NAME}"),
          StringStruct("LegalCopyright", "{APP_COPYRIGHT}"),
          StringStruct("OriginalFilename", "{APP_EXE_NAME}"),
          StringStruct("ProductName", "{APP_NAME}"),
          StringStruct("ProductVersion", "{APP_VERSION}")
        ]
      )
    ]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])])
  ]
)
"""


def installer_version_include_text() -> str:
    """Render the Inno Setup include that carries the version.

    Inno Setup ``#include``s this file. The ``.iss`` script deliberately has no
    fallback: if the file is absent, compilation fails with Inno's own
    "could not open file" error rather than silently building a mislabelled
    installer.
    """
    return f"""\
; GENERATED FILE - do not edit by hand.
; Source of truth: app_metadata.py (VERSION = {VERSION!r})
; Regenerate with: python tools/write_installer_version.py
#define MyAppName "{APP_NAME}"
#define MyAppVersion "{APP_VERSION}"
#define MyAppVersionQuad "{APP_VERSION_QUAD}"
#define MyAppPublisher "{APP_PUBLISHER}"
#define MyAppExeName "{APP_EXE_NAME}"
#define MyAppInternalName "{APP_INTERNAL_NAME}"
#define MyAppURL "{APP_URL}"
#define MyAppLicense "{APP_LICENSE}"
#define MyAppCopyright "{APP_COPYRIGHT}"
"""


if __name__ == "__main__":  # pragma: no cover - convenience only
    print(f"{APP_NAME} {APP_VERSION}")
