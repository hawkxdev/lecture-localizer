# Feature 002 tasks: multilingual localization foundation

## Feature state

**Scope owner:** [spec.md](spec.md), M1-M8 and AC1-AC8, derived from the owner-approved [HLD](plan.md).
**Phase:** preparing.
**Evidence:** HLD content/destination approved; English specification and task preparation assigned. F002-S1.2 is published as [issue #22](https://github.com/hawkxdev/lecture-localizer/issues/22). Every task has a participant/decomposition assessment; a small existing-SRT test portion of S4.3 is published as [issue #24](https://github.com/hawkxdev/lecture-localizer/issues/24). External [PR #25](https://github.com/hawkxdev/lecture-localizer/pull/25), head `16246a5ebc2cd39b3b4ab3f954160fa6259cf76b`, submits that unit portion; maintainer verification and acceptance are pending. Core implementation is unassigned and no provider experiment is complete.
**Blockers:** implementation ownership/execution frame is not assigned. A paid new-pair packet and its human acceptance require later explicit authority; they do not block independent contract preparation.
**Next action:** review PR #25 against #24 at its actual head, then obtain the owner's specific merge decision after technical acceptance. Assign core implementation of #22 separately. Reassess deferred small outcomes when their actual prerequisites exist. Separate [issue #23](https://github.com/hawkxdev/lecture-localizer/issues/23) remains enhancement/help-wanted without a newcomer label. Spending and release retain their separate gates.
**Delivery:** feature documents are prepared for the documentation delivery route. Issues #22 and #24 are published and PR #25 is submitted; no contribution merge or product release is accepted by preparation.

This file is the sole feature-phase and task-assignment owner until an authorized platform binding replaces individual local task records. Once a task is published, link its actual issue and use that issue for execution status instead of mirroring an independently editable backlog here. Never invent issue identifiers or assignees.

## Stage dependencies and execution rules

Stages 1 -> 2 -> 3 -> 4 -> 5. Preserve the approved module radius and outcome of each [HLD stage](plan.md#stages). Shared jobs.py, cli.py and fixtures need explicit ownership before concurrent editing. Tasks do not select agents, models or an execution platform. Run checks locally; do not launch GitHub Actions.

Task acceptance requires the listed outcome and evidence on an exact revision. An author run is self-verification; required result review remains distinct. No task checkbox grants paid or publication authority.

## Stage 1: language model and contracts

| ID | Work | Acceptance and evidence | Requirements | State |
|---|---|---|---|---|
| F002-S1.1 | Record feature scope, sole phase owner and task traceability | This specification/task record link M1-M8, AC1-AC8, the approved HLD and immutable feature 001 baseline; no implementation state is claimed | M1-M8 | Documentation preparation complete |
| F002-S1.2 | Implement tag/profile normalization and representations in localization.py, including its enabling dependency/notice/build-gate subset | Case, region/script, special/private/invalid codes, mappings and unsupported combinations have distinguishing positive/negative checks; syntax is separate from support | M1-M2, M6 | [Issue #22](https://github.com/hawkxdev/lecture-localizer/issues/22), prepared; no executor assigned |
| F002-S1.3 | Add strict translation/job v2 and retain v1 readers | Both supported families validate; duplicate keys/unknown fields and actually unknown versions fail; v1-only language behavior remains tested | M1, M3 | Unassigned |
| F002-S1.4 | Package capabilities/settings and complete language-resource distribution | Catalog and strict nonsecret voice settings are resolved without network; verify the dependency/notice introduced with S1.2 and actual archive/installed resources through the existing gate | M1-M2, M8 | Unassigned |
| F002-S1.5 | Verify foundation regressions and installation | Existing v1 cases, dialect/code/voice contrasts and wheel resources pass; record exact interpreter/result | M1-M3, M8 | Unassigned |

## Stage 2: compatible preparation and state

| ID | Work | Acceptance and evidence | Requirements | State |
|---|---|---|---|---|
| F002-S2.1 | Persist v2 localization/profile and immutable inputs | prepare writes explicit source/target and frozen speech data; snapshots preserve input bytes and state validates against job-v2 | M1, M3 | Unassigned |
| F002-S2.2 | Preserve v1 preparation and reject implicit migration | Old unchanged prepare is idempotent; different family/profile is refused without rewriting/moving/deleting user data | M3, M5 | Unassigned |
| F002-S2.3 | Apply profile-aware selective invalidation | M4 cases vary independently, retaining unaffected speech, prior revisions, outputs and uncertain attempts | M4-M5 | Unassigned |
| F002-S2.4 | Expose capabilities/profile and versioned status | Read-only output shows the effective profile and evidence limits without key/network; preserve v1 field compatibility | M1-M3, M8 | Unassigned |
| F002-S2.5 | Check preparation invariants and partial-delivery guards | Identical text/different language or voice, equivalent tag case and unchanged v1 receipts are distinguished; v2 run/assemble/recover CLI paths refuse until stage 4 | M3-M5, M8 | Unassigned |

## Stage 3: speech profile, journal and recovery

| ID | Work | Acceptance and evidence | Requirements | State |
|---|---|---|---|---|
| F002-S3.1 | Unify effective payload and request-signature consumers | Actual fake-transport request and signature derive from the same frozen profile; exact legacy payload/signatures remain unchanged | M1, M3-M5 | Unassigned |
| F002-S3.2 | Bind attempts, journal reconciliation, reuse and retry | Changes do not resolve/relabel uncertain attempts; cache reuse requires matching profile/provenance/bytes | M4-M5 | Unassigned |
| F002-S3.3 | Preserve explicit offline response recovery | Completed-WAV validation and recovery receipts use the particular job profile; original failed outcomes and old receipt bytes remain intact | M3, M5 | Unassigned |
| F002-S3.4 | Verify admission and offline isolation | Unsupported new work causes zero key reads/HTTP; complete cache causes zero requests; valid offline assembly/recovery needs neither transport nor current provider catalog access | M2, M5 | Unassigned |
| F002-S3.5 | Exercise speech failures through fake transport | Voice/language changes, corruption, cancellation and unknown outcomes have distinguishing checks; no incomplete v2 container is published | M4-M5, M7-M8 | Unassigned |

## Stage 4: multilingual MP4 and full integration

| ID | Work | Acceptance and evidence | Requirements | State |
|---|---|---|---|---|
| F002-S4.1 | Pass one profile to mux and verifier expectations | Both consume the same job record and neutral source/target roles, including explicitly selected source audio | M1, M6 | Unassigned |
| F002-S4.2 | Persist tags, full readable labels, track IDs and defaults | Inspect the actual file after FFmpeg/MP4Box; three-letter tags and full region/script labels agree with expectations | M3, M6 | Unassigned |
| F002-S4.3 | Preserve complete Unicode text and narration separation | Both extracted subtitle languages/texts/times match; no language-specific substitution of full translation by narration occurs | M6-M7 | Existing-SRT unit-regression portion: [issue #24](https://github.com/hawkxdev/lecture-localizer/issues/24), [PR #25](https://github.com/hawkxdev/lecture-localizer/pull/25) submitted, acceptance pending; remaining multilingual integration unassigned |
| F002-S4.4 | Enable complete v2 CLI integration | run/assemble/status/recover retain exit semantics and publish only a verified candidate with provenance and no-clobber behavior | M3-M8 | Unassigned |
| F002-S4.5 | Verify language matrix and container-only rebuilds | Cover reference EN -> RU, non-English source/non-Russian target, equal-language roles, regional tags and wrong persisted tags; container-only changes cause no new speech | M4, M6-M7 | Unassigned |

## Stage 5: verification, reproducibility and acceptance

| ID | Work | Acceptance and evidence | Requirements | State |
|---|---|---|---|---|
| F002-S5.1 | Run full local quality in isolated interpreter environments | Two copies of the exact result have separate Python 3.12/development .venv and recorded sys.version/QC results; main user environment is untouched | M3-M8, AC7 | Unassigned |
| F002-S5.2 | Verify public examples, rights and distribution | Old example remains valid; new technical examples, notices and all actual sdist/wheel resources reproduce without private configuration | M8, AC7 | Unassigned |
| F002-S5.3 | Prepare the exact bounded new-pair speech packet | Rights-cleared source/SRT/reviewed text, pair/voice, receiver, request count, cost ceiling, timing and recovery are reviewable before separate spending/key approval | M5, M7-M8, AC8 | Unassigned |
| F002-S5.4 | Execute only the approved packet and obtain human evidence | Exact output has required meaning, listening, endings and reference-player evidence, or each unmet criterion stays explicit; no automatic media opening | M6-M8, AC8 | Unassigned |
| F002-S5.5 | Reconcile documentation, review and feature state | README/contribution/instructions/guide match implemented commands and evidence; assess result review and retain real publication/release gates; no universal-quality claim | M1-M8 | Unassigned |

## Acceptance evidence

| Criterion | Current evidence | Remaining result |
|---|---|---|
| AC1-AC6 | Existing-code inspection and planning probes only | Implementation and exact-result technical checks |
| AC7 | Existing feature 001 package procedure and public example | Full new-result checks in both isolated interpreter environments |
| AC8 | No new multilingual speech run | Separately approved packet, exact result and competent human assessment |

The previous planning reviews assess the HLD, not the future implementation or the new language matrix. No implementation task is considered accepted from those reviews.
