# 001: Localize one lecture with a reviewed translation

## Preparation state

Feature phase: `preparing`. This specification owns the scenario and its phase record. Scenario scope and acceptance criteria are approved; the technical choices below remain open. Implementation and runtime acceptance have not started. Next action: select the translation exchange format and prepare the remaining technical decisions.

## User and purpose

A learner studies an English lecture first, then watches its complete Russian version for understanding and reinforcement. This scenario makes the [project workflow](../../../README.md) reproducible from documented public inputs.

## Inputs

- One local English lecture video with its original audio.
- English subtitles whose timing matches that video.
- A complete, reviewed Russian translation associated with the same source units. Translation is prepared outside this application; its exchange format remains open.
- An output destination and a configured speech backend and voice. Backend selection, setup and cost controls remain open; credentials stay outside source control.

## Workflow

1. Validate the inputs and destination, then create or resume a job tied to those inputs.
2. Generate Russian narration. Keep the full translation unchanged and any narration-specific wording separately reviewable.
3. Check speech timing and completeness. If an acceptable fit cannot be established, preserve the job and report the affected units for review.
4. Assemble and verify the local result before marking the job complete. Preserve the original recording and existing outputs.

## Outputs

- A lecture video with selectable original English audio and Russian narration, plus independently selectable English and Russian subtitles.
- The full Russian translation and the text actually used for narration.
- A report identifying the inputs, outputs, completed checks and any unresolved issues.

## Acceptance criteria

1. The original recording and existing outputs remain unchanged.
2. The complete translation is retained; source units are not silently dropped, summarized or replaced by narration edits.
3. Both complete audio versions and subtitle languages can be selected in the documented supported player; subtitles can be disabled.
4. Narration is intelligible, fits the agreed timing policy and has no clipped speech endings. A failed check produces an actionable report instead of a successful final result.
5. An interrupted job can resume without repeating unchanged completed work. Changed inputs invalidate affected work rather than silently reusing it.
6. The documented example can be reproduced from a clean public checkout with original or rights-cleared synthetic material and no private skills or personal configuration.

Meaning and listening quality need human review; container and timing checks alone do not prove them. External adoption is a separate desirable outcome.

## Scenario boundary and open choices

The scenario starts with local media, timed subtitles and a reviewed translation. Recording, platform access, transcription and translation generation are outside this scenario.

Before implementation, select the translation exchange format, speech backend and dependency/license boundary, output container and supported player, and acceptable timing policy. No CLI syntax or runtime has been selected.
