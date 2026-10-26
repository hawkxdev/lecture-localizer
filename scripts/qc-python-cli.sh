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
    "src/lecture_localizer/contracts.py",
    "src/lecture_localizer/jobs.py",
    "src/lecture_localizer/media.py",
    "src/lecture_localizer/settings.py",
    "src/lecture_localizer/storage.py",
    "src/lecture_localizer/subtitles.py",
    "src/lecture_localizer/schemas/translation-v1.schema.json",
    "src/lecture_localizer/schemas/job-v1.schema.json",
    "src/lecture_localizer/__init__.py",
    "tests/test_media.py",
    "tests/test_contracts.py",
    "tests/test_subtitles.py",
    "tests/test_storage.py",
    "tests/test_jobs.py",
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
    runtime = runtime_data_members(
        [name for name in members if name.startswith("src/lecture_localizer/")],
        "src/lecture_localizer/",
    )
    if runtime:
        return f"runtime or user data inside the package source: {runtime[:5]}"
    return None


def runtime_data_members(
    package_members: list[str], package_prefix: str
) -> list[str]:
    """Return package members that are not product code or schemas.

    Product content under the package root is limited to Python
    modules and the packaged JSON schemas; manifest, snapshot,
    artifact or lock files are job runtime data that must never ship,
    even when they sit under an allowed product root.
    """
    offenders = []
    for name in package_members:
        relative = name[len(package_prefix) :]
        if not relative or relative.endswith("/"):
            continue
        if relative.endswith(".py") or relative.startswith("schemas/"):
            continue
        offenders.append(name)
    return offenders


prefix = sdists[0].name[: -len(".tar.gz")] + "/"
control_leak = validate_sdist([prefix + "private/process/secret.md", prefix + "src/lecture_localizer/cli.py"])
control_missing = validate_sdist([prefix + "pyproject.toml"])
control_runtime = validate_sdist(
    [
        prefix + "src/lecture_localizer/manifest.json",
        prefix + "src/lecture_localizer/snapshots/translation-r001.json",
    ]
    + [prefix + name for name in EXPECTED_SDIST]
)
control_ok = validate_sdist(
    [prefix + name for name in SDIST_ALLOWLIST if name != "PKG-INFO"]
    + [prefix + name for name in EXPECTED_SDIST]
)
if control_leak is None or "private" not in control_leak:
    raise SystemExit("privacy gate failed its leak control")
if control_missing is None or "missing expected product files" not in control_missing:
    raise SystemExit("privacy gate failed its completeness control")
if control_runtime is None or "runtime or user data" not in control_runtime:
    raise SystemExit("privacy gate failed its runtime data control")
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
SCHEMA_MEMBERS = (
    "lecture_localizer/schemas/translation-v1.schema.json",
    "lecture_localizer/schemas/job-v1.schema.json",
)
missing_schemas = [name for name in SCHEMA_MEMBERS if name not in wheel_members]
wheel_runtime = runtime_data_members(
    [
        name
        for name in wheel_members
        if name.startswith("lecture_localizer/")
        and not name.startswith("lecture_localizer-")
    ],
    "lecture_localizer/",
)
if not wheel_ok or "lecture_localizer/cli.py" not in wheel_members:
    raise SystemExit(f"wheel has unexpected members: {wheel_members}")
if missing_schemas:
    raise SystemExit(f"wheel misses packaged schemas: {missing_schemas}")
if wheel_runtime:
    raise SystemExit(f"wheel carries runtime or user data: {wheel_runtime[:5]}")

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

stage "installed schema resources"
"$SMOKE/venv/bin/python" - <<'EOF'
"""Load both packaged schemas from the installed wheel and use them."""

from importlib import resources

from jsonschema import Draft202012Validator

TRANSLATION_SAMPLE = {
    "schema_version": 1,
    "source": {
        "video_sha256": "a" * 64,
        "srt_sha256": "b" * 64,
    },
    "source_language": "en",
    "target_language": "ru",
    "segments": [
        {
            "id": "s001",
            "source_cues": [1],
            "translation": "Проверка.",
        }
    ],
}


def load(name: str) -> dict:
    text = (
        resources.files("lecture_localizer")
        / "schemas"
        / name
    ).read_text(encoding="utf-8")
    import json

    return json.loads(text)


for name in ("translation-v1.schema.json", "job-v1.schema.json"):
    schema = load(name)
    Draft202012Validator.check_schema(schema)

validator = Draft202012Validator(load("translation-v1.schema.json"))
errors = list(validator.iter_errors(TRANSLATION_SAMPLE))
assert not errors, errors
print("installed schema resources OK: both schemas load and validate")
EOF

printf '\nqc-python-cli: PASS\n'
