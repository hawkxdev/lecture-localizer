# HLD: multilingual localization foundation

**Source:** the owner's request for arbitrary source and target languages and subsequent dev-plan-hld invocation; the accepted [feature 001 specification](../001-lecture-localization/spec.md#preparation-state) supplies the preserved invariants.
**Plan decision:** content and destination approved by the owner. The owner requires English for all project documentation. Implementation is not assigned.
**Path:** docs/specs/002-multilingual-localization/plan.md.
**Inspected base:** main 38fe87d5731ffa9f31c522fa8869979c857534a2. The completed feature 001 plan remains unchanged.

## Owner review checkpoints and progress

The design and preparation documents are ready. Implementation and acceptance stay open at [tasks.md](tasks.md#feature-state). [Issue #22](https://github.com/hawkxdev/lecture-localizer/issues/22) owns the language/profile foundation; [issue #24](https://github.com/hawkxdev/lecture-localizer/issues/24) extracts a small existing-SRT regression guard. Its external [PR #25](https://github.com/hawkxdev/lecture-localizer/pull/25) is submitted at `16246a5ebc2cd39b3b4ab3f954160fa6259cf76b`; maintainer verification and acceptance are pending. The unit contribution alone does not complete Stage 4.

Technical verification, visual inspection and the owner's acceptance decision are recorded separately. Each stage uses its earliest usable result for review, with remaining checks explicit. Reports, JSON and fixtures are reviewable artifacts for stages without a new interface; no unrelated interface is required. No technical or visual implementation checkpoint below is accepted by documentation preparation.

| Stage | Technical result to verify | Owner-visible result and expected inspection | Earliest checkpoint, environment and retained evidence | Current outcome |
| --- | --- | --- | --- | --- |
| 1. Language model and contracts | Distinguishing language/profile/schema tests, exact v1 regressions and installed resources satisfy M1-M3/M8 | Read the sample v2 contract, capability data and profile cases: full tags, media codes, allowed/refused combinations and voice are understandable and consistent | Inspect each delivered foundation artifact, then the complete stage; Python/dev install and synthetic JSON only. Retain exact revision, fixture output, checks and the owner's decision | Preparation complete; implementation unassigned |
| 2. Compatible preparation and state | v1/v2 prepare/status, immutable snapshots, selective invalidation and partial-delivery refusals satisfy M1-M5/M8 | Inspect status/profile output and an incompatible-job error: the saved pair/voice, preserved v1 behavior and required fresh destination are clear | Review as soon as prepare/status and its profile view are usable; fresh synthetic jobs outside the checkout, no key/network. Retain manifests/status/error output, revision, checks and separate visual decision | Waiting for foundation |
| 3. Speech profile, journal and recovery | Fake-transport payload/signature, reuse, uncertainty and offline recovery controls satisfy M1-M5/M7-M8 | Inspect one synthetic request/profile, cache-hit result and uncertain-attempt record; no result disguises paid history or advertises unfinished v2 CLI work as complete | Review each usable payload/offline component, then the combined internal path; fabricated HTTP/PCM evidence only. Retain exact revision, request/journal/recovery observations, checks and the owner's inspection decision | Waiting for compatible state |
| 4. Multilingual MP4 and integration | Saved five-stream containers, tags/names/flags, exact subtitle text/times, source integrity and refusal controls satisfy M1/M3-M8 | Inspect the saved stream report and extracted source/target subtitles: both roles, full tags and complete texts are visible and agree with the inputs | Review the first complete synthetic candidate and each relevant negative case, then full v2 integration; local FFmpeg/ffprobe/MP4Box and synthetic media. Retain exact output/report hashes, extracted text, checks and visual decision; never auto-open media | Unit-regression PR submitted; remaining integration unassigned |
| 5. Reproducibility and acceptance | Exact-result QC/build/install on both interpreters, public resources and the separately approved packet satisfy AC7-AC8 | Inspect the public usage/packet before spending, then the exact authorized output with competent meaning/listening and reference-player track/startup checks | Review the runnable public example and packet before key access; final sample after explicit approval/execution. Retain environment/revision, input/output identities, technical results and separate human/owner decisions | Waiting for integrated result and separate sample authority |

## Outcome and scope

Inputs remain a local video, matching source SRT and a complete reviewed translation. The user specifies source and target languages; narration language and voice form an explicit job profile. The pipeline preserves the original and produces an MP4 with original/target audio and source/target subtitles. Stream roles remain distinct even when their language tags are equal.

Downloading URLs, speech recognition, translation generation, voice cloning and a new provider framework are outside this plan. Subsequent text preparation owns those operations and consumes this contract instead of duplicating language logic. xAI remains the first TTS provider. This plan grants no new credential or spending authority.

Documented provider capability, locally verified technical behavior and human assessment of a particular sample remain separate evidence levels. Availability of a profile does not establish quality for every language pair.

## Requirement basis and preserved constraints

Feature 001 continues to own [complete text and separate narration](../001-lecture-localization/spec.md#translation-input-and-text-use), [timing](../001-lecture-localization/spec.md#speech-timing-policy), [resumption](../001-lecture-localization/spec.md#resumption-and-affected-work), [output profile](../001-lecture-localization/spec.md#output-profile-and-reference-players) and [acceptance criteria](../001-lecture-localization/spec.md#acceptance-criteria).

- M1: source and target languages are job data; EN -> RU is one reference pair.
- M2: the selected language/voice consistently determines the actual request, provenance, cache, subtitles, container and verification.
- M3: preserve the complete language tag; a container language code is a separate representation.
- M4: reject an unsupported combination before reading a key or making a paid request. Offline work with valid retained responses needs no provider access.
- M5: preserve old jobs, responses, journals, receipts and outputs. Unchanged feature 001 jobs retain their exact signatures and make zero new requests on a complete cache.
- M6: preserve complete texts, separate narration edits, refusal without trimming, one writer, explicit uncertain-operation retries and no-clobber publication.
- M7: inspect the persisted container and distinguishing negative cases. Metadata and extracted Unicode do not establish listening or player acceptance.
- M8: public examples and installation need no private canon, personal paths, credentials or restricted materials.

These requirements derive from the owner's description and approved HLD. Feature 002 has its own [specification](spec.md) and [task/phase owner](tasks.md); feature 001 remains accepted within its recorded evidence limits.

## Architectural decisions

### 1. One language and speech profile with distinct representations

A new localization.py owns tag normalization and the immutable effective profile. Use langcodes 3.5.1 without the optional language-data extra for parsing, normalization and three-letter conversion. Retain its pinned dependency and MIT notice instead of growing a project-specific language parser or replacement dictionary.

Persist the complete normalized BCP 47 tag in the document, job and report; preserve original spelling in the immutable input JSON. Case does not change meaning. Syntactic validity does not establish usable support: product policy explicitly rejects undefined, special/private-use values and unsupported extensions. qqq/und and invalid zz/en--US are separate controls. Automatic language detection is not a persisted identity.

For MP4, use a verified three-letter representation, and include the full tag in readable track names and reports. Do not discard region/script from the job or request, silently replace unknown mappings with und, or silently reduce a regional provider variant to its base language. A packaged JSON capability catalog records its source and inspected revision; prepare/status require no network.

Rejected alternatives: writing BCP 47 directly into the current MP4 language field; slicing a tag to its first letters; treating successful to_alpha3 conversion as TTS support; implicit dialect fallback.

### 2. Translation/job v2 with exact v1 compatibility

New inputs use translation-v2.schema.json and new multilingual jobs use job-v2.schema.json. TranslationDocument exposes languages explicitly; job-v2 persists the pair and frozen effective speech profile. Structural schemas and semantic validation remain separate. A language pair does not require a separate schema.

The v1 reader and EN -> RU/Eve legacy profile retain the previous serialization, payload and signature algorithms. status/run/assemble/recover never rewrite an old job to v2. Existing prepare with v1 and no new options preserves behavior and idempotency. Supplying v2 or a different language/speech profile to a v1 job directory fails explicitly and recommends a new job directory; no data moves or deletion occur.

Repeated prepare for an existing v2 job retains its saved profile when no explicit speech change is supplied, rather than adopting new global defaults. An explicit profile change creates a revision and follows the invalidation table.

A new v2 job has its own history. Automatic cache transfer between job directories and a migration tool are outside this plan. After v2 is added, unknown-version regressions use an actually unsupported version instead of continuing to classify 2 as unknown.

Rejected alternatives: changing the meaning of job-v1 in place; rewriting all user jobs; recomputing old receipts; requiring manual manifest edits.

### 3. Actual payload and work identity share one owner

Pass the effective job profile through to xai_client. Mutable global SYNTHESIS_SETTINGS no longer independently owns new parameters. The actual payload factory also supplies request_signature. The new profile contains xAI, the exact target tag/voice and the existing WAV 48 kHz/speed 1/with_timestamps=false mode. v1 retains a separate immutable path.

Signature algorithm versions are independent of journal schemas. Preserve operations-v1 and recovery-v1 while their shape and meaning remain unchanged: they already bind exact attempts and request/synthesis signatures. Recovery and cache checks derive expectations from the particular job profile rather than another job's current settings.

Selective invalidation for v2:

| Change | Work to invalidate |
|---|---|
| Target language or voice | Affected synthesis and dependent fitting/assembly. A target language change also updates target subtitle identity. |
| Complete translation with unchanged separate narration | Subtitles/container; no new TTS. |
| Source language tag with identical actual texts, windows and target narration | Declaration/container metadata; no new TTS. |
| Case of an equivalent normalized tag | No new derived work; the input snapshot still preserves exact bytes. |
| Startup flags or readable labels only | Container; no new speech. |
| Capability documentation with unchanged effective parameters | No automatic resynthesis. A new paid call separately checks current support. |

An unfinished paid attempt remains unfinished when a revision changes. A new profile neither authorizes its retry nor turns an old attempt into a new one. Entirely offline assemble/recover receive neither transport nor a key reader.

### 4. Container and verifier use source/target roles

New MuxRequest/CandidateExpectation consume a shared language profile and neutral source/target roles. Both derive from the same job. Audio/subtitle tags, handler names, explicit source audio selection and persisted-file checks agree.

Preserve five streams, the approved original video/AAC policy, complete translation and startup flags. Verify the actual FFmpeg/MP4Box result before the existing no-clobber publication.

Preserve v1 english_text/russian_text/russian_subtitles report fields and filenames. v2 reports/fields have an explicit version and neutral names. Do not rename old receipts or artifacts automatically. Compare extracted text exactly; rendering direction and player behavior require separate acceptance.

### 5. Capabilities depend on mode; quality is separate

For reviewed SRT/translation inputs, check the language contract, its container representation and the target xAI language/voice. Do not require ASR or translation models. A future text-preparation mode adds its own checks to the same profile.

New jobs receive explicit source_language/target_language from translation-v2. At prepare time, resolve voice settings through the catalog and freeze them in the job. run arguments cannot silently change language/voice. A capability-reading command and status show the selected profile, documented support and actual evidence limits. Invalid/unsupported combinations use the existing contract-error exit before key access and HTTP.

Do not add arbitrary provider endpoints, automatic fallback, cloning or automatic quality acceptance. Eve remains the compatible default. Additional built-in voices require explicit catalog entries. API acceptance of an identifier does not establish voice quality.

## File map

Create:

- src/lecture_localizer/localization.py: language normalization, allowed representations, effective profile and catalog.
- src/lecture_localizer/resources/language-profiles-v1.json: documented current xAI capabilities and representation policy.
- src/lecture_localizer/schemas/translation-v2.schema.json and job-v2.schema.json: explicit new contracts.
- tests/test_localization.py: language and mode controls.
- [spec.md](spec.md) and [tasks.md](tasks.md): feature 002 requirements and task/phase record in the selected repo process; preserve feature 001.

Modify:

- contracts.py: v1/v2 readers, explicit languages and schema dispatch.
- settings.py: strict speech settings for new jobs without secrets.
- jobs.py: prepare/status, versions, effective profile, signatures, execution branches and reports.
- xai_client.py: one effective payload/request_signature and preserved legacy path.
- recoveries.py: job-specific profile checks without changing old receipt bytes.
- mux.py, verification.py, subtitles.py: neutral roles and consistent verification.
- cli.py: visible profiles/capabilities and neutral messages.
- pyproject.toml, scripts/qc-python-cli.sh, tests/conftest.py: dependency, new package resources, exact wheel/sdist and installation checks.
- tests/test_contracts.py, test_jobs.py, test_xai_client.py, test_recoveries.py, test_mux.py, test_verification.py, test_subtitles.py, test_pipeline.py, test_cli.py and container_fixtures.py: distinguishing cases and preserved v1 regressions.
- README.md, CONTRIBUTING.md, AGENTS.md and public project-guide: actual commands, limitations, compatibility and resources after implementation.

## Stages

| Stage | Tasks | Deliverable |
|---|---:|---|
| 1. Language model and contracts | 5 | Validated multilingual model without changing old jobs |
| 2. Compatible preparation and state | 5 | prepare/status for both families without migration |
| 3. Speech profile, journal and recovery | 5 | Internal v2 speech/offline path with preserved v1 behavior |
| 4. Multilingual MP4 and full integration | 5 | Complete local technical v2 scenario |
| 5. Verification, reproducibility and acceptance | 5 | Supported technical result and explicit human evidence limits |

Dependencies: 1 -> 2 -> 3 -> 4 -> 5. Do not assign concurrent authors to shared jobs.py/cli.py/fixtures without an explicit ownership split. Stages do not require separate agents. The subsequent repo workflow owns execution assignments.

<!-- === Stage 1: Language model and contracts === -->
## Stage 1: language model and contracts

Deliverable: validated multilingual model without changing old jobs. Executable module radius: localization.py, contracts.py, settings.py, qc-python-cli.sh; schemas/data/tests are separate.

1. Record the approved feature 002 contract and sole phase owner; link M1-M8, tasks and immutable feature 001 baseline.
2. Introduce tag normalization, distinct container/provider representations and explicit refusal of unsupported values/combinations.
3. Add translation-v2/job-v2 while preserving strict parsing, v1 and rejection of genuinely unknown versions/fields.
4. Add the packaged catalog and strict voice parameters, pin langcodes 3.5.1/notices and include new resources in the existing build gate.
5. Check existing v1 inputs and contrasting tags, dialects, unknown/special codes and unlisted voices; verify installed-wheel resources.

<!-- === Stage 2: Compatible preparation and state === -->
## Stage 2: compatible preparation and state

Dependency: 1. Deliverable: prepare/status serve both families without automatic migration. Radius: jobs.py, contracts.py, localization.py, xai_client.py (pure payload factory), cli.py.

1. Persist language pair and frozen speech profile in new job-v2; wire new input shape and exact raw snapshots.
2. Preserve v1 reading/prepare/signatures and refuse a family/profile change inside an old job directory.
3. Apply new selective invalidation; preserve sources, revisions and outputs without transferring caches between job directories.
4. Expose capabilities and selected profile in status; preserve v1 fields and version neutral v2 fields.
5. Check idempotency, identical text with different languages/voices, equivalent tag case and unchanged old jobs/receipts. Until full integration, v2 run/assemble/recover CLI paths refuse before spending and are not advertised as complete; test subsequent internal contracts separately.

<!-- === Stage 3: Speech profile, journal and recovery === -->
## Stage 3: speech profile, journal and recovery

Dependency: 2. Deliverable: internal v2 speech/offline behavior verified without changing v1 or claiming a complete CLI. Radius: xai_client.py, jobs.py, recoveries.py, localization.py.

1. Pass the effective profile to every actual-payload/signature consumer; preserve exact v1 behavior.
2. Bind journals, new/uncertain attempts, cache and retry to the particular job profile.
3. Apply the same provenance to recover, including completed WAV compatibility, original failed outcomes and immutable recovery-v1 receipts.
4. Prove unsupported language/voice refusal before key/HTTP, zero calls on a complete cache, and no transport/key access in offline paths. Catalog updates must not block valid retained offline assembly.
5. With a fake transport, check voice/language changes, corruption, cancellation and unknown outcomes; before stage 4, v2 CLI paths never publish an unverified container.

<!-- === Stage 4: Multilingual MP4 and full integration === -->
## Stage 4: multilingual MP4 and full integration

Dependency: 3. Deliverable: full local technical v2 scenario, including offline assembly and persisted outputs. Radius: mux.py, verification.py, subtitles.py, jobs.py, cli.py.

1. Supply source/target roles and the language profile to muxing and verifier expectations from one job record.
2. Align three-letter tags, readable full tags, actual track IDs and startup flags after MP4Box.
3. Preserve complete source/target texts and separate narration; check Unicode without hidden EN/RU assumptions.
4. Enable the complete v2 run/assemble/status/recover path, publishing only a verified candidate with preserved exit semantics.
5. Check EN -> RU, non-English source/non-Russian target, equal languages in distinct roles, regional tags and a deliberately wrong persisted tag; container-only changes cause no new speech.

<!-- === Stage 5: Verification, reproducibility and acceptance === -->
## Stage 5: verification, reproducibility and acceptance

Dependency: 4. Deliverable: supported technical scope, obtainable examples and separately recorded human limits. Executable radius: scripts/make-spoken-example.py when needed and qc-python-cli.sh; documents/tests are separate.

1. Prepare two isolated local copies of the exact result revision, each with its own .venv created by Python 3.12 or the development interpreter. Run scripts/qc-python-cli.sh from each root and retain sys.version plus separate results for Ruff, mypy, tests, build, archive privacy, installation and resources. Do not switch the owner's main .venv/dist; activating another Python does not replace the root/.venv selected by the current runner.
2. Verify the old public example and prepare reproducible new technical inputs without private context; check dependency/material notices and actual wheel/sdist contents.
3. Prepare an exact bounded speech packet for a new pair, for example RU -> EN: rights-cleared source/SRT/reviewed translation, voice, receiver, request count, cost/ceiling, timing and recovery. Obtain separate authorization before key access or synthesis.
4. After authorization, execute that packet and arrange meaning, pronunciation, endings and both-player checks on the exact output. Check other scripts technically and retain missing competent semantic/listening assessment as an explicit gap. Never auto-open completed media.
5. Reconcile README/CONTRIBUTING/AGENTS/guide and the sole feature 002 phase owner with actual evidence. Result review, publication/merge and release retain their own authority. One sample does not accept every language.

## Probes supporting the decisions

Local research used FFmpeg/ffprobe 9.0.2 and installed MP4Box:

- An actual saved v1 manifest passes the current schema; adding a language field and setting version 2 separately fail. Original bytes remain unchanged.
- The old payload agrees with its canonical signature; changing language at identical text changes the signature. This supports explicit profile/signature versioning, not correctness of a new implementation.
- Five synthetic container cases preserve eng/rus, deu/fra, jpn/ara and por/fra after MP4Box; direct pt-BR/fr-CA lose language tags. Positive and negative controls are separate.
- Diacritics, CJK and RTL characters round-trip exactly through both mov_text tracks. Full pt-BR/fr-CA handler names survive both tools. This is not rendering or listening evidence.
- Official langcodes 3.5.1 wheel SHA256 b6a9c25c603804e2d169165091d0cdb23934610524a21d226e4f463e8e958a72 was inspected and executed in an isolated temporary directory, without installation in the project. Its MIT license and normalization/alpha3 methods were read. Base wheel metadata requires no language-data extra.
- en/pt-BR pass library validation; zz/en--US fail. qqq/und pass, so product support cannot follow from syntax alone.

### Primary sources

- [RFC 5646: structure, case and meaning](https://www.rfc-editor.org/rfc/rfc5646.html#section-2), [meaning changes through truncation](https://www.rfc-editor.org/rfc/rfc5646.html#section-4.4.2).
- [Official ISO 639-2 list](https://www.loc.gov/standards/iso639-2/php/code_list.php).
- [langcodes 3.5.1](https://pypi.org/project/langcodes/3.5.1/), [publication 4701cd34](https://github.com/georgkrause/langcodes/tree/4701cd34cc607c4ffcd2ed675f03b1d6529398da), inspected methods and MIT license in the verified wheel.
- [xAI TTS: parameters, languages, voices and formats](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech).

### Reproducing the evidence

Schema and legacy-signature checks used saved local research inputs. Those private receipts are not promised assets of a public clone. The complete local runner verifies/extracts its wheel itself, includes every control and requires a fresh output name, preserving earlier receipts. It does not install a project dependency.

The container check below is self-contained. Its commands were repeated in a fresh directory: positive tags, negative control and handler names were checked after MP4Box, and both subtitle texts were extracted in both cases. No provider request, credential access, listening or GUI operation occurred.

Use a new empty temporary directory. Create source.srt and target.srt with cue 1 at 00:00:00,100 -> 00:00:00,800. Source text: Probe café 中文. Target text: مرحبا שלום 日本語. These original short technical strings are neither translations nor speech samples. Commands never open a player or call an API.

1. `ffmpeg -v error -f lavfi -i color=c=black:s=128x72:r=10:d=1 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 -c:v libx264 -pix_fmt yuv420p -c:a aac -ac 1 -shortest base.mp4`
2. `ffmpeg -v error -i base.mp4 -i source.srt -i target.srt -map 0:v:0 -map 0:a:0 -map 0:a:0 -map 1:0 -map 2:0 -c:v copy -c:a copy -c:s mov_text -metadata:s:a:0 language=por -metadata:s:a:1 language=fra -metadata:s:s:0 language=por -metadata:s:s:1 language=fra -metadata:s:a:0 "handler_name=Original (pt-BR)" -metadata:s:a:1 "handler_name=Narration (fr-CA)" -metadata:s:s:0 "handler_name=Subtitles (pt-BR)" -metadata:s:s:1 "handler_name=Subtitles (fr-CA)" mapped.mp4`
3. `MP4Box -disable 2 -enable 3 -disable 4 -disable 5 mapped.mp4 -out mapped-flagged.mp4`
4. `ffprobe -v error -show_entries stream=codec_type:stream_tags=language,handler_name -of json mapped-flagged.mp4`
5. `ffmpeg -v error -i mapped-flagged.mp4 -map 0:s:0 -f srt -` and `ffmpeg -v error -i mapped-flagged.mp4 -map 0:s:1 -f srt -`.

Expect four por/fra/por/fra tags, four complete handler names and exact extracted text. For the negative control, repeat command 2 into raw.mp4 with the four language values changed to pt-BR/fr-CA/pt-BR/fr-CA; repeat commands 3-5 using fresh filenames. On the measured 9.0.2/GPAC 26.07 toolchain the language tags are absent while text remains intact and commands succeed. This does not cover every tool version or alternative extended-tag mechanism.

## Risks and acceptance boundaries

1. Compatibility: generalized signatures can invalidate old cache/receipts. Preserve exact legacy behavior and test every execution entry point plus file integrity.
2. Identity: payload, cache, tags and reports can diverge. Use one effective profile and independent persisted-result negative controls.
3. Tags/scripts: valid BCP 47 does not imply provider/metadata support; extracted Unicode does not imply correct rendering. Use mode-specific capabilities and separate evidence levels.
4. Public claims and paid experiments: documentation or synthesis success does not prove every pair. Require a separately authorized packet and human assessment; missing scope cannot advance feature 002 to accepted.

## Next boundary

The owner approved the design; the feature 002 contract/task set is prepared and issues #22/#24 are published. Review external PR #25 against its bounded unit-test contract before acceptance, and assign core #22 separately. No stage implementation or new-pair sample is accepted from this preparation; automated text preparation retains its later scope.
