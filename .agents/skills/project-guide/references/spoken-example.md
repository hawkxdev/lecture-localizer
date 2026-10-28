# Spoken example reproduction

README owns the user commands. `scripts/make-spoken-example.py` owns retrieval and construction; its adjacent `spoken-example-v1.json` fixes reviewed IDs, English texts, Russian translations, cue windows, byte counts and SHA256 digests. `SPOKEN_EXAMPLE_NOTICE.md` owns corpus attribution and adaptation notices. The tone fixture remains a separate technical check.

The public rows endpoint accepts dataset/config/split/offset/length. The recipe does not pass an undocumented revision parameter or claim its reference revision is served. It first validates all selected row identities and texts, then downloads only HTTPS assets on the expected host and dataset/subset/row paths. Exact bytes remain mandatory even when a metadata revision changes. Signed query strings and foreign row file paths do not become provenance. Failures stop without automatic retries or silent substitutions.

An explicit `--audio-dir` uses only exact hash verified public FLACs and opens no network client. Both modes preserve an existing destination, keep output outside the source checkout and retain partial verified files under an incomplete marker on failure. Generated translation hashes bind the actual media, not an expected digest from another FFmpeg version. Native media subprocesses receive the existing scrubbed environment.

Source preparation needs no provider credential, speech request or player launch. The generated price snapshot is an example requiring review before a separately authorized paid run. Raw provider responses and private job context never ship. A passing source recipe or exported public tree does not establish already published Git delivery, speech quality, reference player behavior or the provisional acceleration limit.
