# 002: multilingual localization foundation

## Scope and authority

This specification derives from the owner's request for arbitrary source and target languages and the approved [HLD](plan.md). The owner assigned preparation of this specification and its tasks, then issue preparation/publication for the bounded next task. The [task record](tasks.md#feature-state) binds the published issue and owns the feature phase; this file owns requirements and acceptance criteria. This specification does not itself assign implementation, paid calls, credential access or release.

Feature 001 remains the accepted EN -> RU reference scenario at [its specification](../001-lecture-localization/spec.md#preparation-state). Its acceptance is not expanded retrospectively to other languages. This feature generalizes the existing pipeline before a separately scoped text-preparation feature.

## User outcome

A learner or another user of the open-source CLI supplies a local video, matching source subtitles and a complete reviewed translation. They declare the source and target languages and choose an available narration voice. The application preserves their inputs and produces the existing selectable-track output with correct language identity, provenance and recovery behavior.

The architecture supports language pairs as data rather than pair-specific code. Actual availability remains constrained by the selected mode, provider and container representation. Documented availability, local technical verification and human language assessment are separate claims.

## Inputs and outputs

Inputs retain the [approved media profile](../001-lecture-localization/spec.md#input-compatibility-and-conversion), source SRT coverage/timing, explicit audio-stream selection where necessary, reviewed complete translation and optional separate narration. This feature does not generate either source text or translations.

Outputs retain the [five-stream MP4 profile](../001-lecture-localization/spec.md#output-profile-and-reference-players): one copied video, original audio, target narration, source subtitles and target subtitles. Preserve complete translation/narration outputs and an explicit verification report. Roles remain distinct when source and target language tags are equal.

## Requirements

### M1: explicit languages and one effective profile

Translation v2 declares source_language and target_language as complete language tags. TranslationDocument exposes them explicitly. New job v2 persists a localization record containing both normalized tags and the effective speech profile. Original input spelling/bytes remain in the immutable translation snapshot.

Voice is a prepare-time, nonsecret setting resolved through the packaged capability catalog; Eve remains the compatible default. The effective speech profile freezes provider xAI, target language, voice, speed 1.0, WAV at 48 kHz and with_timestamps=false. No run-time argument silently changes that profile.

Repeated prepare without an explicit profile override retains the saved v2 profile. An explicit override creates a revision and follows [M4](#m4-selective-invalidation). A current global default or catalog-description edit is not an implicit override.

### M2: validation and mode-specific capabilities

Normalize/validate language tags through the pinned language implementation identified by the HLD. Product policy separately rejects undefined/special/private-use values and unsupported extensions; syntactic validity or a three-letter conversion alone does not establish usable support. Equivalent tag case resolves consistently. Preserve the distinction between a regional/script tag and its base language.

For reviewed-input localization, check source/target declarations, container representation and the target xAI language/voice. Do not require an ASR model or a translation model. Do not silently substitute a dialect, voice, provider or language. Reject an unsupported combination with the existing contract-error exit before reading a provider key or sending HTTP.

Provide a read-only capability view, including JSON, and expose the selected profile in status. It uses packaged data and needs no provider request or key. Show documented capability separately from actual technical/human evidence. API admission of an identifier is not a quality verdict.

Offline assemble/recover with valid retained artifacts does not need current provider availability, account access or a credential reader. Updating a capability catalog does not invalidate otherwise matching retained speech.

### M3: versioned compatibility without implicit migration

Add translation-v2 and job-v2 while retaining strict v1 readers and unknown-version refusal. Preserve the exact legacy EN -> RU/Eve payload, signature algorithms, snapshots, report fields, paths and existing v1 behavior.

status/run/assemble/recover never upgrade or rewrite v1 into v2. Existing prepare with v1 and no new options remains idempotent. A v2 document or different language/speech profile cannot be applied to a v1 job directory; report the incompatibility and recommend a fresh job directory without moving/deleting data or asking the user to edit a manifest.

New v2 jobs own their history. Automatic cache transfer between directories and a migration tool are outside this feature. Unknown-version tests must use a version actually unsupported after v2 is introduced. The v1-only language restriction remains tested for v1.

### M4: selective invalidation

Use the actual effective payload as the source of request_signature; derive synthesis/fitting/subtitle/container identity from the same job profile and relevant inputs. Preserve the legacy path independently.

| Change in a v2 job | Required effect |
|---|---|
| Target language or voice | Invalidate affected speech and dependent fitting/assembly; a target-language change also updates target subtitle identity |
| Complete translation with unchanged separate narration | Rebuild subtitles/container without new TTS |
| Source tag only with identical texts, windows and target narration | Update declaration/container identity without new TTS |
| Equivalent normalized tag case | No new derived work; retain exact input snapshot |
| Startup flags or readable labels only | Rebuild container without new speech |
| Capability documentation only, effective parameters unchanged | No automatic resynthesis; new paid calls still validate current support |

Changing a revision never resolves an uncertain paid attempt, resets its history or authorizes a retry. Reuse requires matching provenance and intact bytes, not a segment ID or file existence alone.

### M5: request, journal and recovery provenance

Pass one immutable effective profile to every consumer of the actual payload, request/synthesis signatures, cache checks, journal reconciliation and recovery. A separate mutable global settings object must not independently determine new request parameters.

Retain operations-v1/recovery-v1 while their shape and meaning remain unchanged. Their versions are independent of the job schema. Preserve original HTTP history and exact failed-attempt bytes; local recovery records separate validation rather than fabricating a new request or changing the old outcome.

Preserve the existing spending guards, full-response retention, completed-WAV compatibility checks, cancellation handling and explicit uncertain-operation retry rule. A complete valid cache produces zero provider calls and no key read. Offline paths cannot acquire transport or credential access.

### M6: language representation and neutral stream roles

The full normalized BCP 47 tag is the language identity of the job, provider request and report. A verified three-letter container code is a separate representation. Preserve region/script in readable track names and the report; unknown mappings do not silently become und. Do not truncate tag strings to implement language conversion.

MuxRequest and CandidateExpectation receive source/target roles and the same job profile. Verify actual saved audio/subtitle tags, readable names, track IDs and startup flags after both media tools. Preserve explicit source-audio selection, original packet checks, exact subtitle content/timing, full decoding and no-clobber publication.

Version v2 report fields explicitly and use neutral names. Existing v1 english_text/russian_text/russian_subtitles fields and filenames remain compatible. Do not rename old receipts/artifacts automatically.

### M7: quality and evidence boundaries

Preserve complete reviewed translation independently from optional narration. Preserve [timing refusal without trimming](../001-lecture-localization/spec.md#speech-timing-policy), source/output integrity and one-writer behavior.

Technical verification covers Unicode text round-trip, metadata, duration and failure controls. It does not establish glyph rendering, text direction, translation meaning, pronunciation or unclipped speech. Human checks are tied to exact result versions and samples. Retain unverified languages/platforms explicitly instead of inferring universal quality from one successful pair.

### M8: portability and staged delivery

Public installation, examples and package resources require no private instructions, personal paths or restricted course assets. Retain notices for the pinned dependency and any example material. Check actual sdist/wheel contents and installed resources through the existing local quality procedure.

During partial implementation, prepare/status may expose their completed contracts. New v2 run/assemble/recover CLI paths refuse before spending until full stage 4 integration. Internal stage 3 checks do not advertise a complete user pipeline. Preserve v1 operation throughout.

## Acceptance criteria

| ID | Required observation |
|---|---|
| AC1 | A v2 input preserves the declared source/target tags and resolved voice/profile through prepare, saved state, status, actual fake-transport payload, signatures, reports and verifier expectations |
| AC2 | Unsupported tags, mappings, dialects and voices fail before key/HTTP; capability/status reads require neither; valid source/target roles also remain distinct for equal tags |
| AC3 | Representative saved v1 jobs retain exact payload/signatures, usable cache/recovery provenance, readable historical reports and unchanged sources; a complete cache causes zero new calls/key reads |
| AC4 | Independent language/voice/text/default changes follow M4; revision changes preserve uncertain-attempt history and require the established explicit retry decision |
| AC5 | Five-stream v2 outputs persist expected language tags, readable full tags and flags after MP4Box; exact source/target Unicode/text/timing and source packets are checked, and a deliberately wrong persisted tag fails |
| AC6 | Complete translation remains separate from narration; timing overflow, corruption, collision and interruption preserve inputs and fail visibly without trimming or false publication |
| AC7 | Full local quality passes in separate exact-result Python 3.12/development environments, including package resources/notices and clean installation; public examples reproduce without private context |
| AC8 | A separately authorized bounded new-pair sample receives the required meaning/listening/player assessment; unverified pairs remain explicit. Without this evidence, feature acceptance stays incomplete |

AC1-AC7 technical checks do not replace AC8. Technical acceptance, product acceptance, merge/publication and release retain separate authority. The task record links evidence for each criterion on the actual result revision.

## Non-goals and later work

URL/platform access, ASR, translation generation, automatic text shortening/acceptance, provider switching/fallback, voice cloning and automatic migration are outside this feature. Subsequent text preparation consumes the new contract. This boundary does not permanently prohibit those product directions.

## Planning evidence

The [HLD probes](plan.md#probes-supporting-the-decisions) establish current schema/signature constraints, a pinned language library and specific FFmpeg/MP4Box metadata/Unicode behavior. They do not verify the future implementation, current provider access or language quality. The [task record](tasks.md) owns pending execution and acceptance evidence.
