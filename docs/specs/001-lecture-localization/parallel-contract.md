# Parallel implementation contract

This document refines the [approved plan](plan.md) for independently testable work after Stage 2. The [specification](spec.md) remains the requirements and feature-phase owner. It changes execution dependencies and technical handoffs, not the user scenario, speech policy, media profile or acceptance authority. Preparing this contract does not assign implementation or permit a paid request.

## Deliverables and ownership

The speech, audio and container components can be implemented against this contract using original synthetic files. Complete job/CLI integration follows all three components. Each assigned task returns a focused PR and exact-version evidence; no worker is selected by this document.

| Lane | Independently acceptable result | Owned product paths | Dependencies |
|---|---|---|---|
| A: S3.1-S3.5 | Guarded synchronous speech adapter and durable operation journal, fully exercised with fake HTTP | `xai_client.py`, `jobs.py`, `storage.py`, `settings.py`, schemas, `pyproject.toml`; necessary exact resource-gate update | Delivered Stage 2 and this contract |
| B: S4.1-S4.5 | Complete PCM normalization, fitting, streaming timeline and AAC preparation from retained WAV inputs | `audio.py` | Delivered Stage 2 and this contract; synthetic WAV replaces the unfinished provider component |
| C: S5.1-S5.3 core | Five-stream MP4 candidate, persisted flags and automated container verification | `mux.py`, `verification.py`, `subtitles.py` | Delivered Stage 2 and this contract; synthetic AAC/SRT replace unfinished upstream production |
| D: S5.4-S5.5 integration | Non-overwriting publication and complete offline/fake-provider `run/assemble/status` | Shared job/storage/settings/media/schema/CLI modules after A; integration tests and actual command documentation | A, B and C accepted and integrated |

Parallel lanes may add their own test modules/helpers. Existing `tests/conftest.py`, `media.py`, CLI wiring and shared project documents have one lead/integration owner; return a concrete required change instead of editing them concurrently. Lane A alone owns common state during parallel work. It must preserve the currently used public settings/media/storage APIs and existing manifest identities so B/C can be developed from the frozen Stage 2 base. After A is integrated, D becomes the single integration writer. Changes beyond these boundaries require lead reconciliation before dependent edits.

## File handoffs

Public component calls use explicit paths, ordinary numeric/string settings and local immutable result records. B does not import the unfinished speech client; C does not import unfinished audio orchestration. Their result types may live in their respective modules. No new shared Python module must land before parallel work can begin.

| Producer -> consumer | Required handoff |
|---|---|
| Prepared job -> A | Validated source binding and media selection, immutable resolved narration-text snapshot, segment ID and current synthesis signature; explicit limits/permission only when a new request is required |
| A -> B/D | Complete retained WAV path and SHA-256, operation ID, exact posted-text snapshot/hash and request signature. Raw bytes remain immutable and separate from normalized/fitted output. Failed/uncertain responses cannot become usable artifacts. |
| Prepared job -> B | Segment ID, positive source start/end in integer milliseconds, raw WAV path, explicit fresh destination, maximum speedup and final timeline frame count |
| B -> D/C | Headerless mono signed 16-bit little-endian PCM at 48 kHz, explicit frame count/hash/format and segment fit results; encoded narration AAC-LC 48 kHz/192 kbit/s and any converted original AAC retain separate paths/integrity. No manifest write or provider access. |
| Prepared job/B -> C | Original video and explicit selected original-audio stream/disposition, prepared narration AAC, complete English SRT and full-translation Russian SRT, expected timing/track profile and a fresh staging destination |
| C -> D | Candidate MP4 path/hash, stream/track identifiers, observed flags, packet/text/decode checks and explicit unresolved checks. A candidate is not final publication, listening acceptance or player acceptance. |

Paths passed between components identify actual files. Persisted job artifact paths remain relative to the job directory; reject escapes, aliases and collisions. Reuse the existing artifact kinds/signatures rather than treating a filename as provenance. A complete response must be recorded as `raw_response`; normalized/fitted artifacts never substitute for it. Components preserve inputs and existing destinations, stream large data, clean only their own disposable partial outputs and return failures actionably.

## Speech and journal boundary

The approved transport remains synchronous HTTPX with verified TLS and finite connect/read/write/pool inactivity limits. There are no automatic retries, provider fallback, private engine imports or credential lookup beyond `XAI_API_KEY` after the guards. No partial `run` command is advertised before D.

The request maps the frozen narration text to `POST https://api.x.ai/v1/tts`, `voice_id=eve`, `language=ru`, `speed=1.0`, `output_format={codec: wav, sample_rate: 48000}`, `with_timestamps=false`. Preserve the complete response before normalization. Request/content-type/WAV validation is fake-transport tested; live access and the actual voice are unverified. Use the current [provider reference](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech) to validate request shape and preflight text limits during implementation. Reject oversized text before any paid operation; do not split, shorten or rewrite a reviewed fragment automatically. HTTP inactivity limits and the documented provider deadline do not establish a client-wide wall-clock guarantee.

