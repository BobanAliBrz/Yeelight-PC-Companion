"""Artifact privacy scanner for Yeelight PC Companion releases.

The scanner answers one question: *does this release artifact contain anything
that belongs to the maintainer's machine rather than to the product?*

It is deliberately artifact-specific rather than one generic string sweep. A
finished PyInstaller bundle is full of third-party binaries, so rules that look
for short numeric or address-like substrings produce false positives in
dependency code and train everyone to ignore the check. The policy below is
therefore split into three independent layers:

1. **Forbidden filenames** - artifacts that must never be shipped at all
   (``config.json``, its backups, runtime logs, provisioning diagnostics). This
   layer is exact and objective.
2. **The portable marker** - ``portable.flag`` must be **absent** from a normal
   install/dist payload and **present** in the portable payload. Both
   directions are errors, and each is reported for the artifact it belongs to.
3. **Exact private values, supplied from OUTSIDE tracked source.** The scanner
   itself stores **no** maintainer value and no fingerprint of one; it checks the
   exact literals it is handed at run time. See below.

How layer 3 gets its values
---------------------------

A fingerprint is not a safe way to record a low-entropy secret in a public
repository. Truncating SHA-256 to 8 hex characters keeps only 32 bits, and even
a full unsalted digest of a private IPv4 address or a short user name is
brute-forceable in seconds. A *length* recorded next to it narrows the search
further. The scanner therefore does not store either one any more.

Values come from, in order:

* ``--private-value`` on the command line (repeatable, for a one-off check),
* ``--private-values FILE`` or ``$YPC_PRIVATE_VALUES_FILE`` - a **gitignored**
  local values file,
* ``$YPC_PRIVATE_VALUES`` - one value per line, so CI can pass repository
  secrets without ever putting the values in a tracked file.

The local file format is one value per line; blank lines and lines starting with
``#`` are ignored, an optional UTF-8 BOM is tolerated, and a JSON array of
strings is also accepted. The default location, ``private_values.local.txt`` in
the repository root, is listed in ``.gitignore``.

Design rules this layer must keep satisfying:

* **Builds work with no private-value source configured at all.** The layer
  simply has nothing to match, which is the normal state for a contributor, for
  CI without the secret, and for the public release pipeline.
* **A maintainer's local/CI release build can supply the exact values**, so the
  check is as strong as it was - it is only the *storage* that moved out of
  tracked source.
* **The values never enter tracked files**, and neither do tests derived from
  them: the test suite uses synthetic fake secrets only.
* A matched value is never printed. Violations name the file and the number of
  matched values.

Usage::

    python tools/release_privacy_scan.py --artifact dist/YeelightPCCompanion
    python tools/release_privacy_scan.py --artifact dist-portable --portable
    python tools/release_privacy_scan.py --repo            # tracked files only
    python tools/release_privacy_scan.py --repo --private-values private_values.local.txt

Exit code is 0 when clean and 1 when any violation is found, so the release
build can fail on it directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------
# Layer 1: prohibited artifact contents (exact names)
# ---------------------------------------------------------
# These are files a *user's* machine may legitimately contain, but which must
# never be part of a release payload.
FORBIDDEN_FILENAMES = (
    "config.json",
    "config.json.bak",
    "yeelight_pc_companion_debug.log",
    "lumina_debug.log",
    "crash.log",
    "openrgb_provision_result.json",
    "openrgb_provisioning.log",
    "portable.flag",
)

# Rotating/backup variants: crash.log.1, config.json.bak.2, debug.log.3 ...
FORBIDDEN_PREFIXES = (
    "config.json.bak.",
    "yeelight_pc_companion_debug.log.",
    "lumina_debug.log.",
    "crash.log.",
)

# Directories whose whole subtree must never be shipped.
FORBIDDEN_DIRNAMES = ("__pycache__",)

# The portable marker is special: required in one artifact, forbidden in the
# other. It is checked from FORBIDDEN_FILENAMES for dist/installer payloads and
# positively for --portable.
PORTABLE_MARKER = "portable.flag"

# ---------------------------------------------------------
# Layer 3: private values, supplied from outside tracked source
# ---------------------------------------------------------
#: Environment variable holding one private value per line. This is how CI can
#: receive the maintainer's values from repository secrets without any value -
#: or a reversible/truncated fingerprint of one - entering a tracked file.
PRIVATE_VALUES_ENV_VAR = "YPC_PRIVATE_VALUES"

#: Environment variable naming a private-values file (alternative to --private-values).
PRIVATE_VALUES_FILE_ENV_VAR = "YPC_PRIVATE_VALUES_FILE"

#: Gitignored default location of the local private-values file.
DEFAULT_PRIVATE_VALUES_FILE = os.path.join(REPO_ROOT, "private_values.local.txt")

#: Shortest value worth matching. Below this a literal behaves like the generic
#: substring rule the policy explicitly refuses (see the tests): a two-decimal
#: number such as ``20.5`` would collide with ordinary content in dependency
#: binaries and legitimate build output.
MIN_PRIVATE_VALUE_LENGTH = 6

#: Values longer than this are almost certainly a whole file pasted by mistake.
MAX_PRIVATE_VALUE_LENGTH = 4096

# Text extensions worth scanning. Binaries are never content-scanned: private
# strings would have to be encoded to match anyway, and scanning third-party
# DLLs/EXEs is exactly the false-positive source this policy avoids.
TEXT_EXTENSIONS = (
    ".py",
    ".txt",
    ".json",
    ".md",
    ".iss",
    ".bat",
    ".cmd",
    ".ps1",
    ".yml",
    ".yaml",
    ".cfg",
    ".ini",
    ".toml",
    ".spec",
    ".log",
    ".csv",
)

# Never content-scan the scanner itself or the metadata that deliberately
# records the policy.
SELF_EXEMPT_BASENAMES = ("release_privacy_scan.py",)

MAX_TEXT_BYTES = 8 * 1024 * 1024


class Violation:
    """One detected privacy problem."""

    __slots__ = ("path", "reason")

    def __init__(self, path, reason):
        self.path = path
        self.reason = reason

    def __str__(self):
        return f"{self.path}: {self.reason}"


def _is_forbidden_name(basename):
    lowered = basename.lower()
    if lowered == PORTABLE_MARKER:
        return True
    if lowered in FORBIDDEN_FILENAMES:
        return True
    if lowered.endswith(".tmp") and lowered.startswith(".config-"):
        return True
    for prefix in FORBIDDEN_PREFIXES:
        if lowered.startswith(prefix):
            return True
    return False


def _is_text_file(path):
    _, ext = os.path.splitext(path)
    return ext.lower() in TEXT_EXTENSIONS


def iter_artifact_files(artifact_dir):
    """Yield (absolute_path, relative_path) for every file under *artifact_dir*."""
    artifact_dir = os.path.abspath(artifact_dir)
    for root, dirnames, filenames in os.walk(artifact_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in FORBIDDEN_DIRNAMES]
        for name in filenames:
            full = os.path.join(root, name)
            yield full, os.path.relpath(full, artifact_dir)


def fingerprint(value):
    """A stable SHA-256-based identifier for a value, for diagnostics only.

    This is **not** a privacy mechanism and must never be used to *store* a
    private value in source: a truncated digest of a low-entropy value (a
    private IPv4 address, a short user name) is brute-forceable. It exists so
    tests and reports can refer to a value reproducibly without printing it.
    """
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def normalize_private_values(values):
    """Trim, drop blanks, de-duplicate (order-preserving) and filter by length."""
    seen = set()
    result = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        if not (MIN_PRIVATE_VALUE_LENGTH <= len(text) <= MAX_PRIVATE_VALUE_LENGTH):
            continue
        seen.add(text)
        result.append(text)
    return result


def parse_private_values_text(text):
    """Parse a private-values file's text into a list of values.

    Accepts one value per line (``#`` comments and blank lines ignored), an
    optional UTF-8 BOM, and a JSON array of strings.
    """
    if text is None:
        return []
    body = text.lstrip("\ufeff")
    stripped = body.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]

    values = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        values.append(line)
    return values


def load_private_values(
    explicit=(),
    file_path=None,
    environ=None,
    env_var=PRIVATE_VALUES_ENV_VAR,
    file_env_var=PRIVATE_VALUES_FILE_ENV_VAR,
    default_file=DEFAULT_PRIVATE_VALUES_FILE,
):
    """Collect the exact private literals to reject.

    Sources, in order: *explicit* values, a values file (the *file_path*
    argument, else ``$YPC_PRIVATE_VALUES_FILE``, else *default_file* when it
    exists), and ``$YPC_PRIVATE_VALUES`` (one value per line, for CI secrets).

    Returns:
        ``(values, sources)`` where *values* is the normalized list and
        *sources* is a list of human-readable descriptions of what was loaded
        (never a value). Anything unreadable is skipped silently: builds must
        keep working with no private-value source configured.
    """
    environ = os.environ if environ is None else environ
    values = list(explicit)
    sources = ["--private-value" for _ in explicit]

    candidate_file = file_path or environ.get(file_env_var) or ""
    if not candidate_file:
        if default_file and os.path.isfile(default_file):
            candidate_file = default_file
    if candidate_file:
        try:
            with open(candidate_file, "rb") as handle:
                raw = handle.read()
        except OSError:
            raw = None
        if raw is not None:
            loaded = parse_private_values_text(raw.decode("utf-8", errors="replace"))
            if loaded:
                values.extend(loaded)
                sources.append(f"file {candidate_file}")

    from_environment = parse_private_values_text(environ.get(env_var))
    if from_environment:
        values.extend(from_environment)
        sources.append(f"${env_var}")

    return normalize_private_values(values), sources


def find_private_values(text, private_values=()):
    """1-based indices of the configured private values present in *text*.

    Matching is an exact plaintext substring test against the values handed in at
    run time. No value is ever stored in this module, and a matched value is
    never returned - only its position - so a report cannot leak it.

    The :data:`MIN_PRIVATE_VALUE_LENGTH` floor is enforced here as well as in
    :func:`normalize_private_values`, so a caller that passes a short literal
    straight in still cannot turn this into the generic substring rule the policy
    refuses.
    """
    if not text or not private_values:
        return []
    return [
        index
        for index, value in enumerate(private_values, start=1)
        if value and len(str(value)) >= MIN_PRIVATE_VALUE_LENGTH and str(value) in text
    ]


def describe_value_count(count):
    """A readable, non-revealing description of how many values matched."""
    if count == 1:
        return "1 configured private value"
    return f"{count} configured private values"


def scan_artifact(artifact_dir, portable=False, private_values=()):
    """Scan a built artifact directory.

    Args:
        artifact_dir: the payload to scan (``dist\\YeelightPCCompanion`` or the
            extracted portable directory).
        portable: when True, ``portable.flag`` is *required* instead of
            forbidden.
        private_values: exact private literals to look for. Empty is fine, and
            is the normal case for a contributor or a public CI run.

    Returns:
        A list of :class:`Violation` (empty when clean).
    """
    if not os.path.isdir(artifact_dir):
        return [Violation(artifact_dir, "artifact directory does not exist")]

    private_values = normalize_private_values(private_values)
    violations = []
    marker_seen = False

    for full, rel in iter_artifact_files(artifact_dir):
        basename = os.path.basename(full)
        lowered = basename.lower()

        if lowered == PORTABLE_MARKER:
            marker_seen = True
            if not portable:
                violations.append(
                    Violation(rel, "portable.flag must NOT be present in an installer/dist payload")
                )
            continue

        if _is_forbidden_name(basename):
            violations.append(Violation(rel, "forbidden artifact filename for a release payload"))
            continue

        if basename in SELF_EXEMPT_BASENAMES:
            continue

        if not _is_text_file(full):
            continue

        try:
            if os.path.getsize(full) > MAX_TEXT_BYTES:
                continue
            with open(full, "rb") as handle:
                blob = handle.read()
        except OSError:
            continue

        text = blob.decode("utf-8", errors="ignore")
        matched = find_private_values(text, private_values)
        if matched:
            violations.append(
                Violation(rel, f"contains {describe_value_count(len(matched))}")
            )

    if portable and not marker_seen:
        violations.append(
            Violation(PORTABLE_MARKER, "portable payload is missing its portable.flag marker")
        )

    return violations


def repo_tracked_files():
    """Tracked files at HEAD, so build output and local config are ignored."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    return [
        os.path.join(REPO_ROOT, name)
        for name in result.stdout.decode("utf-8", errors="replace").split("\0")
        if name
    ]


