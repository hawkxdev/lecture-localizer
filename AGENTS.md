# lecture-localizer

## Project sources

Read `README.md` for purpose and status, and `.agents/skills/project-guide/SKILL.md` for media concepts and project knowledge. Feature 001 keeps its accepted phase at the [feature specification](docs/specs/001-lecture-localization/spec.md#preparation-state). Feature 002 requirements belong to [spec.md](docs/specs/002-multilingual-localization/spec.md); its phase and local task assignments belong to [tasks.md](docs/specs/002-multilingual-localization/tasks.md#feature-state), and architecture belongs to [plan.md](docs/specs/002-multilingual-localization/plan.md).

Before implementation, agree the intended user, inputs, outputs, automation boundaries, stop conditions and acceptance criteria in the feature contract. Proposed behavior is not an implementation assignment.

## Structure

Use one Git root. Code belongs in `src/`, tests in `tests/`, utilities in `scripts/`, feature specifications/plans/tasks in `docs/specs/<NNN-feature>/`, architecture decisions in `docs/adr/`, and project skills in the real flat `.agents/skills/` directory. Empty roles do not travel with Git.

## Checks and boundaries

- Run checks locally; do not introduce GitHub Actions or external execution.
- Local quality runner: `scripts/qc-python-cli.sh` from the root installs `.venv` when missing, then runs Ruff format/check, mypy, the stage tests, the package build and a wheel install smoke test, propagating failures. The doctor tests require locally installed FFmpeg, ffprobe and MP4Box; full diagnostics also use them. From the root, `git diff --check` checks tracked changes; `gitleaks git . --no-banner --redact` requires Gitleaks and committed history.
- The first commit requires explicit owner approval of the prepared result, including an empty seed. Initialization permission does not replace it.
- Repository creation, new remotes, publication, deployment and paid provider calls require explicit authorization.
- Preserve source media and existing outputs. Keep credentials, personal data, restricted course assets and provider responses outside Git and distribution; use original or rights-cleared synthetic fixtures. Review donor source and licensing before importing code.
- Job directories and localized outputs are user data and stay outside this source checkout; the distribution gate rejects runtime data under the package root. Never delete or migrate user job data automatically.

## Languages

Write all project-authored documentation in English, including specifications, plans, task files, reports, private process notes and handoffs. Conversation with the owner may remain Russian. Preserve exact quotations, original source evidence, language-dependent examples and Russian/English skill discovery triggers. Before saving a document, inspect authored prose outside those protected fragments and reject Russian prose; the current English project documents are the reference. Do not create `README.ru.md` unless the owner explicitly requests it.
