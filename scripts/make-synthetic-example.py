"""Create deterministic rights-safe synthetic media fixtures in a fresh directory."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

VIDEO_SECONDS = 2.0
WIDTH = 320
HEIGHT = 240
RATE = 15
FREQUENCY = 440

SRT_CONTENT = """1
00:00:00,000 --> 00:00:01,000
Synthetic cue one for technical checks.

2
00:00:01,000 --> 00:00:02,000
Synthetic cue two for technical checks.
"""

FFMPEG_ARGS = (
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    f"color=c=black:s={WIDTH}x{HEIGHT}:r={RATE}:d={VIDEO_SECONDS}",
    "-f",
    "lavfi",
    "-i",
    f"sine=frequency={FREQUENCY}:duration={VIDEO_SECONDS}",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-pix_fmt",
    "yuv420p",
    "-g",
    str(int(RATE)),
    "-c:a",
    "aac",
    "-b:a",
    "96k",
    "-shortest",
    "-y",
)


def build_parser() -> argparse.ArgumentParser:
    """Build the fixture argument parser."""
    parser = argparse.ArgumentParser(
        prog="make-synthetic-example",
        description="Create deterministic synthetic MP4 and SRT fixtures.",
    )
    parser.add_argument(
        "destination", help="fresh directory to create for the fixtures"
    )
    return parser


def create_fixtures(destination: Path) -> list[Path]:
    """Create the fixture files and return their paths."""
    destination.mkdir(parents=True)
    media_path = destination / "synthetic-example.mp4"
    srt_path = destination / "synthetic-example.srt"
    command = ["ffmpeg", *FFMPEG_ARGS, str(media_path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed with code {result.returncode}: {result.stderr}"
        )
    srt_path.write_text(SRT_CONTENT, encoding="utf-8")
    return [media_path, srt_path]


def main(argv: list[str] | None = None) -> int:
    """Run the fixture generator."""
    args = build_parser().parse_args(argv)
    destination = Path(args.destination)
    if destination.exists():
        print(
            f"destination already exists, refusing to write: {destination}",
            file=sys.stderr,
        )
        return 1
    try:
        created = create_fixtures(destination)
    except (OSError, RuntimeError) as error:
        print(f"fixture creation failed: {error}", file=sys.stderr)
        return 1
    for path in created:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{digest}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