Lane A owns a separate versioned `operations.json` journal inside each job. The existing strict manifest-v1 format is not silently migrated or expanded with unknown fields. Add a strict packaged journal schema and explicitly extend the exact resource allowlist, retaining rejection of arbitrary schema-directory data. Missing journal on an existing valid v1 job means no recorded operations; malformed/unknown journal versions are visible blockers. `prepare` and read-only `status` must preserve/read this journal, not discard or reset it while rebuilding a manifest.

Under the single job lock, persist operation identity, current request signature, exact posted-text snapshot/hash, limits/reservation and started state before HTTP. Commit successful raw-response integrity only after the full body is stored and validated. Cancellation, timeout, partial body, unknown provider result and failure before durable completion remain distinguishable; no blind repeat follows. An authorized retry names the uncertain operation and matching current signature, appends a linked attempt and preserves the original receipt. Interrupted or changed-input preparation cannot erase uncertainty or restore a spent local reservation. Estimates/reservations are not actual provider bills. No real budget, entitlement or retry is approved here.

Before key access/new HTTP, establish complete bound inputs, readable intact snapshots/cache, feasible source windows within the video, exact planned count/text volume, explicit numeric request/text/cost limits and price basis. Complete-cache reuse needs neither a key nor new paid permission. Validate file/settings failures without reading credentials; media children remain scrubbed. Tests use invented prices/keys and a fake transport only.

## Audio boundary

Normalize complete valid WAV inputs without changing their raw bytes. Segment windows in milliseconds map exactly at 48 samples per millisecond. The caller supplies the whole-video timeline frame count; integration derives it from the measured video duration with explicit nearest-sample, half-up rounding and reports that quantization. Video packets/timestamps are never shifted or extended. Reject nonpositive/overlapping/out-of-range windows and missing/corrupt speech rather than filling a missing utterance with silence.

For positive window frames W and measured speech frames D, required speedup is `max(1, D/W)`. Use the configured limit, initially 1.20. Preserve ordinary-speed speech when it fits; fill only unused time with silence. Apply pitch-preserving local fitting only when needed, then measure actual frames again. An overrun, including the known eight-sample case, is refusal with segment/window/duration/required-speedup evidence, never trimming quiet tails or speech. Stream the complete timeline as headerless PCM; do not require a whole-lecture RIFF WAV or a whole-file memory buffer.

Russian narration is mono AAC-LC 48 kHz/192 kbit/s. Convertible original PCM/MP3/Opus becomes AAC-LC at that rate/bitrate while preserving one/two channels; compatible AAC-LC stays copied. Distinguish codec priming/padding from speech placement in verification; no `shortest` operation may silently cut the intended content. Synthetic signals prove technical placement/retention, not human intelligibility.

## Container and integration boundary

C constructs only a fresh staging candidate: one unchanged H.264 video, selected original English audio, Russian narration, English and full-translation Russian mov_text tracks. Preserve original video packets/timeline and compatible copied AAC packets; inspect converted-audio profile separately. Use actual persisted track IDs for MP4Box. Russian audio is default, English is not; both subtitles are nondefault/nonforced. Never change player preferences or infer language from stream position.

Verification observes the saved file: five stream roles, codecs/languages, flags, packet identity where copy is required, complete extracted text/times and full decoding. Account explicitly for documented codec delay/padding; do not equate lossy decoded samples with an exact PCM hash or hide a mismatch under an unexplained tolerance. Return measured values and failures. C tests with prepared synthetic tracks and does not publish a final destination or mutate job state.

D validates and registers component artifacts under one writer and stage-specific identities, publishes only a verified candidate without overwriting an existing path, and wires the approved CLI/exit table. It owns the combined signature/history/recovery checks after all predecessors land. `assemble` must work offline from intact retained artifacts and cannot reach the speech transport; unchanged cached speech causes zero calls. Defaults-only rebuilds preserve prepared tracks. Reports retain full translation and actually posted text, automated evidence, publication state and pending human checks separately. Stage 6 spoken-example, listening, QuickTime/IINA and paid-pilot decisions remain unmet and separately authorized.

## Acceptance and authority

Every lane runs its focused distinguishing tests and the full documented local quality runner with required native tools and no hidden required skips. Retain Stage 1/2 regressions, installation/resource/archive checks and failures. B/C pass their native synthetic acceptance before A exists; D repeats affected combined checks after integration. Each task carries unresolved criteria forward, rather than accepting a green aggregate as full delivery.

This is a dependency and interface contract, not a schedule, resource reservation, price, launch, merge, deployment or paid-call authorization. Exact task bases, PRs, assignments and result versions belong to their issues; the lead reconciles shared changes. No GitHub Actions or replacement CI service is introduced. Public task context must be retrievable from the pinned repository and issues without private skills, personal configuration, private course assets or a preselected agent.
