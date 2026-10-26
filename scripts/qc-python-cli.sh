#!/bin/sh
# Local quality runner for lecture-localizer stage checks.
# Requires Python 3.12+ and the local media tools for the doctor tests.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

stage() {
    printf '\n== qc stage: %s ==\n' "$1"
}

if [ ! -x "$PY" ]; then
    stage "bootstrap venv"
    python3 -m venv "$VENV"
fi

stage "install package with dev extras"
"$PY" -m pip install --quiet --disable-pip-version-check -e "$ROOT[dev]"

stage "ruff format check"
"$VENV/bin/ruff" format --check "$ROOT/src" "$ROOT/tests" "$ROOT/scripts"

stage "ruff check"
"$VENV/bin/ruff" check "$ROOT/src" "$ROOT/tests" "$ROOT/scripts"

stage "mypy"
"$VENV/bin/mypy" "$ROOT/src" "$ROOT/tests" "$ROOT/scripts/make-synthetic-example.py"

stage "pytest"
"$VENV/bin/pytest" "$ROOT/tests"

stage "build"
rm -rf "$ROOT/dist"
"$PY" -m build --outdir "$ROOT/dist" "$ROOT"

stage "artifact privacy gate"
"$PY" - "$ROOT/dist" <<'EOF'
"""Reject archives that contain anything outside the public product allowlist."""

import pathlib
import sys
import tarfile
import zipfile

dist = pathlib.Path(sys.argv[1])
sdists = sorted(dist.glob("lecture_localizer-*.tar.gz"))
wheels = sorted(dist.glob("lecture_localizer-*.whl"))
if len(sdists) != 1 or len(wheels) != 1:
    raise SystemExit(f"expected one sdist and one wheel in {dist}")

SDIST_ALLOWLIST = {
    "PKG-INFO",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    ".gitignore",
    "src",
    "tests",
    "scripts",
}
FORBIDDEN = ("private", ".memory", ".agents", ".venv", ".github", ".git")
EXPECTED_SDIST = (
    "src/lecture_localizer/cli.py",
    "src/lecture_localizer/media.py",
    "src/lecture_localizer/settings.py",
    "src/lecture_localizer/__init__.py",
    "tests/test_media.py",
    "scripts/make-synthetic-example.py",
    "scripts/qc-python-cli.sh",
)


def validate_sdist(raw_members: list[str]) -> str | None:
    """Return a rejection reason for a raw member list, or None."""
    members = [name[len(prefix) :] if name.startswith(prefix) else name for name in raw_members]
    members = [name for name in members if name]
    roots = {name.split("/")[0] for name in members}
    outside = sorted(roots - SDIST_ALLOWLIST)
    if outside:
        return f"non-allowlisted top-level entries: {outside}"
    leaks = [name for name in members if name.split("/")[0] in FORBIDDEN]
    if leaks:
        return f"private members: {leaks[:5]}"
    missing = [name for name in EXPECTED_SDIST if name not in members]
    if missing:
        return f"missing expected product files: {missing}"
    return None


prefix = sdists[0].name[: -len(".tar.gz")] + "/"
control_leak = validate_sdist([prefix + "private/process/secret.md", prefix + "src/lecture_localizer/cli.py"])
control_missing = validate_sdist([prefix + "pyproject.toml"])
control_ok = validate_sdist(
    [prefix + name for name in SDIST_ALLOWLIST if name != "PKG-INFO"]
    + [prefix + name for name in EXPECTED_SDIST]
)
if control_leak is None or "private" not in control_leak:
    raise SystemExit("privacy gate failed its leak control")
if control_missing is None or "missing expected product files" not in control_missing:
    raise SystemExit("privacy gate failed its completeness control")
if control_ok is not None:
    raise SystemExit(f"privacy gate failed its allowlist control: {control_ok}")

with tarfile.open(sdists[0]) as archive:
    members = archive.getnames()
finding = validate_sdist(members)
if finding is not None:
    raise SystemExit(f"sdist rejected: {finding}")

with zipfile.ZipFile(wheels[0]) as archive:
    wheel_members = archive.namelist()
wheel_ok = all(
    name.startswith("lecture_localizer/") or name.startswith("lecture_localizer-")
    for name in wheel_members
)
if not wheel_ok or "lecture_localizer/cli.py" not in wheel_members:
    raise SystemExit(f"wheel has unexpected members: {wheel_members}")

print(
    f"privacy gate OK (leak and completeness controls passed): "
    f"sdist {len(members)} members, wheel {len(wheel_members)} members"
)
EOF

stage "wheel install smoke"
SMOKE=$(mktemp -d)
trap 'rm -rf "$SMOKE"' EXIT
"$PY" -m venv "$SMOKE/venv"
"$SMOKE/venv/bin/pip" install --quiet --disable-pip-version-check "$ROOT"/dist/lecture_localizer-*.whl
"$SMOKE/venv/bin/lecture-localizer" --help > /dev/null
set +e
"$SMOKE/venv/bin/lecture-localizer" doctor --json > "$SMOKE/doctor.json"
DOCTOR_EXIT=$?
set -e
if [ "$DOCTOR_EXIT" != "0" ] && [ "$DOCTOR_EXIT" != "7" ]; then
    echo "doctor smoke exited with unexpected code $DOCTOR_EXIT" >&2
    exit 1
fi
"$SMOKE/venv/bin/python" - "$SMOKE/doctor.json" <<'EOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
if report.get("command") != "doctor" or "ok" not in report or "tools" not in report:
    raise SystemExit("doctor JSON report has unexpected structure")
print(f"doctor smoke JSON valid, ok={report['ok']}")
EOF

printf '\nqc-python-cli: PASS\n'
