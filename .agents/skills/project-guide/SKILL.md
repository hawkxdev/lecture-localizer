---
name: project-guide
description: >
  WHEN: explain lecture-localizer media artifacts, design terms and project documents,
  изучи/изучай lecture-localizer, разбери/разбирай артефакты локализации и устройство проекта.
  WHEN NOT: approve product requirements or implement an unassigned media workflow;
  use AGENTS.md and the explicitly assigned task for those boundaries.
allowed-tools: [Read, Grep, Glob, Bash]
---

# Project Guide

## Role

Own lecture-localizer's media vocabulary and implementation knowledge. `README.md` introduces the project; `AGENTS.md` owns working instructions. Decision sources are mapped in [references/kb-index.md](references/kb-index.md).

## Degrees of Freedom: MEDIUM

## Implemented capabilities (stage 2 through 5)

The integrated pipeline of stages 2 to 5 is delivered; stage 6 spoken example, listening and player acceptance remain open.

| Capability | Owner |
|---|---|
| `lecture-localizer doctor [--json]` | `src/lecture_localizer/cli.py` |
| `lecture-localizer prepare` with full input validation | `src/lecture_localizer/contracts.py`, `subtitles.py`, `jobs.py` |
| `lecture-localizer status [--json]` read only report | `src/lecture_localizer/jobs.py`, `cli.py` |
| `lecture-localizer run` guarded pipeline with publication | `src/lecture_localizer/jobs.py`, `cli.py` |
| `lecture-localizer assemble` offline assembly and publication | `src/lecture_localizer/jobs.py`, `cli.py` |
| Guarded synchronous speech adapter and operations journal | `src/lecture_localizer/xai_client.py`, `jobs.py` |
| PCM normalization, fitting, streaming timeline, AAC tracks | `src/lecture_localizer/audio.py` |
| Five stream MP4 candidate with persisted flags | `src/lecture_localizer/mux.py` |
| Persisted candidate verification with named checks | `src/lecture_localizer/verification.py` |
| Validated nonsecret settings incl. job policy | `src/lecture_localizer/settings.py` |
| Tool discovery, capability probing, scrubbed subprocesses | `src/lecture_localizer/media.py` |
| Atomic writes, immutable snapshots, one writer locks | `src/lecture_localizer/storage.py` |
| Packaged Draft 2020 12 translation and job schemas | `src/lecture_localizer/schemas/` |
| Local quality runner with artifact privacy gate | `scripts/qc-python-cli.sh` |
| Deterministic synthetic fixtures | `scripts/make-synthetic-example.py` |

Prepare verifies video and SRT hashes against the translation binding, parses cues by ordinal position, derives nonoverlapping windows, validates the media profile (H.264 yuv420p SDR, AAC LC or convertible mono/stereo audio) and requires explicit audio selection for multi track inputs. A refreshed translation with the new source hashes is an explicit rebind: affected window, fitting, subtitle and container identities are recomputed from the changed inputs while old revisions and artifacts stay in place. Cache eligibility requires current matching records with verified bytes for every required unit; fitted audio never stands in for missing raw provenance, and the distribution gate rejects runtime data under the package root. Stage signatures separate synthesis, fitting, subtitle and container work so reuse decisions follow the approved dependency table: changed narration invalidates affected synthesis and downstream work, translation only changes preserve speech, fitting changes reuse raw speech, startup default changes reuse prepared tracks, and changed video or SRT requires a rebound translation. Corrupt cache bytes fail prepare with exit 3 while status stays readable and reports blockers. Status is read only, exits 0 for a readable job and reports blocked conditions explicitly. Exit codes: 0 ok, 2 usage, 3 contract and input violations, 4 paid execution denied before any request, 5 provider or transport failure including unresolved operations, 6 timing refusal or failed verification, 7 tool or filesystem failures, 130 interruption.

Run executes the whole pipeline under one job lock: guarded speech (zero calls and no key reads on a complete cache), normalization, per segment fitting without trimming, streaming timeline, narration and original AAC preparation, Russian subtitles, candidate assembly on the destination filesystem, named verification of the saved candidate, immutable registration and no clobber publication. New job artifact kinds beyond the Stage 2 five are `timeline_audio`, `narration_audio` and `original_audio` in the job manifest schema; the manifest stays schema_version 1 and unknown kinds stay rejected. Publication keeps a durable receipt at `evidence/publication.json` recording the exact destination, the device, inode and size of the file the job actually linked and its SHA 256: an existing output is recognized as this job's own result only through that full chain together with the current registered candidate, while any other preexisting file, including a byte identical foreign copy or a file edited in place, is preserved and reported as a collision. `assemble` completes the remaining local work from retained artifacts entirely offline: it may normalize, fit and encode from retained raw responses and rebuild the container, performs no provider access and reads no credentials, and refuses jobs whose required retained artifacts are missing or corrupt. Every run or assemble writes a report under `evidence/` separating automated evidence from pending human checks: listening acceptance, QuickTime Player and IINA startup and translation meaning remain manual and ungranted.

Doctor verifies the real features the media profile needs: FFmpeg H.264 decoding, AAC and mov_text encoding, the atempo filter, and MP4Box track flag rewriting on a synthetic input inside a temporary directory. Missing or unusable tools exit 7, invalid settings exit 3, usage errors exit 2. Settings load from `LECTURE_LOCALIZER_SETTINGS` and reject unknown fields. Media subprocess environments are scrubbed of credential variables. The sdist ships an explicit public allowlist; the quality gate rejects any archive member outside it.

## Artifact model

The job artifacts below are implemented; the artifact table lists the actual persisted job state.

| Artifact | Meaning |
|---|---|
| Job manifest | Versioned `manifest.json` in the job directory; unknown versions are rejected, never migrated |
| Text snapshots | Immutable per revision copies of the full translation, the resolved narration text and the source cue text and times, each recorded with a SHA 256 digest |
| Stage signatures | Separate synthesis, fitting, subtitle and container signatures that drive selective reuse |
| Cached artifacts | Hash pinned files: raw provider responses, per segment normalized and placed PCM, the assembled timeline, prepared narration and original AAC tracks, Russian subtitles and the verified candidate MP4; missing or corrupt bytes are reported as blocked, never reconstructed from processed audio |
| Publication receipt | Durable `evidence/publication.json` with the device, inode and size identity of the linked output; byte equality alone never proves ownership of an output file |
| Run reports | Per invocation `evidence/run-*.json` with speech summary, fit evidence, verification checks, publication state, immutable text output hashes and separate pending human checks |
| Original media | The source video and audio retained independently of localized output |
| Verification report | Evidence about the produced media; container and timing checks alone do not establish translation quality |

Read the approved scenario linked in the knowledge index for resumption requirements, acceptance criteria and open technical choices.

## Development of this guide

Add actual pipeline stages, module paths, artifact formats, media invariants, verified commands and observed failure cases as implementation develops. Keep requirements and architecture decisions at their linked owners.

## Harness enhancements

Use native skill loading or read this skill and its references directly.
