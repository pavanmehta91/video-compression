"""Compress a folder of videos with FFmpeg and macOS VideoToolbox."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata


VIDEO_EXTENSIONS = set(".mp4 .mov .m4v .mkv .avi .webm .wmv .flv .3gp .3g2 "
                       ".mts .m2ts .mpg .mpeg .vob .ogv .ts .asf".split())
TEMP_PREFIX = ".video-compress."
COLOR_FIELDS = ("color_primaries", "color_transfer", "color_space", "color_range")
COLOR_FLAGS = ("-color_primaries", "-color_trc", "-colorspace", "-color_range")
COLOR_CODES = {
    "color_primaries": {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6,
                        "smpte240m": 7, "bt2020": 9, "smpte431": 11, "smpte432": 12},
    "color_transfer": {"bt709": 1, "gamma22": 4, "gamma28": 5, "smpte170m": 6,
                       "smpte240m": 7, "iec61966-2-1": 13, "bt2020-10": 14},
    "color_space": {"bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6,
                    "smpte240m": 7, "bt2020nc": 9, "bt2020c": 10},
    "color_range": {"tv": 0, "pc": 1},
}
HEVC_COLOR_OPTIONS = ("colour_primaries", "transfer_characteristics",
                      "matrix_coefficients", "video_full_range_flag")
TEXT_SUBTITLES = {"mov_text", "subrip", "ass", "ssa", "webvtt", "text"}


class SkipVideo(Exception):
    """A source was left alone for a reported reason."""


class Cancelled(Exception):
    """The user cancelled the operation."""


def capture(command):
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(detail or f"{command[0]} exited with status {result.returncode}")
    return result.stdout


class Android:
    def __init__(self, serial):
        self.command = ["adb", "-s", serial]
        if capture(self.command + ["get-state"]).strip() != b"device":
            raise RuntimeError("ADB device is unavailable")

    def shell(self, *args):
        return capture(self.command + ["shell", "-T", shlex.join(map(str, args))])

    def files(self, root):
        self.shell("test", "-d", root)
        found = self.shell("find", str(root).rstrip("/") + "/", "-mindepth", "1",
                           "-maxdepth", "1", "-type", "f", "-print0")
        return [PurePosixPath(os.fsdecode(path)) for path in found.split(b"\0") if path]

    def folders(self, root):
        found = self.shell("find", str(root).rstrip("/") + "/", "-mindepth", "1",
                           "-maxdepth", "1", "-type", "d", "-print0")
        return [PurePosixPath(os.fsdecode(path)) for path in found.split(b"\0") if path]

    def stamp(self, path):
        return tuple(map(int, self.shell("stat", "-c", "%s %Y", path).split()))

    def pull(self, source, target):
        capture(self.command + ["pull", "-a", str(source), str(target)])

    def publish(self, output, destination, source, original_stamp):
        stage = self.shell("mktemp", "-d", destination.parent / (TEMP_PREFIX + "XXXXXX"))
        stage = PurePosixPath(os.fsdecode(stage.rstrip(b"\r\n")))
        staged = stage / "encode.mp4"
        try:
            capture(self.command + ["push", str(output), str(staged)])
            with output.open("rb") as file:
                expected = hashlib.file_digest(file, "sha256").hexdigest()
            actual = self.shell("sha256sum", staged).split(maxsplit=1)[0].decode()
            if actual != expected:
                raise RuntimeError("Android copy failed its checksum")
            modified = datetime.fromtimestamp(original_stamp[1], timezone.utc)
            self.shell("env", "TZ=UTC", "touch", "-m", "-t",
                       modified.strftime("%Y%m%d%H%M.%S"), staged)
            if self.stamp(staged)[1] != original_stamp[1]:
                raise RuntimeError("Android copy has the wrong modification time")
            if self.stamp(source) != original_stamp:
                raise RuntimeError("Source changed during compression")
            script = (
                'if [ -e "$2" ] || [ -L "$2" ]; then exit 3; fi; '
                'mv -nT "$1" "$2" && [ ! -e "$1" ]'
            )
            result = subprocess.run(
                self.command + ["shell", "-T",
                                shlex.join(["sh", "-c", script, "sh", str(staged), str(destination)])],
                capture_output=True,
            )
            if result.returncode == 3:
                raise SkipVideo("destination already exists")
            if result.returncode:
                raise RuntimeError("Could not publish Android copy without overwriting")
        finally:
            try:
                self.shell("rm", "-f", staged)
                self.shell("rmdir", stage)
            except RuntimeError:
                print(f"Could not clean up Android temporary directory {str(stage)!r}",
                      file=sys.stderr)


def local_files(root):
    return [path for path in root.iterdir() if not path.is_symlink() and path.is_file()]


def path_key(path, android):
    return str(path) if android else unicodedata.normalize("NFC", str(path)).casefold()


def candidates(files, android):
    occupied = {path_key(path, android) for path in files}
    pairs = sorted(
        (path, path.with_name(path.stem + "-small.mp4"))
        for path in files
        if path.suffix.lower() in VIDEO_EXTENSIONS and not path.stem.lower().endswith("-small")
    )
    counts = Counter(path_key(dst, android) for _, dst in pairs)
    for source, destination in pairs:
        key = path_key(destination, android)
        reason = None
        if key in occupied or (not android and os.path.lexists(destination)):
            reason = "destination already exists"
        elif counts[key] > 1:
            reason = "multiple sources would have the same output name"
        yield source, destination, reason


def probe(path):
    return json.loads(capture(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                               "-of", "json", str(path)]))


def streams(info, kind):
    return [stream for stream in info["streams"] if stream["codec_type"] == kind
            and not stream.get("disposition", {}).get("attached_pic")]


def frame_rate(video):
    for field in ("avg_frame_rate", "r_frame_rate"):
        value = video.get(field)
        if value and value != "0/0":
            rate = Fraction(value)
            if rate > 0:
                return rate
    return Fraction(30)


def rotation(video):
    return next((data["rotation"] for data in video.get("side_data_list", [])
                 if "rotation" in data), 0)


def recording_date(info):
    tags = info["format"].get("tags", {})
    video = streams(info, "video")[0]
    value = (tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
             or video.get("tags", {}).get("creation_time"))
    if value:
        try:
            instant = datetime.fromisoformat(value)
        except ValueError:
            return value
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def console_progress(stage, percent=None):
    if stage == "Encoding" and sys.stdout.isatty():
        print(f"\r  Encoding {percent:3.0f}%", end="\n" if percent == 100 else "", flush=True)


def encode(command, duration, progress):
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=errors, text=True
    ) as process:
        try:
            progress("Encoding", 0)
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value != "N/A":
                    percent = max(0, min(99, int(value) * 100 / (duration * 1_000_000)))
                    progress("Encoding", percent)
            if process.wait():
                errors.seek(0)
                detail = errors.read().decode(errors="replace").strip()
                raise RuntimeError(detail or "FFmpeg encoding failed")
            progress("Encoding", 100)
        except (Exception, KeyboardInterrupt):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise


def convert(source, output, info, args, progress):
    family = args.preset == "family"
    video, = streams(info, "video")
    subtitles = streams(info, "subtitle")
    if any(stream["codec_name"] not in TEXT_SUBTITLES for stream in subtitles):
        raise SkipVideo("has subtitle tracks that cannot be preserved in MP4")
    if (video.get("color_transfer") in {"smpte2084", "arib-std-b67"}
            or any("DOVI" in data["side_data_type"] for data in video.get("side_data_list", []))):
        raise SkipVideo("HDR or Dolby Vision needs a separate preset")

    command = ["ffmpeg", "-nostdin", "-v", "error", "-n", "-stats_period", "0.25",
               "-progress", "pipe:1"]
    if family:
        command += ["-noautorotate"]
    command += ["-i", str(source), "-map", "0:V:0", "-map", "0:a?", "-map", "0:s?",
                "-map_metadata", "0", "-map_chapters", "0", "-c:s", "mov_text"]
    date = recording_date(info)
    date_args = ["-metadata", f"creation_time={date}"] if date else []
    command += date_args

    if family:
        width = "if(gte(iw,ih),1920,1080)"
        height = "if(gte(iw,ih),1080,1920)"
        command += ["-c:v", "hevc_videotoolbox", "-q:v", str(args.quality), "-tag:v", "hvc1",
                    "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
                    "-c:a", "aac", "-b:a", "192k"]
        for index, audio in enumerate(streams(info, "audio")):
            if audio["codec_name"] == "aac":
                command += [f"-c:a:{index}", "copy"]
    else:
        width, height = "1280", "720"
        command += ["-c:v", "h264_videotoolbox", "-b:v", f"{args.video_kbps}k", "-profile:v",
                    "main", "-level:v", "3.1", "-fps_mode", "cfr", "-r",
                    str(min(frame_rate(video), 30)), "-c:a", "aac", "-profile:a", "aac_low",
                    "-ac", "2", "-b:a", "96k"]
    scale = (f"scale=w='min(iw,{width})':h='min(ih,{height})':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2")
    color_range = video.get("color_range")
    if family and color_range in COLOR_CODES["color_range"]:
        scale += f":in_range={color_range}:out_range={color_range}"
    color_args = [item for key, flag in zip(COLOR_FIELDS, COLOR_FLAGS)
                  if video.get(key) in COLOR_CODES[key] for item in (flag, video[key])]
    if family:
        command += color_args
    high_depth = any(depth in video.get("pix_fmt", "") for depth in
                     ("10", "12", "14", "16"))
    if family and high_depth:
        command += ["-profile:v", "main10"]
    pixel_format = "p010le" if family and high_depth else "nv12"
    command += ["-vf", f"{scale},format={pixel_format}", "-allow_sw", "0", "-movflags",
                "+faststart", str(output)]
    encode(command, float(info["format"]["duration"]), progress)

    progress("Checking output")
    rendered = probe(output)
    if family:
        actual = streams(rendered, "video")[0]
        settings = []
        needs_correction = False
        for field, option in zip(COLOR_FIELDS, HEVC_COLOR_OPTIONS):
            expected = video.get(field)
            if expected in COLOR_CODES[field]:
                settings.append(f"{option}={COLOR_CODES[field][expected]}")
            if expected not in (None, "unknown", "unspecified") and actual.get(field) != expected:
                if expected not in COLOR_CODES[field]:
                    raise SkipVideo(f"could not preserve {field}={expected}")
                needs_correction = True
        if needs_correction:
            remuxed = output.with_name("color.mp4")
            capture(["ffmpeg", "-nostdin", "-v", "error", "-n", "-i", str(output),
                     "-map", "0", "-map_metadata", "0", "-c", "copy", "-tag:v", "hvc1", *date_args,
                     "-bsf:v", "hevc_metadata=" + ":".join(settings), *color_args,
                     "-movflags", "+faststart", str(remuxed)])
            remuxed.replace(output)
            rendered = probe(output)
    return rendered


def validate(source, result, output, family):
    video, = streams(source, "video")
    new, = streams(result, "video")
    if new["codec_name"] != ("hevc" if family else "h264"):
        raise RuntimeError("Unexpected output codec")
    if abs(float(result["format"]["duration"]) - float(source["format"]["duration"])) > 0.5:
        raise RuntimeError("Output duration differs from source")
    for kind in ("audio", "subtitle"):
        if len(streams(source, kind)) != len(streams(result, kind)):
            raise RuntimeError(f"Output has a different number of {kind} tracks")
    if family:
        if rotation(video) != rotation(new):
            raise RuntimeError("Output rotation differs from source")
        if video.get("nb_frames") and new.get("nb_frames") != video["nb_frames"]:
            raise RuntimeError("Output frame count differs from source")
        for field in COLOR_FIELDS:
            expected = video.get(field)
            if expected not in (None, "unknown", "unspecified") and new.get(field) != expected:
                raise RuntimeError(f"Output {field} differs from source")
        short, long = sorted((new["width"], new["height"]))
        if short > 1080 or long > 1920:
            raise RuntimeError("Output exceeds the family resolution limit")
    elif (new["width"] > 1280 or new["height"] > 720 or new.get("level", 0) > 31
          or frame_rate(new) > 30 or new.get("pix_fmt") != "yuv420p"):
        raise RuntimeError("Output exceeds course playback specifications")
    if recording_date(source) and recording_date(source) != recording_date(result):
        raise RuntimeError("Could not preserve the recording date")
    capture(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(output),
             "-map", "0:V:0", "-map", "0:a?", "-fps_mode", "passthrough",
             "-enc_time_base:v", "demux", "-f", "null", "-"])


def efficient_reason(source, info, video, args, total_bitrate):
    media_format = info["format"]
    if (args.min_savings == 0 or source.suffix.lower() != ".mp4"
            or "mp4" not in media_format.get("format_name", "").split(",")
            or media_format.get("tags", {}).get("major_brand", "").strip() == "qt"):
        return None
    width, height = video["width"], video["height"]
    if args.preset == "family":
        short, long = sorted((width, height))
        if video["codec_name"] == "hevc" and short <= 1080 and long <= 1920 and total_bitrate <= 2_000_000:
            return f"already a low-bitrate HEVC MP4 ({total_bitrate / 1000:.0f} kbps total)"
        return None

    audio = streams(info, "audio")
    target = args.video_kbps * 1000
    stream_bitrate = video.get("bit_rate")
    # Allow normal swings around the encoder's average bitrate target.
    if (
        video["codec_name"] == "h264"
        and video.get("profile") in {"Baseline", "Constrained Baseline", "Main"}
        and 0 < video.get("level", 0) <= 31
        and video.get("pix_fmt") == "yuv420p"
        and width <= 1280 and height <= 720 and rotation(video) == 0 and frame_rate(video) <= 30
        and all(track["codec_name"] == "aac" and track.get("profile") == "LC"
                and 0 < track.get("channels", 0) <= 2 for track in audio)
        and (not stream_bitrate or stream_bitrate == "N/A" or int(stream_bitrate) <= target * 1.1)
        and total_bitrate <= (target + len(audio) * 96_000) * 1.1
    ):
        return f"already a low-bitrate, TV-compatible MP4 ({total_bitrate / 1000:.0f} kbps total)"
    return None


def compress(source, destination, android, args, progress):
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX,
                                     dir=None if android else source.parent) as work:
        output = Path(work) / "encode.mp4"
        if android:
            progress("Copying from Android")
            stamp = android.stamp(source)
            local = Path(work) / ("input" + source.suffix)
            android.pull(source, local)
            if local.stat().st_size != stamp[0] or android.stamp(source) != stamp:
                raise RuntimeError("Source changed during transfer")
        else:
            local = source
        progress("Analyzing")
        original_stat = local.stat()
        original_size = original_stat.st_size
        info = probe(local)
        if len(streams(info, "video")) != 1:
            raise SkipVideo("expected one playable video track")
        duration = float(info["format"].get("duration") or 0)
        if duration <= 0:
            raise SkipVideo("could not determine duration")
        video = streams(info, "video")[0]
        family = args.preset == "family"
        reason = efficient_reason(source, info, video, args, original_size * 8 / duration)
        if reason:
            raise SkipVideo(reason)

        result = convert(local, output, info, args, progress)
        size = output.stat().st_size
        savings = 100 * (original_size - size) / original_size
        report = f"{original_size / 1e6:.1f} MB → {size / 1e6:.1f} MB ({savings:.1f}% smaller)"
        if savings < args.min_savings:
            raise SkipVideo(f"{report}. Below the {args.min_savings:g}% savings threshold")
        progress("Validating")
        validate(info, result, output, family)
        if android:
            progress("Copying to Android")
            android.publish(output, destination, source, stamp)
        else:
            progress("Saving")
            current = local.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                original_stat.st_ino, original_size, original_stat.st_mtime_ns
            ):
                raise RuntimeError("Source changed during compression")
            os.utime(output, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            try:
                os.link(output, destination)
            except FileExistsError:
                raise SkipVideo("destination already exists") from None
        return report


def choose(prompt, options, default=None):
    print(f"\n{prompt}")
    for number, label in enumerate(options, 1):
        print(f"  {number}. {label}")
    print("  0. Cancel")
    while True:
        answer = input(f"Choice [{default}]: " if default else "Choice: ").strip()
        if not answer and default:
            return default - 1
        if answer == "0":
            raise Cancelled
        if answer.isascii() and answer.isdecimal() and len(answer) <= len(str(len(options))):
            number = int(answer)
            if 1 <= number <= len(options):
                return number - 1
        print("Enter one of the listed numbers.")


def folder_path(value, android):
    if android:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Use an absolute Android path such as /sdcard/Movies.")
        return path
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Choose a directory.")
    return path


def child_folders(root, android):
    folders = android.folders(root) if android else [path for path in root.iterdir() if path.is_dir()]
    return sorted((path for path in folders if not path.name.startswith(".")),
                  key=lambda path: (path.name.casefold(), path.name))


def browse_folder(android):
    current = PurePosixPath("/sdcard") if android else Path.home().resolve()
    while True:
        try:
            folders = child_folders(current, android)
        except (OSError, RuntimeError) as error:
            print(f"Could not list folders. {error}")
            folders = []
        actions = ["Use this folder", "Enter a folder path", "Go up one level"]
        choice = choose(f"Folder {str(current)!r}", actions + [repr(p.name) for p in folders])
        if choice == 0:
            return current
        if choice == 1:
            text = input("Folder path (press Enter to go back): ")
            if text:
                try:
                    path = folder_path(text, android)
                    if android:
                        android.shell("test", "-d", path)
                    current = path
                except (OSError, RuntimeError, ValueError) as error:
                    print(f"Could not open folder {text!r}. {error}")
        elif choice == 2:
            current = current.parent
        else:
            current = folders[choice - len(actions)]
            if not android:
                current = current.resolve()


def authorized_devices():
    rows = capture(["adb", "devices", "-l"]).decode().splitlines()
    devices = []
    for row in map(str.split, rows):
        if len(row) >= 2 and row[1] == "device":
            serial, _, *details = row
            model = next((part[6:].replace("_", " ") for part in details
                          if part.startswith("model:")), "Android")
            devices.append((f"{model} ({serial})", serial))
    return devices


def configure_interactive(args):
    android = None
    while choose("Where are the videos?", ["This Mac", "Android phone over USB"], default=1) == 1:
        try:
            devices = authorized_devices()
        except (OSError, RuntimeError) as error:
            print(f"Could not list Android devices. {error}")
            continue
        if not devices:
            print("No authorized phone found. Connect it, unlock it, and allow USB debugging.")
            continue
        labels = [label for label, _ in devices]
        selection = choose("Choose a phone", labels, default=1 if len(labels) == 1 else None)
        android = Android(devices[selection][1])
        break

    root = browse_folder(android)
    preset = choose("Choose a compression preset",
                    ["Family — HEVC, up to 1080p", "Course — H.264, up to 720p"], default=1)
    args.preset = ("family", "course")[preset]
    return root, android


def plan_jobs(root, android):
    files = android.files(root) if android else local_files(root)
    return list(candidates(files, android))


def check_media_tools():
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise RuntimeError(f"{executable} is required")


def run_job(source, destination, android, args, reason, progress=console_progress):
    if reason:
        return "skipped", reason
    try:
        report = compress(source, destination, android, args, progress)
        return "saved", f"{destination.name!r}. {report}"
    except SkipVideo as error:
        return "skipped", str(error)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return "failed", str(error)


def run_jobs(root, android, args, jobs, *, preview=False):
    if not preview:
        check_media_tools()
    summary = Counter()
    for index, (source, destination, reason) in enumerate(jobs, 1):
        print(f"[{index}/{len(jobs)}] {str(source.relative_to(root))!r}", flush=True)
        if preview and not reason:
            print(f"  Would create {str(destination.relative_to(root))!r}")
            summary["planned"] += 1
            continue
        status, detail = run_job(source, destination, android, args, reason)
        print(f"  {status.capitalize()}. {detail}",
              file=sys.stderr if status == "failed" else sys.stdout)
        summary[status] += 1
    print(", ".join(f"{count} {label}" for label, count in summary.items())
          or "No video files found.")
    return 1 if summary["failed"] else 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Run without arguments to open the interactive menu."
    )
    parser.add_argument("folder", nargs="?", help="Mac or absolute Android folder (direct files only)")
    parser.add_argument("--list-devices", action="store_true", help="list phones seen by ADB")
    parser.add_argument("--device", metavar="SERIAL", help="read and write on this ADB device")
    parser.add_argument("--preset", choices=("family", "course"), default="family")
    parser.add_argument("--dry-run", action="store_true", help="list planned outputs without encoding")
    parser.add_argument("--tui", action="store_true", help="launch the full-screen terminal interface")
    parser.add_argument("--quality", type=int, default=60, help="family HEVC quality, 1–100")
    parser.add_argument("--video-kbps", type=int, default=1000, help="course average video kbps")
    parser.add_argument("--min-savings", type=float, default=10,
                        help="minimum percentage savings, 0 disables the early bitrate check")
    args = parser.parse_args()
    interactive = len(sys.argv) == 1
    if not interactive and not args.list_devices and not args.folder and not args.tui:
        parser.error("a folder is required")
    if args.tui and (args.list_devices or args.dry_run):
        parser.error("--tui has its own device selection and preview")
    if not 1 <= args.quality <= 100 or args.video_kbps <= 0 or not 0 <= args.min_savings <= 100:
        parser.error("quality must be 1–100, video bitrate positive, and minimum savings 0–100")

    try:
        if args.tui:
            try:
                from .tui import VideoCompressionApp
            except ModuleNotFoundError as error:
                if error.name != "textual":
                    raise
                raise RuntimeError("Install the TUI extra with uv run --extra tui video-compress --tui") from None
            app = VideoCompressionApp(args)
            app.run()
            return app.return_code or 0
        if args.list_devices:
            print(capture(["adb", "devices", "-l"]).decode(), end="")
            return 0
        if interactive:
            root, android = configure_interactive(args)
        else:
            android = Android(args.device) if args.device else None
            try:
                root = folder_path(args.folder, android)
            except ValueError as error:
                parser.error(str(error))
        jobs = plan_jobs(root, android)
        if interactive:
            print(f"\nPreview for {str(root)!r} using the {args.preset} preset")
            run_jobs(root, android, args, jobs, preview=True)
            count = sum(reason is None for _, _, reason in jobs)
            if not count:
                return 0
            answer = input(f"\nStart compression for {count} candidate(s)? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                raise Cancelled
        return run_jobs(root, android, args, jobs, preview=args.dry_run)
    except (Cancelled, EOFError):
        print("\nCancelled. No encoding started.")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun the same command to skip completed outputs.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
