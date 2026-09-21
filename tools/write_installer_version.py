"""Generate ``installer/version.iss`` from :mod:`app_metadata`.

Inno Setup cannot import Python, so the installer's version facts are rendered
into a small generated include. The ``.iss`` script ``#include``s it and has no
hard-coded fallback: when the include is missing, compilation fails instead of
producing an installer labelled with a guessed version.

Usage::

    python tools/write_installer_version.py            # write the include
    python tools/write_installer_version.py --check    # fail if stale/missing
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app_metadata  # noqa: E402  (path set up above)

TARGET = os.path.join(REPO_ROOT, "installer", "version.iss")


def build_text() -> str:
    """The exact bytes ``installer/version.iss`` should contain (LF newlines)."""
    return app_metadata.installer_version_include_text()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero when the include is missing or stale",
    )
    args = parser.parse_args(argv)

    expected = build_text()

    if args.check:
        try:
            with open(TARGET, encoding="utf-8") as handle:
                actual = handle.read()
        except FileNotFoundError:
            print(f"ERROR: {TARGET} is missing; run tools/write_installer_version.py")
            return 1
        if actual.replace("\r\n", "\n") != expected:
            print(
                "ERROR: installer/version.iss is out of date with app_metadata.py "
                f"({app_metadata.APP_VERSION}); run tools/write_installer_version.py"
            )
            return 1
        print(f"installer/version.iss is up to date ({app_metadata.APP_VERSION}).")
        return 0

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(expected)
    print(f"Wrote {TARGET} for version {app_metadata.APP_VERSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
