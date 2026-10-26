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

## Implemented capabilities (stage 2)

| Capability | Owner |
|---|---|
| `lecture-localizer doctor [--json]` | `src/lecture_localizer/cli.py` |
| `lecture-localizer prepare` with full input validation | `src/lecture_localizer/contracts.py`, `subtitles.py`, `jobs.py` |
| `lecture-localizer status [--json]` read only report | `src/lecture_localizer/jobs.py`, `cli.py` |
| Validated nonsecret settings incl. job policy | `src/lecture_localizer/settings.py` |
| Tool discovery, capability probing, scrubbed subprocesses | `src/lecture_localizer/media.py` |
| Atomic writes, immutable snapshots, one writer locks | `src/lecture_localizer/storage.py` |
| Packaged Draft 2020 12 translation and job schemas | `src/lecture_localizer/schemas/` |
| Local quality runner with artifact privacy gate | `scripts/qc-python-cli.sh` |
| Deterministic synthetic fixtures | `scripts/make-synthetic-example.py` |

Prepare verifies video and SRT hashes against the translation binding, parses cues by ordinal position, derives nonoverlapping windows, validates the media profile (H.264 yuv420p SDR, AAC LC or convertible mono/stereo audio) and requires explicit audio selection for multi track inputs. The job directory holds a versioned manifest, immutable per revision snapshots (full translation, resolved narration, source cues) with integrity hashes, and a lock file; stale or uncertain locks stay visible and are never removed automatically. Stage signatures separate synthesis, fitting, subtitle and container work so reuse decisions follow the approved dependency table: changed narration invalidates affected synthesis and downstream work, translation only changes preserve speech, fitting changes reuse raw speech, startup default changes reuse prepared tracks, and changed video or SRT requires a rebound translation. Status is read only, exits 0 for a readable job and reports blocked conditions explicitly. Exit codes: 0 ok, 2 usage, 3 contract and input violations, 7 tool or filesystem failures, 130 interruption.

Doctor verifies the real features the media profile needs: FFmpeg H.264 decoding, AAC and mov_text encoding, the atempo filter, and MP4Box track flag rewriting on a synthetic input inside a temporary directory. Missing or unusable tools exit 7, invalid settings exit 3, usage errors exit 2. Settings load from `LECTURE_LOCALIZER_SETTINGS` and reject unknown fields. Media subprocess environments are scrubbed of credential variables. The sdist ships an explicit public allowlist; the quality gate rejects any archive member outside it.

## Artifact model

The job artifacts below are implemented by Stage 2; later output artifacts remain proposals.

| Artifact | Meaning |
|---|---|
| Job manifest | Versioned `manifest.json` in the job directory; unknown versions are rejected, never migrated |
| Text snapshots | Immutable per revision copies of the full translation, the resolved narration text and the source cue text and times, each recorded with a SHA 256 digest |
| Stage signatures | Separate synthesis, fitting, subtitle and container signatures that drive selective reuse |
| Cached artifacts | Hash pinned files such as raw provider responses; missing or corrupt bytes are reported as blocked, never reconstructed from processed audio |
| Original media | The source video and audio retained independently of localized output |
| Verification report | Evidence about the produced media; container and timing checks alone do not establish translation quality |

Read the approved scenario linked in the knowledge index for resumption requirements, acceptance criteria and open technical choices.

## Development of this guide

Add actual pipeline stages, module paths, artifact formats, media invariants, verified commands and observed failure cases as implementation develops. Keep requirements and architecture decisions at their linked owners.

## Harness enhancements

Use native skill loading or read this skill and its references directly.
