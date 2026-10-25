# 001: Localize one lecture with a reviewed translation

## Preparation state

Feature phase: `preparing`. This specification owns the scenario and its phase record. The scenario, acceptance criteria and decisions below are approved. Implementation and runtime acceptance have not started. Next action: agree the timing policy and complete the JSON schema, input compatibility and remaining dependency/license checks.

## User and purpose

A learner studies an English lecture first, then watches its complete Russian version for understanding and reinforcement. This scenario makes the [project workflow](../../../README.md) reproducible from documented public inputs.

## Inputs

- One local English lecture video with its original audio.
- English SRT subtitles whose timing matches that video.
- A `translation.json` file containing the complete, reviewed Russian translation linked to the source subtitle cues, with optional separate narration text. Translation and narration wording are prepared and reviewed outside this application.
- An output destination and speech configuration for direct xAI Text to Speech, initially using the `eve` voice. The user supplies their own API key; setup and cost controls remain open, and credentials stay outside source control.

## Translation input and text use

Each JSON fragment identifies its source subtitle cues and contains the full Russian translation plus optional separate narration text. A fragment may group adjacent source cues into a complete thought. Validate the association with the supplied source and complete fragment coverage before synthesis; mismatched inputs or missing fragments must be reported.

Russian subtitles use the full reviewed translation. Speech uses the supplied narration text when present and otherwise the full translation. Preserve both the full translation and the text actually spoken. The application does not generate translations or automatically shorten wording to fit an interval. If acceptable speech timing cannot be established, preserve the job and identify the affected fragment for an externally reviewed revision.

Exact JSON field names, source identity encoding and schema versioning remain technical design choices.

## Speech backend and delivery boundary

The first supported speech provider is xAI, accessed directly through its Text to Speech REST API with `eve` as the initial voice. Send only the text selected for narration to the provider; retain local video processing and assembly.

The first-version direction uses project-authored client and orchestration code under MIT and separately installed media tools. pyVideoTrans internals are outside the application core. The concrete media utilities and redistribution obligations still require review for the actual delivery.

Direct synthesis does not place speech into the video's intervals. A separate local adapter must measure, fit and assemble the speech under the agreed timing policy while preserving speech endings. Provider selection alone does not establish adapter correctness, listening quality, current account access or complete license compliance.

## Output profile and reference players

The first output profile is MP4 with two AAC audio tracks, original English and Russian narration, plus two separate embedded `mov_text` subtitle tracks, English and Russian. Russian subtitles use the full reviewed translation. Subtitles remain selectable text rather than being burned into the picture, and can be disabled independently of the selected audio language. Preserve the original input file.

QuickTime Player and IINA on macOS are the reference players for initial acceptance. Verify both audio languages, both subtitle languages and subtitles off in each player. Container inspection and decoding alone do not establish this player behavior.

Supported input codecs, copy/transcode policy, startup track defaults and concrete media utilities remain technical design choices. Selecting MP4 does not establish compatibility with every source video or player.

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

Before implementation, define the JSON schema, supported input codecs and copy/transcode policy, acceptable timing policy, speech-fitting adapter and concrete dependency/license boundary. No CLI syntax or runtime has been selected. Installation and paid synthesis have not started.
