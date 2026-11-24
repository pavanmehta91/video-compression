# Video Compression

Compress a folder of videos on a Mac or an ADB-connected Android phone. For example,
`Courses/Lesson 01.MOV` produces `Courses/Lesson 01-small.mp4`. Only videos
directly in the selected folder are included, and the original stays in place.

## Setup

Install the system tools with Homebrew.

```sh
brew install uv ffmpeg
brew install --cask android-platform-tools  # For Android folders
uv sync --locked
```

The standard CLI uses Python 3.11 or later and has zero third-party Python
runtime dependencies. uv manages the project's `.venv` and lockfile and installs the
`video-compress` command. FFprobe reads the source and
output formats, tracks, and metadata. FFmpeg encodes through VideoToolbox and
decodes the output as a validation check. ADB transfers one file at a time
when working on a phone.

## Full-screen terminal interface

```sh
uv run --extra tui video-compress --tui
```

Choose **This Mac** or a connected Android phone, select a preset, then enter a
folder path or open **Browse**. Use Up and Down to highlight a folder, Right
to open it, and Left to go up. Enter or Space selects the highlighted folder.
**Use current folder** selects the folder whose contents you are viewing.
**Refresh devices** rescans for
authorized phones. Use **Preview** to see the planned filenames, then **Start**
to compress.

The progress bar shows the current video's encoded percentage. The status line
shows when the tool is copying, analyzing, validating, or saving. The results
panel lists saved sizes and savings, plus any skipped files and errors.
Use **Stop** or <kbd>Ctrl</kbd>+<kbd>X</kbd> to stop the run. Copying or validation
may finish its current step before the run stops. Preview the same folder again
to resume. <kbd>Ctrl</kbd>+<kbd>Q</kbd> quits when idle.

The `tui` extra installs Textual for the full-screen interface.

## Numbered menu

Run the CLI without arguments from the project directory.

```sh
uv run video-compress
```

1. Choose **This Mac** or **Android phone over USB**. For Android, connect and
   unlock the phone, enable **USB debugging**, and accept the authorization
   prompt. The menu lists authorized devices by model and serial.
2. Browse to the folder, then choose **Use this folder**. You can also choose
   **Enter a folder path** to paste a path. On Android the browser starts at
   `/sdcard`, where you can open folders such as `DCIM`, `Movies`, and `Download`.
3. Choose **Family** or **Course**. Review the planned `-small.mp4` copies, then
   type `y` to begin. Press Enter at the confirmation prompt to cancel.

Each run scans only the selected folder. To use custom preset settings or run
from a script, pass the arguments below.

## Compress an Android folder with arguments

Run these commands from the project directory. Connect and unlock your phone,
enable **USB debugging** in Developer options, and accept the authorization
prompt on the phone. Then list the connected devices.

```sh
uv run video-compress --list-devices
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
and `/sdcard/Download`. Quote the path if it contains spaces. The CLI includes
only videos directly inside the selected folder.

```sh
# Preview just one course folder
uv run video-compress "/sdcard/Movies/My Course" --device "$serial" --preset course --dry-run

# Create -small.mp4 copies beside the originals on that phone
uv run video-compress "/sdcard/Movies/My Course" --device "$serial" --preset course

# Preview the camera folder with the family preset
uv run video-compress /sdcard/DCIM/Camera --device "$serial" --preset family --dry-run
```

## Compress a Mac folder with arguments

```sh
uv run video-compress "$HOME/Movies/Family Videos" --dry-run
uv run video-compress "$HOME/Movies/Family Videos" --preset family
```

## Presets and settings

| Preset | Intended use | Video and audio | Adjustment |
| --- | --- | --- | --- |
| `family` (default) | Camera and family videos | HEVC MP4 up to 1080p. Keeps the source frame cadence and rotation. Copies AAC audio where possible. | `--quality 60` by default. Higher values may preserve more detail and produce larger files. |
| `course` | Lessons for an older TV | H.264 Main MP4 up to 1280×720 at 30 fps with stereo AAC-LC at 96 kbps. | `--video-kbps 1000` by default. Try `--video-kbps 1500` if small text is hard to read. |

Before encoding, FFprobe checks whether an MP4 already fits the preset.
**Family** skips HEVC MP4s within the resolution limit at up to 2 Mbps total.
**Course** skips TV-compatible H.264 MP4s near or below the selected video
bitrate target. Both presets save a new copy only if it is at least 10%
smaller by default. Use `--min-savings 0` to bypass the early bitrate check
and save any output that is at least as small as its source.

Dry runs show candidate names and existing output collisions. The bitrate and
savings decisions happen during a real run. Rerun the same command after an
interruption to skip completed copies. Names ending in `-small` are reserved
for outputs. Files that would share an output name, HDR and Dolby Vision
videos, and unsupported subtitle tracks are reported and skipped.

Each completed copy preserves the file modification time and the embedded
recording date when present. The CLI has no original-removal command. On
Android it needs enough free space beside the originals for the new copies.
