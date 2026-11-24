"""Optional full-screen interface for browsing and compressing video folders."""

import asyncio
from collections import Counter
from pathlib import Path
from threading import Event

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, OptionList, ProgressBar, RichLog, Select, Static
from rich.text import Text

from . import cli


def open_folder(serial, value):
    android = cli.Android(serial) if serial else None
    return cli.folder_path(value, android), android


def scan_folder(serial, value):
    root, android = open_folder(serial, value)
    return root, android, cli.plan_jobs(root, android)


class FolderOptions(OptionList):
    BINDINGS = [Binding("space", "select", "Select folder")]


class FolderBrowser(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Back"),
        Binding("right", "open_child", "Open highlighted folder"),
        Binding("left", "parent_folder", "Go up"),
    ]
    CSS = """
    FolderBrowser { align: center middle; }
    #folder-dialog {
        width: 90%; max-width: 90; height: 85%;
        border: round $primary; background: $surface; padding: 0 1;
    }
    #browser-path { height: 2; color: $accent; }
    #folders { height: 1fr; }
    #browser-actions { height: 3; }
    #browser-status { height: 1; color: $text-muted; }
    """

    def __init__(self, root, android):
        super().__init__()
        self.root = root
        self.android = android
        self.folders = []

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-dialog"):
            yield Static(id="browser-path", markup=False)
            yield FolderOptions(id="folders")
            yield Static(id="browser-status", markup=False)
            with Horizontal(id="browser-actions"):
                yield Button("Use current folder", id="use-folder", variant="primary", disabled=True)
                yield Button("Open", id="open-folder", disabled=True)
                yield Button("Go up", id="up-folder")
                yield Button("Back", id="close-browser")

    def on_mount(self):
        self.load_folders()

    @work(exclusive=True)
    async def load_folders(self):
        options = self.query_one("#folders", OptionList)
        status = self.query_one("#browser-status", Static)
        use = self.query_one("#use-folder", Button)
        open_button = self.query_one("#open-folder", Button)
        self.query_one("#browser-path", Static).update(repr(str(self.root)))
        self.query_one("#up-folder", Button).disabled = self.root == self.root.parent
        options.clear_options()
        use.disabled = True
        open_button.disabled = True
        status.update("Reading folders…")
        try:
            self.folders = await asyncio.to_thread(cli.child_folders, self.root, self.android)
        except (OSError, RuntimeError) as error:
            status.update(f"Could not open folder. {error}")
            return
        options.add_options(Text(repr(folder.name)) for folder in self.folders)
        use.disabled = False
        if self.folders:
            open_button.disabled = False
            options.highlighted = 0
            options.focus()
            status.update("Enter / Space selects · → opens · ← goes up")
        else:
            use.focus()
            status.update("No subfolders.")

    @on(OptionList.OptionSelected, "#folders")
    def select_subfolder(self, event):
        self.dismiss(str(self.folders[event.option_index]))

    def action_open_child(self):
        highlighted = self.query_one("#folders", OptionList).highlighted
        if highlighted is not None:
            self.root = self.folders[highlighted]
            self.load_folders()

    def action_parent_folder(self):
        if self.root != self.root.parent:
            self.root = self.root.parent
            self.load_folders()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "use-folder":
            self.dismiss(str(self.root))
        elif event.button.id == "open-folder":
            self.action_open_child()
        elif event.button.id == "up-folder":
            self.action_parent_folder()
        else:
            self.dismiss(None)


