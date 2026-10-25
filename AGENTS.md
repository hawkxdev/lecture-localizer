# lecture-localizer

## Project sources

Read `README.md` for purpose and status, and `.agents/skills/project-guide/SKILL.md` for media concepts and project knowledge. The current phase is design; runtime, dependencies, media backend and provider are not selected.

Before implementation, agree the intended user, inputs, outputs, automation boundaries, stop conditions and acceptance criteria in the feature contract. Proposed behavior is not an implementation assignment.

## Structure

Use one Git root. Code belongs in `src/`, tests in `tests/`, utilities in `scripts/`, feature specifications/plans/tasks in `docs/specs/<NNN-feature>/`, architecture decisions in `docs/adr/`, and project skills in the real flat `.agents/skills/` directory. Empty roles do not travel with Git.

## Checks and boundaries

- Run checks locally; do not introduce GitHub Actions or external execution.
- Product setup, test and build commands do not exist yet. From the root, `git diff --check` checks tracked changes; `gitleaks git . --no-banner --redact` requires Gitleaks and committed history. Neither tests localization.
- The first commit requires explicit owner approval of the prepared result, including an empty seed. Initialization permission does not replace it.
- Repository creation, new remotes, publication, deployment and paid provider calls require explicit authorization.
- Preserve source media and existing outputs. Keep credentials, personal data, restricted course assets and provider responses outside Git and distribution; use original or rights-cleared synthetic fixtures. Review donor source and licensing before importing code.

## Languages

Write the primary README and agent instructions in English; skill discovery triggers use Russian and English. Other human-facing project notes use Russian unless requested otherwise. Ask before creating `README.ru.md` and reuse the current task's answer.
