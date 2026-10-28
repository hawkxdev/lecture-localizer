# 001: Localize one lecture with a reviewed translation

## Preparation state

Feature phase: `in_progress`. This specification owns the scenario and its phase record. The scenario, acceptance criteria and technical choices below are approved. The initial speedup limit remains subject to listening validation. The [implementation plan](plan.md) and its stage sequence are approved. Stage 1 (installable package and local tool checks) is delivered through [PR 6](https://github.com/hawkxdev/lecture-localizer/pull/6); Stage 2 (validated persistent jobs and selective reuse) is delivered through the merged [issue 7](https://github.com/hawkxdev/lecture-localizer/issues/7) work; the component stages are delivered through PRs 16, 15 and 17; the integrated guarded run and offline assembly of stages 5.4-5.5 is delivered through [PR 18](https://github.com/hawkxdev/lecture-localizer/pull/18), pending lead acceptance and the owner merge. A live pilot needs its own protocol and authorization.

## User and purpose

A learner studies an English lecture first, then watches its complete Russian version for understanding and reinforcement. This scenario makes the [project workflow](../../../README.md) reproducible from documented public inputs.

## Inputs

- One local English lecture video with its original audio.
- English SRT subtitles whose timing matches that video.
- A `translation.json` file containing the complete, reviewed Russian translation linked to the source subtitle cues, with optional separate narration text. Translation and narration wording are prepared and reviewed outside this application.
- An output destination and speech configuration for direct xAI Text to Speech, initially using the `eve` voice. The user supplies their own API key; setup and cost controls remain open, and credentials stay outside source control.

## Input compatibility and conversion

Accept MP4, MOV or MKV containing H.264 video with 8-bit `yuv420p` pixels in SDR and a selected English audio track with one or two channels. Identify containers and codecs from the actual streams rather than the filename extension.

| Input | Required behavior |
|---|---|
| Supported H.264 video | Copy without re-encoding; preserve resolution, frame rate and the original timeline |
| Original AAC-LC, mono or stereo | Copy the original audio packets |
| Original PCM `pcm_s16le`, MP3 or Opus, mono or stereo | Convert the selected original audio to AAC-LC in the new output |
| Another video format or unsupported audio | Stop before synthesis and report the incompatibility |

If more than one source audio track is available, require the user to select the English track explicitly. Do not infer the language from its first-track position. Missing source audio or a read/decode failure stops processing before paid synthesis.

Preserve the input file independently of the output. Any audio conversion affects the track in the new MP4, not the source file. The new Russian narration remains an AAC track under the output profile below.

The first version does not include video transcoding. An incompatible video may be prepared as a separate compatible copy outside the application; the supplied SRT and JSON must then correspond to that new input. Input-profile selection does not establish tested compatibility with every encoding variant or reference player.

## Translation input and text use

Each JSON fragment identifies its source subtitle cues and contains the full Russian translation plus optional separate narration text. A fragment may group adjacent source cues into a complete thought. Validate the association with the supplied source and complete fragment coverage before synthesis; mismatched inputs or missing fragments must be reported.

Russian subtitles use the full reviewed translation. Speech uses the supplied narration text when present and otherwise the full translation. Preserve both the full translation and the text actually spoken. The application does not generate translations or automatically shorten wording to fit an interval. If acceptable speech timing cannot be established, preserve the job and identify the affected fragment for an externally reviewed revision.

## Translation JSON contract

All fields below are required except `narration`. Reject unknown fields, duplicate JSON keys, unsupported schema versions, empty text strings and `null` values before synthesis.

| Field | Contract |
|---|---|
| `schema_version` | Integer `1` |
| `source` | Object containing the two input hashes below |
| `source.video_sha256` | String containing the 64 hexadecimal characters of the SHA-256 digest of the complete supplied video bytes |
| `source.srt_sha256` | String containing the 64 hexadecimal characters of the SHA-256 digest of the complete supplied English SRT bytes |
| `source_language` | String `en` |
| `target_language` | String `ru` |
| `segments` | Ordered array of translation segments |
| `segments[].id` | Nonempty unique string retained when editing the same segment; an ID alone never validates cached audio |
| `segments[].source_cues` | Nonempty ordered array of consecutive positive integer source cue positions, counted from one in the parsed SRT; printed SRT sequence labels are not reference IDs |
| `segments[].translation` | Nonempty full reviewed Russian translation |
| `segments[].narration` | Optional nonempty externally reviewed speech text; if absent, speak `translation` |

Segments follow source order and cover every source cue exactly once, without omissions or duplicate references. Verify both input hashes and complete cue coverage before synthesis. A mismatch stops processing. Structural coverage does not establish semantic translation completeness; that remains a human review responsibility.

Do not duplicate timestamps in the JSON. Derive them from the referenced SRT cues. Generate one Russian subtitle per segment using its complete `translation` and its source window defined below. Narration edits and audio fitting do not change Russian subtitle text or timing.

Example for an SRT containing three cues; the repeated-letter hashes are illustrative and must be replaced with actual input digests:

```json
{
  "schema_version": 1,
  "source": {
    "video_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "srt_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "source_language": "en",
  "target_language": "ru",
  "segments": [
    {
      "id": "s001",
      "source_cues": [1, 2],
      "translation": "Бизнес-анализ помогает определить потребности и предложить подходящие изменения.",
      "narration": "Бизнес-анализ помогает определить потребности и предложить изменения."
    },
    {
      "id": "s002",
      "source_cues": [3],
      "translation": "Теперь рассмотрим пример."
    }
  ]
}
```

## Speech timing policy

Preserve the original video timeline. A segment's source window runs from the earliest start to the latest end of its referenced cues, including gaps within that group. Different segment windows must not overlap. Reject overlapping windows before synthesis and identify the segments requiring external regrouping or input correction.

Start narration at the beginning of its window. If it is shorter than the window, retain normal speed and fill the remaining time with silence. If it is longer, allow local speedup while preserving pitch. Do not shift following segments, extend the video, or trim speech endings or quiet sections. Preserve the complete provider audio response separately from fitted audio.

The approved initial maximum speedup is `1.20`, or 20 percent. This is a provisional guard for a future listening check, not a demonstrated intelligibility threshold. Keep fitting parameters in job settings, outside the translation JSON.

For measured speech duration `D` and positive available window duration `W`, the required speedup is `max(1, D / W)`. If it exceeds `1.20`, stop the affected segment. After fitting, measure the audio again and require the complete result to fit inside the window. Never trim the result merely to satisfy that check.

| Five-second window | Required speedup | Timing disposition |
|---|---|---|
| Four seconds of speech | `1.00` | Normal speed, followed by one second of silence |
| Six seconds of speech | `1.20` | Within the provisional limit; still requires fitting and listening checks |
| 6.1 seconds of speech | `1.22` | Stop and report the segment |

A timing failure preserves the job and original speech. Report the segment ID, available time, measured duration, required speedup and configured limit. Wording revision occurs outside the application; do not automatically shorten text or mark the job complete.

## Resumption and affected work

| Changed input | Work to invalidate or repeat |
|---|---|
| Text actually spoken | Synthesis of the affected segment and downstream assembly |
| Full translation while separate narration remains unchanged | Russian subtitle generation and assembly; no new synthesis for unchanged narration |
| Fitting policy | Local fitting and assembly using retained provider speech |
| Startup audio or subtitle choices only | Rebuild the MP4 from unchanged prepared tracks; do not repeat translation, synthesis or speech fitting |
| Video or source SRT | Require an updated JSON source binding before reuse |

Retain source responses and identify reusable work from the actual relevant inputs and settings, not segment IDs or file existence alone.

## Speech backend and delivery boundary

The first supported speech provider is xAI, accessed directly through its Text to Speech REST API with `eve` as the initial voice. Send only the text selected for narration to the provider; retain local video processing and assembly.

The first-version direction uses project-authored client and orchestration code under MIT and separately installed media tools. pyVideoTrans internals are outside the application core. The concrete media utilities and redistribution obligations still require review for the actual delivery.

Direct synthesis does not place speech into the video's intervals. A separate local adapter must measure, fit and assemble the speech under the agreed timing policy while preserving speech endings. Provider selection alone does not establish adapter correctness, listening quality, current account access or complete license compliance.

## Output profile and reference players

The first output profile is MP4 with two AAC audio tracks, original English and Russian narration, plus two separate embedded `mov_text` subtitle tracks, English and Russian. Russian subtitles use the full reviewed translation. Subtitles remain selectable text rather than being burned into the picture, and can be disabled independently of the selected audio language. Preserve the original input file.

QuickTime Player and IINA on macOS are the reference players for initial acceptance. Verify both audio languages, both subtitle languages and subtitles off in each player. Container inspection and decoding alone do not establish this player behavior.

The input codecs, copy/conversion policy and technical basis are defined in this specification. Selecting MP4 does not establish compatibility with every source video or player.

## Startup track defaults

The MP4 marks Russian narration as its default audio track; the English original remains available for manual selection. Both subtitle languages start disabled in the file and neither is forced. Do not change global QuickTime Player or IINA preferences. These defaults do not impose a viewing order.

| Track | File flags |
|---|---|
| Russian narration | `default=1` |
| English original | `default=0` |
| Russian subtitles | `default=0`, `forced=0` |
| English subtitles | `default=0`, `forced=0` |

Store these choices in job settings. Changing only startup choices rebuilds the MP4 from the existing unchanged tracks without repeating translation, synthesis or speech fitting.

Verify the persisted file flags separately from actual player startup. During acceptance, record the initial audio selection and subtitle state in QuickTime Player and IINA with the user's current preferences, then check manual language selection and subtitle disabling. File metadata alone does not establish player behavior; actual startup remains unverified until those checks are performed.

## Technical basis

Target Python 3.12 or later, with initial platform acceptance on macOS. Minimum-version compatibility must be tested during implementation; it is not established by the current design probes. Use argparse for the CLI, synchronous HTTPX for the direct speech API, and jsonschema for structural validation. Pin dependency versions and review their complete notices when preparing the environment.

Use an explicit Draft 2020-12 JSON Schema alongside strict JSON parsing and semantic checks. Schema validation alone does not detect duplicate keys already discarded by a parser or establish input-hash identity and complete ordered cue coverage.

The proposed package location is `src/lecture_localizer`, exposed as `lecture-localizer`. Its command responsibilities are approved; exact arguments and exit codes belong in the implementation plan:

| Command | Responsibility |
|---|---|
| `doctor` | Check local dependencies and required capabilities without contacting the speech API |
| `prepare` | Validate inputs and prepare the job without API calls |
| `run` | Execute the authorized job and resume unchanged completed work |
| `assemble` | Assemble from retained artifacts without API calls |
| `status` | Read job state and its report |

Run stages sequentially under one job writer. Keep a versioned JSON manifest, artifact hashes and atomic checkpoints in a user-selected job directory outside the source repository. Stage signatures separate synthesis, fitting, subtitles and container settings according to the resumption rules above. No server, database or task queue is required; execution and verification remain local.

Use separately installed FFmpeg, ffprobe and GPAC MP4Box through subprocesses. FFmpeg handles audio conversion, atempo and muxing; MP4Box applies the approved MP4 track flags; ffprobe and stream extraction verify the persisted result. Check required capabilities rather than treating an arbitrary version string as proof of compatibility. Do not import pyVideoTrans internals or include any source-removal step.

Request xAI speech with `voice_id=eve`, `language=ru`, `speed=1.0`, WAV output at 48 kHz and `with_timestamps=false`. Preserve the complete response body before local conversion. The request mode is documentation-backed; its actual content type, decoding and listening quality still require the authorized live pilot.

Use mono 48 kHz signed 16-bit PCM as the working speech representation. Place complete segments at integer sample positions, adding only the necessary silence. Build long output incrementally rather than holding the whole lecture in memory. Apply atempo only when needed and remeasure its actual output against the approved window; a computed ratio alone does not establish a successful fit.

Encode Russian narration as mono AAC-LC at 48 kHz with a target bitrate of 192 kbit/s. When original PCM/MP3/Opus audio needs conversion, use AAC-LC at 48 kHz and a target 192 kbit/s while retaining its one or two channels. Compatible original AAC-LC remains copied without resampling or re-encoding. These settings do not establish perceptual transparency or verified speech quality.

Before each paid request, persist the operation start. An interrupted operation without a verified complete response has an uncertain outcome and requires an explicit retry decision; do not retry paid calls or switch providers automatically. Require validated inputs, the planned request count and text volume, and explicit spending limits before paid execution. No numeric live-pilot budget is assigned by this specification.

Read the key from `XAI_API_KEY`; do not store it in manifests, logs or media-process arguments. Keep credentials out of media subprocesses. Configure explicit HTTP timeouts and distinguish network inactivity from total operation duration. Creating a job, reading status and offline assembly require no provider access.

Project-authored code remains MIT. HTTPX and jsonschema retain their own license and copyright notices, and the locked transitive dependency set requires review. Do not bundle Python, FFmpeg, GPAC or pyVideoTrans binaries in the first distribution. External executable use does not relabel those tools or the whole dependency set as MIT.

## Workflow

1. Validate the inputs and destination, then create or resume a job tied to those inputs.
2. Generate Russian speech from the selected text under the rule above. Keep the full translation unchanged and any narration-specific wording separately reviewable.
3. Check speech timing and completeness. If an acceptable fit cannot be established, preserve the job and report the affected units for review.
4. Assemble and verify the local result before marking the job complete. Preserve the original recording and existing outputs.

## Outputs

- An MP4 lecture video with the two audio tracks and two independent subtitle tracks defined above.
- The full Russian translation and the text actually used for narration.
- A report identifying the inputs, outputs, completed checks and any unresolved issues.

## Acceptance criteria

1. The original recording and existing outputs remain unchanged.
2. The complete translation is retained and used for Russian subtitles; source units are not silently dropped, summarized or replaced by narration edits. Speech uses separate supplied narration text when present and otherwise the full translation.
3. Both complete audio versions and subtitle languages can be selected in QuickTime Player and IINA on macOS; subtitles can be disabled independently of audio selection in each player.
4. Narration is intelligible, fits the agreed timing policy and has no clipped speech endings. A failed check produces an actionable report instead of a successful final result.
5. An interrupted job can resume without repeating unchanged completed work. Changed inputs invalidate affected work rather than silently reusing it.
6. The documented example can be reproduced from a clean public checkout with original or rights-cleared synthetic material and no private skills or personal configuration.

Meaning and listening quality need human review; container and timing checks alone do not prove them. External adoption is a separate desirable outcome.

## Scenario boundary and open choices

The scenario starts with local media, matching English SRT subtitles and reviewed Russian text in JSON. Recording, platform access, transcription, translation generation and narration rewriting are outside this scenario.

The runtime, command responsibilities, media tools, speech-fitting approach, audio encoding parameters and first-distribution boundary are approved above. The implementation plan must define task-level interfaces, dependency pins, tests and failure handling. Minimum Python compatibility, long-file handling, live speech quality and reference-player behavior remain unverified; the provisional speedup limit still needs listening validation.
