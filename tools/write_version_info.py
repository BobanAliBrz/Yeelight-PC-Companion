"""Generate ``version_info.txt`` from :mod:`app_metadata`.

Windows executable metadata cannot be computed at run time, so PyInstaller needs
a file. That file is a build output, not a hand-maintained source: this script
renders it from the single version source of truth.

Usage::

    python tools/write_version_info.py            # write version_info.txt
    python tools/write_version_info.py --check    # fail if it is out of date

``--check`` is used by the release build so that a stale checked-in file cannot
silently ship a wrong version.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app_metadata  # noqa: E402  (path set up above)

TARGET = os.path.join(REPO_ROOT, "version_info.txt")


def build_text() -> str:
    """The exact bytes ``version_info.txt`` should contain (LF newlines)."""
    return app_metadata.version_info_text()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero when the file is missing or stale",
    )
    args = parser.parse_args(argv)

    expected = build_text()

    if args.check:
        try:
            with open(TARGET, encoding="utf-8") as handle:
                actual = handle.read()
        except FileNotFoundError:
            print(f"ERROR: {TARGET} is missing; run tools/write_version_info.py")
            return 1
        if actual.replace("\r\n", "\n") != expected:
            print(
                "ERROR: version_info.txt is out of date with app_metadata.py "
                f"({app_metadata.APP_VERSION}); run tools/write_version_info.py"
            )
            return 1
        print(f"version_info.txt is up to date ({app_metadata.APP_VERSION}).")
        return 0

    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(expected)
    print(f"Wrote {TARGET} for version {app_metadata.APP_VERSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