def scan_repo(private_values=()):
    """Scan the tracked files of the repository itself."""
    tracked = repo_tracked_files()
    if tracked is None:
        return [Violation(REPO_ROOT, "could not enumerate tracked files (git unavailable)")]

    private_values = normalize_private_values(private_values)
    violations = []
    for full in tracked:
        rel = os.path.relpath(full, REPO_ROOT)
        basename = os.path.basename(full)

        if basename in SELF_EXEMPT_BASENAMES or basename == "config.example.json":
            continue
        if _is_forbidden_name(basename):
            violations.append(Violation(rel, "forbidden filename is tracked in the repository"))
            continue
        if not _is_text_file(full):
            continue
        try:
            with open(full, "rb") as handle:
                text = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        matched = find_private_values(text, private_values)
        if matched:
            violations.append(
                Violation(rel, f"contains {describe_value_count(len(matched))}")
            )
    return violations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--artifact", help="built artifact directory to scan")
    target.add_argument(
        "--repo",
        action="store_true",
        help="scan the repository's tracked files (no build output)",
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="this artifact is the portable payload: portable.flag is required",
    )
    parser.add_argument(
        "--private-value",
        action="append",
        default=[],
        help="an exact private literal to reject (repeatable)",
    )
    parser.add_argument(
        "--private-values",
        default=None,
        help=(
            "file holding the exact private literals to reject, one per line "
            "(default: $" + PRIVATE_VALUES_FILE_ENV_VAR + ", else "
            "private_values.local.txt when present). Never commit this file."
        ),
    )
    args = parser.parse_args(argv)

    private_values, sources = load_private_values(
        explicit=args.private_value, file_path=args.private_values
    )

    if args.repo:
        violations = scan_repo(private_values)
        label = "repository (tracked files)"
    else:
        violations = scan_artifact(
            args.artifact, portable=args.portable, private_values=private_values
        )
        label = f"{args.artifact} ({'portable' if args.portable else 'installer/dist'} payload)"

    if sources:
        print(
            "Private-value check enabled: "
            + f"{len(private_values)} value(s) from "
            + ", ".join(sorted(set(sources)))
        )
    else:
        print(
            "Private-value check disabled: no private-value source configured "
            f"(--private-value/--private-values/${PRIVATE_VALUES_ENV_VAR}). "
            "Generic release rules still apply."
        )

    if violations:
        print(f"PRIVACY FAILURE: {len(violations)} problem(s) in {label}:")
        for violation in violations:
            print(f"  - {violation}")
        return 1

    print(f"Privacy scan clean: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
