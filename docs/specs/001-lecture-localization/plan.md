# HLD: Localize one lecture with a reviewed translation

**Plan decision:** Approved design and stage sequence; implementation requires a separately assigned task.

**Requirements:** [spec.md](spec.md), approved baseline SHA-256 `2fe9d48919372bce1a87c225cd4136e8119a0165982af108d5ffe83b8d61af57`. Requirements and feature phase remain there.

## Scope and ownership

Implement the approved local CLI on macOS using Python 3.12+, argparse, synchronous HTTPX, jsonschema Draft 2020-12, and separately installed FFmpeg/ffprobe/MP4Box. Requirements and feature state remain owned by spec.md. This document defines architecture and stage order; selected task contracts will own execution claims and result evidence.

## Architectural decisions

### 1. Validate a complete bound input before provider access

Requirements: [spec: translation-json-contract](spec.md#translation-json-contract), [spec: input-compatibility-and-conversion](spec.md#input-compatibility-and-conversion).

Strict JSON parsing before schema validation; source hashes, source-cue ordinal identity, complete ordered coverage, media profile and explicit source-audio selection are separate semantic checks. Prepare and doctor do not read the key or call the provider.

Rationale: The approved JSON and media contracts require rejection before paid synthesis. A successful schema check alone cannot prove cue coverage or recover discarded duplicate keys.

Rejected alternatives: Permissive parsing, printed SRT labels as IDs, automatic first-audio selection, implicit translation or shortening.

### 2. Persist immutable artifacts and stage-specific dependencies

Requirements: [spec: resumption-and-affected-work](spec.md#resumption-and-affected-work), [spec: outputs](spec.md#outputs).

One writer per job, a versioned manifest, atomic checkpoints, artifact hashes and separate synthesis/fitting/subtitle/container signatures. Detect stale or corrupt work on resume. A new output revision needs a fresh destination; never overwrite an existing output. Status is read-only. Freeze the full translation and resolved narration text per revision, and bind the posted text snapshot to each response and the final text outputs.

Rationale: Preserves originals and completed unchanged work while exposing interruption, disk-write and collision failures.

Rejected alternatives: Whole-job signatures that resynthesize unchanged narration; cache hits based on file existence; source retirement; deleting uncertain locks or other processes.

### 3. Isolate and guard direct speech requests

Requirements: [spec: speech-backend-and-delivery-boundary](spec.md#speech-backend-and-delivery-boundary), [spec: technical-basis](spec.md#technical-basis).

Only the selected xAI endpoint and approved WAV/48k/eve/ru/speed=1.0 mode. Persist request-start before POST, preserve the complete response before normalization, and stop uncertain outcomes for an explicit retry decision. Read XAI_API_KEY only after validated inputs and explicit limits, redact credentials and remove them from media subprocess environments. HTTP inactivity controls do not imply a hard overall duration; cancellation leaves the journaled operation visible and no automatic repeat.

Rationale: Local spending guards and replay-safe state are required; no account entitlement or live response shape has been verified.

Rejected alternatives: Automatic paid retries or provider fallback, personal Keychain/config dependencies, key-bearing process arguments, raw responses in source or distribution.

### 4. Fit and assemble complete PCM by sample positions

Requirements: [spec: speech-timing-policy](spec.md#speech-timing-policy), [spec: technical-basis](spec.md#technical-basis).

Mono signed 16-bit PCM at 48kHz; exact source windows in sample coordinates; configured speedup cap and mandatory actual-frame postcheck. Stream the full timeline as headerless PCM with explicit format/frame metadata, adding silence only. Encode the approved AAC-LC profiles after fitting.

Rationale: Synthetic probes showed an atempo=1.20 result exceeding the window by eight samples. Headerless streaming avoids requiring a RIFF-sized whole-lecture WAV or loading all audio into memory.

Rejected alternatives: Trimming quiet ends or overshoot, shifting following windows, automatic text rewriting, inheriting an external application's single-segment alignment branch.

### 5. Publish only a verified selectable-track artifact

Requirements: [spec: output-profile-and-reference-players](spec.md#output-profile-and-reference-players), [spec: startup-track-defaults](spec.md#startup-track-defaults).

Explicit one-video/two-audio/two-subtitle mapping, approved language/default flags and a temporary result in the destination filesystem. Verify persisted streams, relevant packet hashes, extracted text/times and decoding before non-overwriting publication. Report automated checks separately from unverified human listening/player acceptance.

Rationale: Mux success and container metadata do not prove translation meaning, intact speech endings or actual reference-player startup.

Rejected alternatives: Implicit stream selection, forced subtitles, global player preference changes, green aggregate as product acceptance.

## Planned files

| Path | Action | Responsibility |
|---|---|---|
| pyproject.toml | Create | Package metadata, dependency groups/pins, console entry, resources and local quality configuration. |
| scripts/qc-python-cli.sh | Create | Local Ruff format/check, mypy, selected pytest suites and build runner with failure propagation; no CI service. |
| scripts/make-synthetic-example.py | Create | Original deterministic technical media fixtures in an explicit new directory; tones are not semantic speech-acceptance evidence. |
| src/lecture_localizer/__init__.py | Create | Package surface and installed-version identity. |
| src/lecture_localizer/cli.py | Create | argparse command boundaries, diagnostics and exit codes; advertise only completed commands. |
| src/lecture_localizer/settings.py | Create | Validated nonsecret job/tool/profile/timeout/spending settings; credentials remain outside serialized settings. |
| src/lecture_localizer/media.py | Create | Tool discovery/capabilities, ffprobe media facts and checked subprocesses with scrubbed environments. |
| src/lecture_localizer/contracts.py | Create | Strict translation import and complete input binding/coverage validation. |
| src/lecture_localizer/schemas/translation-v1.schema.json | Create | Packaged structural schema of the approved translation contract. |
| src/lecture_localizer/schemas/job-v1.schema.json | Create | Versioned job-state validation; reject unknown state versions rather than migrate silently. |
| src/lecture_localizer/subtitles.py | Create | SRT ordinal parsing, group windows, complete Russian subtitle generation and source-text preservation. |
| src/lecture_localizer/storage.py | Create | Owned atomic writes, immutable artifact integrity, locking primitives and non-overwriting publication. |
| src/lecture_localizer/jobs.py | Create | Job orchestration, per-stage signatures/invalidation, operation journal and structured job reports. |
| src/lecture_localizer/xai_client.py | Create | Explicit synchronous request/response handling and inactivity timeouts; no full-call wall-clock guarantee or personal workflow imports. |
| src/lecture_localizer/audio.py | Create | PCM normalization, fitting, streaming placement and approved audio encoding. |
| src/lecture_localizer/mux.py | Create | Explicit MP4 assembly and application of track IDs/default flags. |
| src/lecture_localizer/verification.py | Create | Persisted output checks and evidence consumed by the job report. |
| tests/test_doctor.py, tests/test_contracts.py, tests/test_subtitles.py | Create | Dependency/input boundaries, malformed and valid data, coverage/order and Unicode/timing. |
| tests/test_storage.py, tests/test_jobs.py, tests/test_xai_client.py | Create | Corruption, collision, interruption, selective reuse, fake-transport failure and zero-call controls. |
| tests/test_audio.py, tests/test_mux.py, tests/test_verification.py, tests/test_cli.py | Create | One/multiple segments, frame edges, real local media tools, CLI error/success and offline end-to-end behavior. |
| README.md, CONTRIBUTING.md, AGENTS.md, .agents/skills/project-guide/ | Modify when behavior exists | Actual setup/commands, technical example, implemented invariants and check limits; keep private provenance out of public content. |

## Interfaces and command boundaries

- The [parallel implementation contract](parallel-contract.md) fixes the Stage 3/4/container handoffs, common-state owner and final integration dependencies. It refines the execution sequence without changing the approved scenario or granting implementation authority.
- CLI -> validated settings/contracts/media facts -> jobs. Jobs owns runtime execution state; storage owns filesystem durability and immutable artifact publication.
- Jobs -> xai_client only for explicit paid execution. The transport consumes an approved request and sink, not the whole manifest; no hidden provider access from doctor, prepare, assemble or status.
- Jobs -> audio -> mux -> verification. Media supplies shared tool/probe services, not provider credentials. Subtitles consume full translation independently of narration.
- Distinguish source-cue ordinals, segment IDs, input stream indices and MP4 track IDs. Distinguish full provider response, normalized PCM, fitted PCM and encoded narration artifacts.
- Planned CLI: doctor [--json]; prepare --video PATH --source-srt PATH --translation PATH --job DIR --output PATH [--source-audio INDEX] [--settings PATH]; run --job DIR [--allow-paid --limits PATH] [--retry-uncertain OPERATION_ID]; assemble --job DIR [--output FRESH_PATH]; status --job DIR [--json]. Paid flags/key access are required only if new requests are needed; complete-cache reuse needs none. Limits define numeric request/text/cost bounds and the applicable price basis before paid execution; this plan supplies no real values. Retry permission binds the explicit unresolved operation and current request signature.
- Processing failures never return success. A successful status read is not a statement that the job passed; the reported job state retains all blocked/unverified conditions. Reports include both immutable text outputs and separate automated results, pending human review and publication/delivery state.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Requested command completed; run/assemble passed their automated artifact checks. A readable status may return 0 while reporting a blocked job; it does not grant product/human acceptance. |
| 2 | CLI argument or usage error. |
| 3 | Invalid input, settings or job contract, unsupported version/profile, hash/cue mismatch or corrupt readable job data. |
| 4 | Paid execution denied before a request: absent explicit permission, required limits or key. |
| 5 | Provider/transport failure or unresolved paid-operation outcome, including an uncertain operation without the explicit retry decision. |
| 6 | Speech does not fit or persisted output verification fails. |
| 7 | Required tool unavailable, local media process failure or filesystem/I/O failure; status read I/O failure also uses 7. |
| 130 | User interruption; journal any started-but-unresolved paid operation before exit. |

## Stage overview

| Stage | Tasks | Deliverable |
|---|---|---|
| 1 | 4 | Installable package and local tool checks |
| 2 | 5 | Validated persistent jobs and selective reuse |
| 3 | 5 | Guarded direct speech adapter |
| 4 | 5 | Complete fitted narration on a streaming timeline |
| 5 | 5 | Verified MP4 and complete offline CLI flow |
| 6 | 4 | Reproducible candidate and owner acceptance |

<!-- === Stage 1: Installable package and local tool checks === -->
## Stage 1: Installable package and local tool checks

Requirements: [spec: technical-basis](spec.md#technical-basis).

Dependencies: none. Production/utility module radius: __init__.py, cli.py, settings.py, media.py, scripts/make-synthetic-example.py.

- **S1.1** Create the package, console entry and runtime/dev dependency definitions; select compatible pins and preserve license notices.
- **S1.2** Implement validated settings and local tool discovery/probe/subprocess services, including doctor capability checks and credential-free media environments.
- **S1.3** Create local quality/build configuration and runner; package/install smoke tests verify the console entry. Mark these commands as newly introduced, not existing checks.
- **S1.4** Create deterministic rights-safe technical fixtures and dependency/path diagnostics tests. No API, recording, transcription or playback; update actual command documentation.

Exit evidence: A clean local wheel/install exposes doctor with meaningful success/failure. Ruff format/check, mypy, package build and stage tests pass; no live provider dependency or placeholder success.

<!-- === Stage 2: Validated persistent jobs and selective reuse === -->
## Stage 2: Validated persistent jobs and selective reuse

Requirements: [spec: input-compatibility-and-conversion](spec.md#input-compatibility-and-conversion), [spec: translation-json-contract](spec.md#translation-json-contract), [spec: resumption-and-affected-work](spec.md#resumption-and-affected-work).

Dependencies: Stage 1. Production/utility module radius: contracts.py, subtitles.py, storage.py, jobs.py, cli.py.

- **S2.1** Package translation/job schemas; reject duplicate keys, unsupported versions, unknown fields, invalid types and blank/null text before synthesis; validate complete hashes and cue coverage.
- **S2.2** Parse ordinal source cues and derive positive, nonoverlapping group windows. Persist immutable full-translation and resolved narration-text snapshots for every job revision (`narration`, otherwise `translation`), retaining segment/cue identity. Preserve source text/times, validate the media profile and explicit original-audio selection.
- **S2.3** Implement atomic manifests, immutable artifacts, output/source alias and collision guards, one-writer locks and visible recovery for interruption/disk failures. Never delete originals or uncertain locks automatically.
- **S2.4** Define per-stage signatures and invalidation/revision rules; verify cached bytes and settings, preserve unaffected artifacts, and block missing/corrupt raw responses instead of replacing them with processed audio.
- **S2.5** Wire prepare/status with no provider/key access; test changed narration, translation-only with fixed narration, timing-only and defaults-only changes, plus paths with spaces, Unicode, apostrophes and #.

Exit evidence: Valid inputs create an inspectable job; invalid/mismatched inputs and existing outputs fail without changing source bytes. Resume decisions match the approved dependency table and offline commands make zero provider calls.

<!-- === Stage 3: Guarded direct speech adapter === -->
## Stage 3: Guarded direct speech adapter

Requirements: [spec: technical-basis](spec.md#technical-basis), [spec: speech-backend-and-delivery-boundary](spec.md#speech-backend-and-delivery-boundary).

Dependencies: delivered Stage 2 and the [parallel contract](parallel-contract.md). This lane may run alongside standalone Stage 4 and Stage 5 container work, and alone owns shared job/journal/settings/schema changes during that interval. Production/utility module radius: xai_client.py, jobs.py, storage.py, settings.py.

- **S3.1** Implement the documented direct synchronous xAI mode with verified TLS and explicit finite connect/read/write/pool timeouts. These are phase/inactivity controls, not a guaranteed maximum wall-clock duration for the entire response. No automatic retry/fallback or personal access lookup.
- **S3.2** Stream and preserve the complete response, validate response status/type and decodable WAV before committing its integrity record. Bind the exact frozen text sent, request signature and response hash; retain failures as distinct private evidence. Never reconstruct past spoken-input text from a mutable external JSON.
- **S3.3** Require validated inputs, execution preview and explicit numeric spending limits; reserve/journal the operation before POST. Keep local budget/estimate records distinct from actual provider billing.
- **S3.4** Handle interrupted/uncertain requests, partial responses, cancellation and damaged cache without blind repeat. Record observed elapsed time separately from inactivity settings. A new paid retry requires an explicit pending operation ID and refreshed limits; the original uncertain receipt is retained.
- **S3.5** Use a fake transport to test request fields, credential redaction, malformed/truncated audio, HTTP errors and timeouts, with exact positive/zero call-count controls. Do not expose a partially implemented run command as a complete pipeline.

Exit evidence: All transport and job-transition contracts pass offline tests. Cached unchanged speech causes no new request; missing approval/limits causes zero requests; uncertain attempts remain visibly unresolved. Live access is not claimed.

<!-- === Stage 4: Complete fitted narration on a streaming timeline === -->
## Stage 4: Complete fitted narration on a streaming timeline

Requirements: [spec: speech-timing-policy](spec.md#speech-timing-policy), [spec: technical-basis](spec.md#technical-basis).

Dependencies: delivered Stage 2 and the [parallel contract](parallel-contract.md); standalone audio work uses retained synthetic WAV and may run alongside Stage 3. The parallel lane owns audio.py; shared jobs/storage/media wiring waits for final integration after Stage 3. Overall production/utility module radius: audio.py, jobs.py, storage.py, media.py.

- **S4.1** Normalize retained full responses to the approved working PCM without altering their original bytes; persist exact format, hash and frame counts.
- **S4.2** Implement integer-sample windows, configured speedup checks and mandatory actual output-length checks. Preserve ordinary PCM and reject excessive or post-fit overflow without trimming.
- **S4.3** Stream single and multiple complete segments into a headerless PCM timeline with leading/intermediate/trailing silence and explicit format metadata; avoid whole-lecture memory and RIFF-size assumptions.
- **S4.4** Implement Russian mono AAC-LC 48k/192k and original-audio conversion preserving mono/stereo; compatible original AAC-LC stays copied. Preserve timing relationships rather than silently realign.
- **S4.5** Verify quiet tails, nonzero starts, the eight-sample post-fit case, one/multiple fragments, unsafe paths, ENOSPC and interruption. Use bounded native media fixtures and large-count streaming controls; no claim of real speech quality.

Exit evidence: Complete segment samples fit their windows or produce an actionable refusal. Raw responses remain intact. Streaming placement and approved encoding pass exact fixture checks and full decoding.

<!-- === Stage 5: Verified MP4 and complete offline CLI flow === -->
## Stage 5: Verified MP4 and complete offline CLI flow

Requirements: [spec: output-profile-and-reference-players](spec.md#output-profile-and-reference-players), [spec: startup-track-defaults](spec.md#startup-track-defaults), [spec: outputs](spec.md#outputs).

Dependencies: S5.1-S5.3 container core may start from delivered Stage 2 and the [parallel contract](parallel-contract.md), using synthetic prepared AAC/SRT. S5.4-S5.5 final publication/CLI integration waits for accepted Stage 3, Stage 4 and that container core. The core owns mux.py, verification.py and necessary subtitles.py work; the lead/final integration owner reconciles common modules and documentation. Overall production/utility module radius: mux.py, verification.py, jobs.py, cli.py, media.py, subtitles.py.

- **S5.1** Build explicit selected-video/original-audio/narration/EN/RU mapping and metadata. No automatic first-audio choice or source removal.
- **S5.2** Apply approved defaults through actual track IDs and inspect the persisted flags: Russian audio default, English selectable, both subtitle languages off/not forced; never change player preferences.
- **S5.3** Verify five streams, languages/codecs, video packet integrity, copied original AAC, extracted complete texts/times, timing relationships/durations and full decoding. Keep unchecked human quality separate.
- **S5.4** Publish only a verified temporary result on the destination filesystem without overwriting any existing output. Test collisions, aliases, cancellation and failed verification; preserve all originals.
- **S5.5** Wire complete run/assemble/status reporting and the explicit exit-code table. Include paths and hashes of the immutable full-translation and actual narration-text outputs in the final report. Prove offline assemble cannot reach transport and fake-provider resume does not repeat unchanged synthesis; every processing failure remains non-successful.

Exit evidence: The technical synthetic scenario produces the required selectable-track container with verified persisted properties. Existing outputs and sources remain unchanged; no raw/provider/private material enters public output or source. GUI and listening acceptance are still pending.

<!-- === Stage 6: Reproducible candidate and owner acceptance === -->
## Stage 6: Reproducible candidate and owner acceptance

Requirements: [spec: acceptance-criteria](spec.md#acceptance-criteria), [spec: scenario-boundary-and-open-choices](spec.md#scenario-boundary-and-open-choices).

Dependencies: Stage 5. Production/utility module radius: documentation, tests and package verification only.

- **S6.1** Run the full required local quality, build/install and real-tool integration gates with no hidden required skips, on minimum Python 3.12 and the development interpreter. Review locked transitive licenses/notices and the complete public package.
- **S6.2** Complete actual README/contribution/instruction/guide documentation and two clearly separated examples: deterministic tones for technical checks, and a short spoken-English input with matching SRT/reviewed translation whose original or redistribution-cleared provenance is recorded and which a clean public checkout user can obtain or reproduce without private files. Raw provider responses remain excluded. If the spoken example is unavailable, AC6 and dependent live/semantic acceptance remain unmet, not just the pilot. The lead prepares a concrete candidate and the owner decides material/rights.
- **S6.3** Prepare a concrete live-pilot packet with exact inputs/texts, receiver, request count, applicable price/cost ceiling, timing, credential boundary and recovery. Wait for explicit owner approval before key access or any paid call; no numeric budget is guessed here.
- **S6.4** After that approval, run the bounded pilot and owner-authorized listening/player acceptance on the exact result: beginning/middle/end, endings, both audio languages, both subtitle languages, subtitles off and observed startup under current preferences in QuickTime/IINA. Do not auto-open/play completed media. Return failures for fixes and retain all unmet criteria; no release/merge/deployment is inferred.

Exit evidence: Every scoped criterion has result-version evidence and the required owner decisions, or the feature remains explicitly unaccepted with exact gaps. A passing container or source test cannot close listening/player/clean-checkout criteria.

## Verification policy

Each stage runs only checks its completed code supports, maintains affected documentation/project-guide, and retains exact results and required gaps. Stage 1 creates Ruff format/check, mypy, pytest and build commands; none exist before that stage. Full offline acceptance at stages 5/6 requires actual FFmpeg/MP4Box and no required skips. Guard tests use distinct valid/invalid/boundary fixtures and demonstrable negative controls for preservation, window, cache and spending guards; do not mirror implementation. No GitHub Actions or replacement CI service.

## Probes supporting the decisions

- Before planning, the JSON example and approved input bindings were inspected; these are not application validation.
- On FFmpeg 9.0.2, eight short H.264/AAC/PCM/MP3/Opus mono/stereo design fixtures preserved video packets; AAC copies and conversions were inspected and fully decoded. This does not establish all encoding variants or player behavior.
- 48k mono PCM probes covered no speedup, 1.1, 1.2 and 1.21, one/multiple nonzero-start placements and AAC-LC encoding. A 57,600-frame input at atempo=1.20 produced 48,008 frames for a 48,000-frame window; the existing postcheck correctly refuses it without clipping.
- The same boundary fixture was independently reproduced, and a three-second headerless s16le PCM stream encoded to mono AAC-LC 48k/192k with a declared duration of 3.0 seconds and complete decoding. This is not a long-file test.
- HTTPX/jsonschema runtime compatibility, minimum Python, live response/access, actual voice intelligibility and GUI behavior remain implementation/acceptance checks; no provider credentials or paid requests were used to prepare this plan.

## Risks and gates

- Irreversible paid requests, uncertain interruption and unbounded overall latency: journal before POST, explicit limits/inactivity timeouts and cancellation, no automatic repeat, owner-approved pilot with its own timing window. Do not present inactivity timeout as total-call deadline.
- Integrity and selective resume: separate signatures/raw vs processed artifacts, one writer, corruption/disk/collision controls and no source retirement.
- Discrete timing and real speech: actual-frame checks, streaming format, one/multiple paths and quiet-tail tests; synthetic metrics cannot prove intelligibility.
- Environment, data rights and actual compatibility: minimum Python/tool capability gates, locked-license review, rights-cleared spoken input and separate QuickTime/IINA acceptance.

## Acceptance traceability

| Requirement | Planned work | Evidence |
|---|---|---|
| AC1 source/output preservation | S2.3, S5.4 | Source and existing destination hashes/inodes, alias/collision/failure controls. |
| AC2 full translation and narration separation | S2.1-S2.2, S5.3 | Immutable complete-translation and resolved posted-text snapshots, exact cue coverage, final text-output hashes and extracted full subtitle text/times; narration edits never replace translation. |
| AC3 selectable languages and subtitles off | S5.1-S5.3, S6.4 | Container stream/flag checks plus actual QuickTime/IINA manual selection/off/startup evidence. |
| AC4 intelligible fitted unclipped narration | S4.1-S4.5, S6.4 | Raw integrity/frame guards and bounded synthetic tests, then human listening on the actual voice. |
| AC5 resume and affected-input invalidation | S2.3-S2.5, S3.3-S3.5, S5.5 | Call counts, cache integrity, input changes, cancellation and uncertain-outcome recovery. |
| AC6 clean public reproducibility | S1.1-S1.4, S6.1-S6.2 | Clean wheel/install/resource/dependency/notice checks plus an obtainable or reproducible original/rights-cleared spoken example without private configuration. Tones alone do not close AC6. |

## Authority and next step

The owner approved this design and stage sequence for saving. Stage 1 is delivered through [PR 6](https://github.com/hawkxdev/lecture-localizer/pull/6); Stage 2 is delivered through the merged [issue 7](https://github.com/hawkxdev/lecture-localizer/issues/7) work; the speech, audio and container components are delivered through PRs 16, 15 and 17; the stage 5.4-5.5 integration is implemented in [PR 18](https://github.com/hawkxdev/lecture-localizer/pull/18), which owns its current technical review and merge status. Stage 6 and full-feature acceptance remain open. Paid pilot, media inspection/playback, publication, merge and deployment need their actual scoped authority. Follow [CONTRIBUTING.md](../../../CONTRIBUTING.md) and the approved feature contract. No schedule, price or owner capacity is invented; dependency order is technical rather than a calendar forecast.
