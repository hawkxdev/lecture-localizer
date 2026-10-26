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

## Implemented capabilities (stage 1)

| Capability | Owner |
|---|---|
| `lecture-localizer doctor [--json]` | `src/lecture_localizer/cli.py` |
| Validated nonsecret settings | `src/lecture_localizer/settings.py` |
| Tool discovery, capability probing, scrubbed subprocesses | `src/lecture_localizer/media.py` |
| Local quality runner with artifact privacy gate | `scripts/qc-python-cli.sh` |
| Deterministic synthetic fixtures | `scripts/make-synthetic-example.py` |

Doctor verifies the real features the media profile needs: FFmpeg H.264 decoding, AAC and mov_text encoding, the atempo filter, and MP4Box track flag rewriting on a synthetic input inside a temporary directory. Missing or unusable tools exit 7, invalid settings exit 3, usage errors exit 2. Settings load from `LECTURE_LOCALIZER_SETTINGS` and reject unknown fields. Media subprocess environments are scrubbed of credential variables. The sdist ships an explicit public allowlist; the quality gate rejects any archive member outside it.

## Proposed artifact model

These terms describe the current proposal, not implemented formats or approved interfaces:

| Artifact | Meaning |
|---|---|
| Original media | The source video and audio retained independently of localized output |
| Source subtitles | Timed source-language text used as an input to the workflow |
| Full translation | The reviewed translation of the complete source meaning |
| Narration text | A separate speech-oriented rendition; editing it must not silently replace the full translation |
| Verification report | Evidence about the produced media; container and timing checks alone do not establish translation quality |

Read the approved scenario linked in the knowledge index for resumption requirements, acceptance criteria and open technical choices.

## Development of this guide

Add actual pipeline stages, module paths, artifact formats, media invariants, verified commands and observed failure cases as implementation develops. Keep requirements and architecture decisions at their linked owners.

## Harness enhancements

Use native skill loading or read this skill and its references directly.
