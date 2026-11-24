# Video Compression

Compress a folder of videos on a Mac or an ADB-connected Android phone. For example,
`Courses/Lesson 01.MOV` produces `Courses/Lesson 01-small.mp4`. The folder is
scanned recursively and the original stays in place.

## Setup

Install the system tools with Homebrew.

```sh
brew install uv ffmpeg
brew install --cask android-platform-tools  # For Android folders
uv sync --locked
```

The CLI uses Python 3.11 or later and has zero third-party Python dependencies.
uv manages the project's `.venv` and lockfile. FFprobe reads the source and
output formats, tracks, and metadata. FFmpeg encodes through VideoToolbox and
decodes the output as a validation check. ADB transfers one file at a time
when working on a phone.

## Use

```sh
# Preview a Mac folder
uv run video_compress.py "/path/to/Family Videos" --dry-run

# Compress the same folder
uv run video_compress.py "/path/to/Family Videos" --preset family

# Find your phone's serial and preview an Android folder
uv run video_compress.py --list-devices
uv run video_compress.py /sdcard/Movies --device SERIAL --preset course --dry-run

# Compress that Android folder
uv run video_compress.py /sdcard/Movies --device SERIAL --preset course
```

- **Family** uses HEVC MP4, limits video to 1080p, keeps frame cadence and
  rotation, and copies AAC audio. The default VideoToolbox quality is 60.
  Adjust it with `--quality`. Larger values may retain more detail and yield
  larger files.
- **Course** uses H.264 Main profile, up to 1280×720 at 30 fps, and stereo
  AAC-LC at 96 kbps. Adjust the default 1,000 kbps video target with
  `--video-kbps`.
- **Savings** defaults to 10%. Use `--min-savings 0` to try low-bitrate HEVC
  sources and save any output that is at least as small as its source.

Dry runs show candidate names and existing output collisions. The bitrate and
savings decisions happen during a real run. Rerun the same command after an
interruption to skip completed copies. Names ending in `-small` are reserved
for outputs. Files that would share an output name, HDR and Dolby Vision
videos, and unsupported subtitle tracks are reported and skipped.

Each completed copy preserves the file modification time and the embedded
recording date when present. The CLI has no original-removal command. On
Android it needs enough free space beside the originals for the new copies.
