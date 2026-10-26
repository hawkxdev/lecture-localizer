# Contributing

lecture-localizer is in early development: the installable package currently provides local tool diagnostics, while the localization pipeline itself is not implemented. Useful contributions include workflow feedback, concrete use cases, documentation corrections, and small examples using synthetic or rights-cleared media.

## Discuss a change

Open an issue describing the user problem, inputs, expected outputs, and how to judge success. Search existing issues first. Agree the scope before starting a feature or adding dependencies. Read the [feature specification](docs/specs/001-lecture-localization/spec.md) for approved choices and remaining technical decisions.

For documentation problems, link the affected file and explain the correction. Do not include credentials, personal data, restricted course material, or private provider responses in issues or pull requests. For vulnerabilities, follow [SECURITY.md](SECURITY.md).

## Submit a pull request

Keep each pull request focused. Explain the change, link its issue when applicable, and describe the checks performed and their limits. Write public documentation in English. See [AGENTS.md](AGENTS.md) for project layout and working constraints.

## Local setup and checks

The package requires Python 3.12 or later. Create an isolated environment and install the package with its development tools:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Run the local quality runner from the repository root:

```sh
scripts/qc-python-cli.sh
```

The runner creates `.venv` when it is missing and then, propagating every failure: Ruff format check, Ruff check, mypy, the stage test suite, the package build, an artifact privacy gate that rejects any archive member outside the public allowlist, and a fresh-venv wheel installation smoke test that runs the `lecture-localizer` console entry. The doctor tests exercise the real local FFmpeg, ffprobe and MP4Box, so those tools must be installed for the suite to pass; this is a setup prerequisite, not a hidden skip. Full diagnostics on macOS additionally follow [README.md](README.md).

Synthetic, rights-safe media fixtures are created only in an explicit new directory:

```sh
python scripts/make-synthetic-example.py NEW_DIRECTORY
```

For documentation-only changes, read the rendered text, check relative links, and run these whitespace checks from the repository root:

```sh
git diff --check
git diff --cached --check
```

These commands inspect unstaged and staged changes to tracked files. Run checks locally; do not add GitHub Actions workflows.

Use only material you have the right to share. Contributions to this repository are under the [MIT License](LICENSE); review compatibility and required notices before introducing third-party material.
