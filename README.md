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

## Compress an Android folder

Run these commands from the project directory. Connect and unlock your phone,
enable **USB debugging** in Developer options, and accept the authorization
prompt on the phone. Then list the connected devices.

```sh
uv run video_compress.py --list-devices
```

Copy the serial from the first column of the row marked `device`. Set it
below in place of `YOUR_SERIAL`. If the row says `unauthorized`, unlock the
phone and accept the USB debugging prompt.

```sh
serial=YOUR_SERIAL
adb -s "$serial" shell ls -1 /sdcard/
adb -s "$serial" shell ls -1 /sdcard/Movies/
```

Pass the phone's absolute folder path as the first argument and select the phone
with `--device "$serial"`. Android's **Internal storage** is available at
`/sdcard`. Common starting folders are `/sdcard/DCIM/Camera`, `/sdcard/Movies`,
and `/sdcard/Download`. Quote the path if it contains spaces. The CLI scans the
selected folder and all its subfolders.

```sh
# Preview just one course folder
uv run video_compress.py "/sdcard/Movies/My Course" --device "$serial" --preset course --dry-run

# Create -small.mp4 copies beside the originals on that phone
uv run video_compress.py "/sdcard/Movies/My Course" --device "$serial" --preset course

# Preview the camera folder with the family preset
uv run video_compress.py /sdcard/DCIM/Camera --device "$serial" --preset family --dry-run
```

## Compress a Mac folder

```sh
uv run video_compress.py "$HOME/Movies/Family Videos" --dry-run
uv run video_compress.py "$HOME/Movies/Family Videos" --preset family
```

## Presets and settings

| Preset | Intended use | Video and audio | Adjustment |
| --- | --- | --- | --- |
| `family` (default) | Camera and family videos | HEVC MP4 up to 1080p. Keeps the source frame cadence and rotation. Copies AAC audio where possible. | `--quality 60` by default. Higher values may preserve more detail and produce larger files. |
| `course` | Lessons for an older TV | H.264 Main MP4 up to 1280×720 at 30 fps with stereo AAC-LC at 96 kbps. | `--video-kbps 1000` by default. Try `--video-kbps 1500` if small text is hard to read. |

Both presets save an output only if it is at least 10% smaller by default.
Use `--min-savings 0` to try low-bitrate HEVC sources and save any output that
is at least as small as its source.

Dry runs show candidate names and existing output collisions. The bitrate and
savings decisions happen during a real run. Rerun the same command after an
interruption to skip completed copies. Names ending in `-small` are reserved
for outputs. Files that would share an output name, HDR and Dolby Vision
videos, and unsupported subtitle tracks are reported and skipped.

Each completed copy preserves the file modification time and the embedded
recording date when present. The CLI has no original-removal command. On
Android it needs enough free space beside the originals for the new copies.