class VideoCompressionApp(App):
    TITLE = "Video Compression"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
    ]
    CSS = """
    Screen { background: $surface; }
    #workspace { padding: 0 2; height: 1fr; }
    .row { height: 3; }
    #source, #preset, #folder { width: 1fr; }
    #source, #folder { margin-right: 1; }
    #refresh { margin-right: 1; }
    #actions Button { margin-right: 1; }
    #current { height: 1; margin-top: 1; text-style: bold; color: $accent; }
    #stage { height: 1; color: $text-muted; }
    #encode-progress { width: 1fr; margin-bottom: 1; }
    #encode-progress Bar { width: 1fr; }
    #results { height: 1fr; min-height: 2; border: round $primary; padding: 0 1; }
    """

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.serial = args.device or ""
        self.plan = None
        self.activity = None
        self.stop_requested = Event()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="workspace"):
            sources = [("This Mac", "")]
            if self.serial:
                sources.append((f"Android ({self.serial})", self.serial))
            with Horizontal(classes="row"):
                yield Select(sources, value=self.serial, allow_blank=False, id="source")
                yield Button("Refresh devices", id="refresh")
                yield Select(
                    [("Family · HEVC 1080p", "family"), ("Course · H.264 720p", "course")],
                    value=self.args.preset, allow_blank=False, id="preset",
                )
            with Horizontal(classes="row"):
                default_path = "/sdcard" if self.serial else str(Path.home())
                yield Input(self.args.folder or default_path, placeholder="Folder path", id="folder")
                yield Button("Browse", id="browse")
            with Horizontal(id="actions", classes="row"):
                yield Button("Preview", id="preview", variant="primary")
                yield Button("Start", id="start", variant="success", disabled=True)
                yield Button("Stop", id="stop", variant="error", disabled=True)
            yield Static("Current video", id="current", markup=False)
            yield Static("Choose a folder. Only videos directly inside it are included.",
                         id="stage", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="encode-progress")
            yield RichLog(id="results", wrap=True, markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#results").border_title = "Preview and results"
        self.query_one("#folder", Input).focus()
        self.refresh_devices()

    @work(exclusive=True, group="devices")
    async def refresh_devices(self, *, show_status=False):
        try:
            devices = await asyncio.to_thread(cli.authorized_devices)
        except (OSError, RuntimeError) as error:
            if show_status:
                self.set_stage(f"Could not read Android devices. {error}")
            return
        selected = self.query_one("#source", Select)
        sources = [("This Mac", ""), *devices]
        current = selected.value
        if current and all(value != current for _, value in devices):
            sources.append((f"Android unavailable ({current})", current))
        selected.set_options(sources)
        selected.value = current
        if show_status:
            self.set_stage(f"{len(devices)} authorized Android device(s) found." if devices else
                           "No authorized Android device. Connect and unlock the phone.")

    def set_stage(self, stage, percent=0):
        self.query_one("#stage", Static).update(stage)
        self.query_one(ProgressBar).update(total=None if percent is None else 100,
                                           progress=0 if percent is None else percent)

    def set_current(self, name):
        self.query_one("#current", Static).update(name)

    def log_line(self, text, style=""):
        self.query_one("#results", RichLog).write(Text(text, style=style))

    def set_activity(self, activity):
        self.activity = activity
        busy = activity is not None
        for selector in ("#source", "#preset", "#folder", "#refresh", "#browse", "#preview"):
            self.query_one(selector).disabled = busy
        self.query_one("#start", Button).disabled = busy or self.plan is None
        self.query_one("#stop", Button).disabled = activity != "running"

    def invalidate_preview(self):
        if self.plan is not None:
            self.plan = None
            self.query_one("#start", Button).disabled = True
            self.set_stage("Selection changed. Preview again before starting.")

    def on_select_changed(self, event):
        if event.value != event.select.value:
            return
        if event.select.id == "source" and event.value != self.serial:
            self.serial = event.value
            self.query_one("#folder", Input).value = "/sdcard" if self.serial else str(Path.home())
            self.invalidate_preview()
        elif event.select.id == "preset":
            self.invalidate_preview()

    @on(Input.Changed, "#folder")
    def folder_changed(self):
        self.invalidate_preview()

    @on(Input.Submitted, "#folder")
    def preview_path(self):
        if self.activity is None:
            self.preview()

    @work(exclusive=True, group="navigation")
    async def browse(self):
        value = self.query_one("#folder", Input).value
        self.set_activity("browsing")
        try:
            root, android = await asyncio.to_thread(open_folder, self.serial, value)
            result = await self.push_screen_wait(FolderBrowser(root, android))
            if result is not None:
                self.query_one("#folder", Input).value = result
        except (OSError, RuntimeError, ValueError) as error:
            self.set_stage(f"Could not open folder. {error}")
        finally:
            self.set_activity(None)

    @work(exclusive=True, group="navigation")
    async def preview(self):
        self.plan = None
        self.set_activity("previewing")
        self.set_current("Current video")
        self.set_stage("Scanning for videos…", None)
        self.query_one("#results", RichLog).clear()
        value = self.query_one("#folder", Input).value
        preset = self.query_one("#preset", Select).value
        try:
            if not value:
                raise ValueError("Enter or browse to a folder first.")
            root, android, jobs = await asyncio.to_thread(scan_folder, self.serial, value)
            self.log_line(f"{str(root)!r} · {preset} preset", "bold")
            count = 0
            for source, destination, reason in jobs:
                name = str(source.relative_to(root))
                if reason:
                    self.log_line(f"Skip {name!r} · {reason}", "dim")
                else:
                    self.log_line(f"{name!r} → {str(destination.relative_to(root))!r}")
                    count += 1
            if count:
                self.plan = root, android, preset, jobs
            if jobs:
                self.set_stage(f"{count} ready · {len(jobs) - count} skipped")
            else:
                self.set_stage("No video files directly inside this folder.")
        except (OSError, RuntimeError, ValueError) as error:
            self.set_stage("Could not preview this folder.")
            self.log_line(str(error), "red")
        finally:
            self.set_activity(None)

    def on_button_pressed(self, event):
        action = event.button.id
        if action == "stop":
            self.action_stop()
        elif self.activity is None:
            if action == "refresh":
                self.refresh_devices(show_status=True)
            elif action == "browse":
                self.browse()
            elif action == "preview":
                self.preview()
            elif action == "start" and self.plan:
                plan = self.plan
                self.plan = None
                self.stop_requested.clear()
                self.set_activity("running")
                self.compress_plan(plan)

    @work(thread=True, group="compression")
    def compress_plan(self, plan):
        root, android, preset, jobs = plan
        self.args.preset = preset
        summary = Counter()
        outcome = "Finished"

        def progress(stage, percent=None):
            if self.stop_requested.is_set():
                raise cli.Cancelled
            self.call_from_thread(self.set_stage, stage, percent)

        try:
            cli.check_media_tools()
            pending = sum(reason is None for _, _, reason in jobs)
            index = 0
            for source, destination, reason in jobs:
                if self.stop_requested.is_set():
                    raise cli.Cancelled
                if not reason:
                    index += 1
                    name = str(source.relative_to(root))
                    self.call_from_thread(self.set_current, f"{index}/{pending}  {name!r}")
                status, detail = cli.run_job(source, destination, android, self.args, reason, progress)
                summary[status] += 1
                if not reason:
                    style = {"saved": "green", "skipped": "yellow", "failed": "red"}[status]
                    self.call_from_thread(self.log_line, f"{status.capitalize()} {source.name!r} · {detail}",
                                          style)
            if self.stop_requested.is_set():
                outcome = "Stopped"
        except cli.Cancelled:
            outcome = "Stopped"
        except (OSError, RuntimeError) as error:
            outcome = "Could not start compression"
            self.call_from_thread(self.log_line, str(error), "red")
        finally:
            self.call_from_thread(self.finish_run, outcome, summary)

    def finish_run(self, outcome, summary):
        counts = " · ".join(f"{summary[key]} {key}" for key in ("saved", "skipped", "failed"))
        self.set_stage(f"{outcome} · {counts}", 100 if outcome == "Finished" else 0)
        self.set_activity(None)

    def action_stop(self):
        if self.activity == "running":
            self.stop_requested.set()
            self.query_one("#stop", Button).disabled = True
            self.set_stage("Stopping after the active operation…", None)

    def action_request_quit(self):
        if self.activity == "running":
            self.action_stop()
        else:
            self.exit()
