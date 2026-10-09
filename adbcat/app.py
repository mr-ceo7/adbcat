"""The adbcat Textual application."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from pathlib import Path

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Footer, Input, Label, Static

from .adb import (DEFAULT_BUFFERS, START_PROC_RE, FileSource, LogcatSource, Proc,
                  ProcessTracker, clear_device_log, list_devices)
from .config import Config
from .logview import BADGE, BG, MSG, LogView, name_style
from .model import KIND_LOG, LEVEL_CHARS, LEVEL_NAMES, Entry, LogStore, make_marker
from .query import (IS_VALUES, Query, QueryError, is_crash, quote, set_level,
                    toggle_term)
from .screens import (BufferPicker, ConfirmScreen, DevicePicker, ExportScreen,
                      FormatPicker, HelpScreen, InputScreen, SavedFilters)

KEY_SUGGESTIONS = ("tag:", "tag~:", "tag=:", "package:", "package~:", "process:",
                   "message:", "message~:", "line:", "level:", "age:", "is:",
                   "pid:", "tid:", "uid:")


class QuerySuggester(Suggester):
    """Inline completion for query keys and their values."""

    def __init__(self, app: "AdbcatApp") -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.owner = app

    async def get_suggestion(self, value: str) -> str | None:
        if not value or value[-1].isspace() or value.count('"') % 2:
            return None
        last = value.split()[-1]
        neg = "-" if last.startswith("-") else ""
        body = last[len(neg):]
        if ":" not in body:
            if len(body) < 1:
                return None
            for k in KEY_SUGGESTIONS:
                if k.startswith(body) and k != body:
                    return value + k[len(body):]
            return None
        key, _, val = body.partition(":")
        base = key.rstrip("~=")
        app = self.owner
        if base == "level":
            cands = [n for n in LEVEL_NAMES]
            val = val.upper()
        elif base == "is":
            cands = list(IS_VALUES)
        elif base == "package":
            cands = ["mine"] + [p for p, _ in app.pkg_counts.most_common()]
        elif base == "tag":
            cands = [t for t, _ in app.tag_counts.most_common()]
        elif base == "process":
            cands = sorted({p.name for p in app.procs.values()})
        elif base == "age":
            cands = ["30s", "1m", "5m", "15m", "1h"]
        else:
            return None
        for c in cands:
            if c.startswith(val) and c != val and " " not in c:
                return value[: len(value) - len(val)] + c
        return None


class HistoryInput(Input):
    """Input with shell-like up/down history."""

    BINDINGS = [
        Binding("up", "history(-1)", "Older", show=False),
        Binding("down", "history(1)", "Newer", show=False),
    ]

    def __init__(self, history: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.history = history
        self._pos: int | None = None
        self._draft = ""

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        if self._pos is None:
            if step > 0:
                return
            self._draft = self.value
            self._pos = len(self.history)
        self._pos += step
        if self._pos >= len(self.history):
            self._pos = None
            self.value = self._draft
        else:
            self._pos = max(0, self._pos)
            self.value = self.history[self._pos]
        self.cursor_position = len(self.value)

    def reset_history(self) -> None:
        self._pos = None


class AdbcatApp(App):
    TITLE = "adbcat"
    CSS = f"""
    Screen {{ background: {BG}; }}
    #filterbar, #findbar {{
        height: 1;
        background: #22252b;
    }}
    #filterbar Label, #findbar Label {{
        padding: 0 1;
        color: #7a8290;
        text-style: bold;
    }}
    #filterbar Input, #findbar Input {{
        border: none;
        height: 1;
        padding: 0 1;
        background: #1b1d21;
        width: 1fr;
    }}
    #filterbar Input:focus, #findbar Input:focus {{ background: #262a31; }}
    #filterbar Input.error {{ color: #ff7b72; }}
    #filter-count, #find-count {{ min-width: 18; text-align: right; }}
    #findbar {{ display: none; }}
    #main {{ height: 1fr; }}
    #stats {{
        width: 40;
        display: none;
        background: #22252b;
        border-left: vkey #3a3f4b;
        padding: 0 1;
    }}
    #detail {{
        height: 14;
        display: none;
        background: #22252b;
        border-top: hkey #3a3f4b;
        padding: 0 1;
    }}
    #status {{
        height: 1;
        background: #16181c;
        color: #a0a7b4;
    }}
    Footer {{ background: #16181c; }}
    """

    BINDINGS = [
        Binding("question_mark", "help", "Help"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("space", "pause", "Pause"),
        Binding("enter", "detail", "Detail", show=False),
        Binding("s", "stats", "Stats"),
        Binding("w", "wrap", "Wrap"),
        Binding("d", "devices", "Device"),
        Binding("b", "buffers", "Buffers", show=False),
        Binding("o", "format", "Columns", show=False),
        Binding("F", "saved", "Saved", show=False),
        Binding("ctrl+s", "export", "Export", priority=True),
        Binding("ctrl+o", "open_file", "Open", priority=True, show=False),
        Binding("c", "clear", "Clear", show=False),
        Binding("C", "clear_device", "Clear device", show=False),
        Binding("x", "clear_filter", "Clear filter", show=False),
        Binding("e", "next_error(1)", "Next error", show=False),
        Binding("E", "next_error(0)", "Prev error", show=False),
        Binding("n", "find_next(1)", "Next match", show=False),
        Binding("N", "find_next(0)", "Prev match", show=False),
        Binding("m", "bookmark", "Bookmark", show=False),
        Binding("right_square_bracket", "next_bookmark(1)", "Next bookmark", show=False),
        Binding("left_square_bracket", "next_bookmark(0)", "Prev bookmark", show=False),
        Binding("t", "filter_tag(1)", "Only tag", show=False),
        Binding("T", "filter_tag(0)", "Hide tag", show=False),
        Binding("p", "filter_package(1)", "Only package", show=False),
        Binding("P", "filter_package(0)", "Hide package", show=False),
        Binding("M", "set_mine", "Mine", show=False),
        Binding("y", "copy(0)", "Copy", show=False),
        Binding("Y", "copy(1)", "Copy trace", show=False),
        Binding("1", "level('V')", show=False),
        Binding("2", "level('D')", show=False),
        Binding("3", "level('I')", show=False),
        Binding("4", "level('W')", show=False),
        Binding("5", "level('E')", show=False),
        Binding("6", "level('F')", show=False),
        Binding("0", "level('')", show=False),
        Binding("escape", "escape", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, serial: str | None = None, buffers: tuple[str, ...] = DEFAULT_BUFFERS,
                 query: str = "", file: str | None = None, mine: list[str] | None = None,
                 tail: int | None = None, max_lines: int | None = None,
                 config: Config | None = None, banner: str | None = None) -> None:
        super().__init__()
        self.banner = banner
        self.config = config or Config()
        self.serial = serial
        self.buffers = buffers
        self.file = file
        self.tail = tail
        self.mine = list(mine or self.config["mine"])
        self.initial_query = query
        self.store = LogStore(max_lines or self.config["max_lines"])
        self.sink: deque[Entry] = deque()
        self.pending: list[Entry] = []
        self.paused = False
        self.source: LogcatSource | FileSource | None = None
        self.tracker: ProcessTracker | None = None
        self.procs: dict[int, Proc] = {}
        self.app_pids: set[int] = set()
        self._known_pkg: dict[int, str] = {}
        self._ps_ready = False
        self.query_obj = Query("")
        self.query_error = ""
        self.find_rx = None
        self.state = "starting"
        self.state_detail = ""
        self.device_label = ""
        self.level_counts = [0] * 6
        self.tag_counts: Counter[str] = Counter()
        self.pkg_counts: Counter[str] = Counter()
        self.crash_count = 0
        self.total_seen = 0
        self._rate_window: deque[tuple[float, int]] = deque(maxlen=20)
        self._filter_timer = None
        self._find_timer = None
        self._main_screen = None
        self._closing = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="filterbar"):
            yield Label("FILTER")
            yield HistoryInput(
                self.config["history"], id="filter",
                placeholder="package:mine level:W tag:ActivityManager -tag:chatty is:crash  (? for help)",
                suggester=QuerySuggester(self),
            )
            yield Label("", id="filter-count")
        with Horizontal(id="main"):
            yield LogView(self.store, id="log")
            yield Static(id="stats")
        with VerticalScroll(id="detail"):
            yield Static(id="detail-text")
        with Horizontal(id="findbar"):
            yield Label("FIND")
            yield Input(id="find", placeholder="text or /regex/   enter next   esc close")
            yield Label("", id="find-count")
        yield Static(id="status")
        yield Footer()

    def _q(self, selector: str, expect_type=None):
        """Query the main screen (App.query_one would search whichever dialog is on top)."""
        screen = self._main_screen or self.screen
        if expect_type is None:
            return screen.query_one(selector)
        return screen.query_one(selector, expect_type)

    @property
    def view(self) -> LogView:
        return self._view

    def on_mount(self) -> None:
        self._main_screen = self.screen
        self._view = view = self.screen.query_one("#log", LogView)
        view.columns.update(self.config["columns"])
        view.wrap = bool(self.config["wrap"])
        view.tag_width = int(self.config["tag_width"])
        view.package_width = int(self.config["package_width"])
        view.focus()
        if self.banner:
            self.notify(self.banner, title="Filtering to your app", timeout=6)
        if self.initial_query:
            self._q("#filter", Input).value = self.initial_query
            self.apply_filter(self.initial_query)
        self.set_interval(0.1, self._drain)
        self.set_interval(1.0, self._tick)
        if self.file:
            self.start_file(self.file)
        else:
            self.call_after_refresh(self._choose_initial_device)

    def _choose_initial_device(self) -> None:
        if self.serial:
            self.start_device(self.serial)
            return
        online = [d for d in list_devices() if d.state == "device"]
        if len(online) > 1:
            self.push_screen(DevicePicker(None), self._device_chosen_initial)
        else:
            self.start_device(online[0].serial if online else None)

    def _device_chosen_initial(self, serial: str | None) -> None:
        if serial is None:
            online = [d for d in list_devices() if d.state == "device"]
            serial = online[0].serial if online else None
        self.start_device(serial)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # keep the main-screen keys from firing underneath a dialog
        if isinstance(self.screen, ModalScreen) and action not in ("quit", "command_palette"):
            return False
        return True

    # ------------------------------------------------------------------ sources
    def stop_sources(self) -> None:
        if self.source:
            self.source.stop()
        if self.tracker:
            self.tracker.stop()
        self.source = None
        self.tracker = None
        self.sink.clear()
        self.pending.clear()

    def start_device(self, serial: str | None) -> None:
        self.stop_sources()
        self.file = None
        self.serial = serial
        self.procs = {}
        self.app_pids = set()
        self._known_pkg = {}
        self._ps_ready = False
        self.device_label = self._label_for(serial)
        self.clear_view()
        sink = deque()
        self.sink = sink
        self.source = LogcatSource(serial, self.buffers, sink,
                                   lambda s, d: self._thread_status(s, d, sink),
                                   tail=self.tail or self.store.max_lines)
        self.source.start()
        self.tracker = ProcessTracker(lambda: self.source.serial if isinstance(self.source, LogcatSource) else None,
                                      lambda procs, sink=sink: self._thread_ps(procs, sink))
        self.tracker.start()

    def start_file(self, path: str) -> None:
        self.stop_sources()
        self.file = path
        self.serial = None
        self.procs = {}
        self.app_pids = set()
        self.device_label = Path(path).name
        self.clear_view()
        sink = deque()
        self.sink = sink
        self.source = FileSource(path, sink, lambda s, d: self._thread_status(s, d, sink),
                                 max_lines=self.store.max_lines)
        self.source.start()

    @staticmethod
    def _label_for(serial: str | None) -> str:
        if not serial:
            return ""
        for d in list_devices():
            if d.serial == serial:
                return f"{d.label} ({serial})"
        return serial

    def _thread_status(self, state: str, detail: str, sink: deque) -> None:
        try:
            self.call_from_thread(self._set_status, state, detail, sink)
        except RuntimeError:
            pass

    def _set_status(self, state: str, detail: str, sink: deque) -> None:
        if sink is not self.sink:
            return  # a stale source we already stopped
        if state == "device":
            self.serial = detail
            self.device_label = self._label_for(detail)
            return
        if state == "error":
            self.notify(detail, title="logcat", severity="error", timeout=8)
        self.state = state
        self.state_detail = detail
        self._update_status()

    def _thread_ps(self, procs: dict[int, Proc], sink: deque) -> None:
        try:
            self.call_from_thread(self._on_ps, procs, sink)
        except RuntimeError:
            pass

    def _on_ps(self, procs: dict[int, Proc], sink: deque) -> None:
        if sink is not self.sink:
            return
        self.procs = procs
        new_apps = {pid for pid, p in procs.items() if p.is_app}
        if self._ps_ready:
            for pid in sorted(self.app_pids - new_apps):
                pkg = self._known_pkg.get(pid, "")
                self.sink.append(make_marker(f"PROCESS ENDED ({pid}) for package {pkg}",
                                             pid=pid, pkg=pkg or None, level=3))
            for pid in sorted(new_apps - self.app_pids):
                p = procs[pid]
                self.sink.append(make_marker(f"PROCESS STARTED ({pid}) for package {p.package}",
                                             pid=pid, pkg=p.package))
        self._known_pkg = {pid: procs[pid].package for pid in new_apps}
        self.app_pids = new_apps
        self._ps_ready = True
        # backfill entries that arrived before we knew their process
        changed = False
        entries = self.store.entries
        for e in entries[-30000:]:
            if e.proc is None and e.kind == KIND_LOG and e.pid in procs:
                self._annotate_proc(e, procs[e.pid])
                changed = True
        if changed:
            if self.query_obj.uses_package or "process" in self.query_obj.text:
                self.refilter()
            else:
                self.view.invalidate()

    @staticmethod
    def _annotate_proc(e: Entry, p: Proc) -> None:
        e.proc = p.name
        e.pkg = p.package if p.is_app else None

    # ------------------------------------------------------------------ ingest
    def _annotate(self, e: Entry) -> None:
        if e.kind != KIND_LOG:
            return
        p = self.procs.get(e.pid)
        if p is not None:
            self._annotate_proc(e, p)
        if e.tag == "ActivityManager" and e.msg.startswith("Start proc "):
            m = START_PROC_RE.match(e.msg)
            if m:
                pid = int(m.group(1))
                proc = Proc(pid, m.group(3), m.group(2))
                if pid not in self.procs or self.procs[pid].name != proc.name:
                    self.procs[pid] = proc
                    if self._ps_ready and proc.is_app and pid not in self.app_pids:
                        self.app_pids.add(pid)
                        self._known_pkg[pid] = proc.package
                        self.sink.append(make_marker(
                            f"PROCESS STARTED ({pid}) for package {proc.package}",
                            pid=pid, pkg=proc.package))

    def _drain(self) -> None:
        sink = self.sink
        if not sink or self._closing:
            return
        batch = []
        pop = sink.popleft
        while sink and len(batch) < 25000:
            batch.append(pop())
        for e in batch:
            self._annotate(e)
        if self.paused:
            self.pending.extend(batch)
            self._update_status()
            return
        self._ingest(batch)

    def _ingest(self, batch: list[Entry]) -> None:
        view = self.view
        follow = view.at_bottom
        q = self.query_obj
        if q.uses_age:
            q.refresh()
        match = q.matches
        store = self.store
        counts = self.level_counts
        tags = self.tag_counts
        pkgs = self.pkg_counts
        new = []
        for e in batch:
            store.append(e)
            if e.kind == KIND_LOG:
                counts[e.level] += 1
                tags[e.tag] += 1
                if e.pkg:
                    pkgs[e.pkg] += 1
                if e.level >= 4 and is_crash(e) and ("FATAL EXCEPTION" in e.msg or "Fatal signal" in e.msg):
                    self.crash_count += 1
            if match(e):
                new.append(e.seq)
        self.total_seen += len(batch)
        self._rate_window.append((time.monotonic(), self.total_seen))
        if store.trim():
            view.drop_before(store.base)
        view.append(new)
        if follow:
            view.scroll_to_bottom()
        self._update_counts()

    def refilter(self) -> None:
        view = self.view
        anchor = view._anchor()
        q = self.query_obj
        q.refresh()
        match = q.matches
        seqs = [e.seq for e in self.store.entries if match(e)]
        view.highlights = q.highlights
        view.set_filtered(seqs)
        view._restore_anchor(anchor)
        if view.selected is not None:
            idx = view.index_of(view.selected)
            if idx is not None:
                view.ensure_visible(idx, center=True)
        self._update_counts()

    def clear_view(self) -> None:
        self.store.clear()
        self.pending.clear()
        self.level_counts = [0] * 6
        self.tag_counts.clear()
        self.pkg_counts.clear()
        self.crash_count = 0
        view = self.view
        view.clear_selection()
        view.set_filtered([])
        self._update_counts()
        self._render_detail(None)

    # ------------------------------------------------------------------ filter
    def apply_filter(self, text: str) -> bool:
        inp = self._q("#filter", Input)
        try:
            q = Query(text, self.mine)
        except QueryError as err:
            self.query_error = str(err)
            inp.add_class("error")
            self._update_status()
            return False
        inp.remove_class("error")
        self.query_error = ""
        if q.uses_mine and not self.mine:
            self.notify("package:mine is empty - start with -p <package> or press M on a line",
                        severity="warning")
        self.query_obj = q
        self.refilter()
        self._update_status()
        return True

    def set_query(self, text: str) -> None:
        inp = self._q("#filter", HistoryInput)
        inp.value = text
        inp.cursor_position = len(text)
        if self._filter_timer:
            self._filter_timer.stop()
        if self.apply_filter(text):
            self.config.add_history(text)
            self.config.save()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        if self._filter_timer:
            self._filter_timer.stop()
        value = event.value
        if value.strip() == self.query_obj.text and not self.query_error:
            return
        self._filter_timer = self.set_timer(0.2, lambda: self.apply_filter(value))

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        if self._filter_timer:
            self._filter_timer.stop()
        if self.apply_filter(event.value):
            self.config.add_history(event.value)
            self.config.save()
            self._q("#filter", HistoryInput).reset_history()
            self.view.focus()

    # ------------------------------------------------------------------ find
    def action_find(self) -> None:
        bar = self._q("#findbar")
        bar.display = True
        self._q("#find", Input).focus()

    def _compile_find(self, text: str):
        import re
        if not text:
            return None
        flags = 0 if any(c.isupper() for c in text) else re.I
        if len(text) > 2 and text.startswith("/") and text.endswith("/"):
            try:
                return re.compile(text[1:-1], flags)
            except re.error:
                return None
        return re.compile(re.escape(text), flags)

    @on(Input.Changed, "#find")
    def _find_changed(self, event: Input.Changed) -> None:
        if self._find_timer:
            self._find_timer.stop()
        self._find_timer = self.set_timer(0.15, lambda: self._apply_find(event.value))

    def _apply_find(self, text: str) -> None:
        rx = self._compile_find(text)
        self.find_rx = rx
        view = self.view
        view.find_rx = rx
        view.invalidate()
        label = self._q("#find-count", Label)
        if rx is None:
            label.update("" if not text else Text("bad regex", style="#ff7b72"))
            return
        n = sum(1 for e in view.visible_entries() if rx.search(e.msg) or rx.search(e.tag))
        label.update(f"{n:,} lines")

    @on(Input.Submitted, "#find")
    def _find_submitted(self, event: Input.Submitted) -> None:
        if self._find_timer:
            self._find_timer.stop()
        self._apply_find(event.value)
        self.action_find_next(1)

    def action_find_next(self, forward: int) -> None:
        rx = self.find_rx
        if rx is None:
            self.notify("Nothing to find - press Ctrl+F", severity="warning")
            return
        if not self.view.jump(lambda e: bool(rx.search(e.msg) or rx.search(e.tag)), bool(forward)):
            self.notify("No more matches", timeout=2)

    def _close_find(self) -> None:
        self._q("#findbar").display = False
        self._q("#find", Input).value = ""
        self.find_rx = None
        self.view.find_rx = None
        self.view.invalidate()
        self.view.focus()

    # ------------------------------------------------------------------ actions
    def action_escape(self) -> None:
        focused = self.focused
        if focused is not None and focused.id == "find":
            self._close_find()
        elif isinstance(focused, Input):
            self.view.focus()
        elif self._q("#detail").display:
            self._q("#detail").display = False
        else:
            self.view.clear_selection()

    def action_focus_filter(self) -> None:
        self._q("#filter", Input).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused and self.pending:
            batch, self.pending = self.pending, []
            self._ingest(batch)
        self._update_status()

    def action_detail(self) -> None:
        pane = self._q("#detail")
        if not pane.display and self.view.selected is None:
            self.view.action_move(0)
        pane.display = not pane.display
        if pane.display:
            self._render_detail(self._selected_entry())

    def action_stats(self) -> None:
        panel = self._q("#stats")
        panel.display = not panel.display
        if panel.display:
            self._render_stats()

    def action_wrap(self) -> None:
        self.view.set_format(self.view.columns, not self.view.wrap)
        self.config["wrap"] = self.view.wrap
        self.config.save()
        self._update_status()

    def action_format(self) -> None:
        def done(result) -> None:
            if result:
                columns, wrap = result
                self.view.set_format(columns, wrap)
                self.config["columns"] = columns
                self.config["wrap"] = wrap
                self.config.save()
                self._update_status()
        self.push_screen(FormatPicker(self.view.columns, self.view.wrap), done)

    def action_devices(self) -> None:
        def done(serial: str | None) -> None:
            if serial:
                self.start_device(serial)
        current = self.source.serial if isinstance(self.source, LogcatSource) else None
        self.push_screen(DevicePicker(current), done)

    def action_buffers(self) -> None:
        def done(buffers) -> None:
            if buffers and buffers != self.buffers:
                self.buffers = buffers
                if not self.file:
                    self.start_device(self.serial)
        self.push_screen(BufferPicker(self.buffers), done)

    def action_saved(self) -> None:
        saved = self.config["saved"]

        def done(query: str | None) -> None:
            self.config.save()
            if query is not None:
                self.set_query(query)
        self.push_screen(SavedFilters(saved, self.query_obj.text), done)

    def action_export(self) -> None:
        name = self.serial or (Path(self.file).stem if self.file else "logcat")
        default = f"~/adbcat-{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        shown = len(self.view.filtered)

        def done(result) -> None:
            if result:
                path, filtered = result
                self._export(os.path.expanduser(path), filtered)
        self.push_screen(ExportScreen(default, shown, len(self.store)), done)

    def _export(self, path: str, filtered: bool) -> None:
        entries = self.view.visible_entries() if filtered else list(self.store.entries)
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(e.raw + "\n")
        except OSError as err:
            self.notify(str(err), title="Export failed", severity="error")
            return
        self.notify(f"Wrote {len(entries):,} lines to {path}", title="Exported")

    def action_open_file(self) -> None:
        def done(path: str | None) -> None:
            if not path:
                return
            path = os.path.expanduser(path)
            if not os.path.isfile(path):
                self.notify(f"No such file: {path}", severity="error")
                return
            self.start_file(path)
        self.push_screen(InputScreen("OPEN LOG FILE", "", "path to a saved logcat file"), done)

    def action_clear(self) -> None:
        self.clear_view()
        self.notify("Screen cleared", timeout=2)

    def action_clear_device(self) -> None:
        if not isinstance(self.source, LogcatSource) or not self.source.serial:
            self.notify("No device connected", severity="warning")
            return
        serial = self.source.serial

        def done(ok: bool | None) -> None:
            if ok:
                self._clear_device(serial)
        self.push_screen(ConfirmScreen(
            f"Clear the {', '.join(self.buffers)} log buffers on {serial}?"), done)

    @work(thread=True)
    def _clear_device(self, serial: str) -> None:
        err = clear_device_log(serial, self.buffers)
        if err:
            self.call_from_thread(self.notify, err, title="adb logcat -c", severity="error")
        else:
            self.call_from_thread(self.clear_view)
            self.call_from_thread(self.notify, "Device log cleared", timeout=2)

    def action_clear_filter(self) -> None:
        self.set_query("")

    def action_level(self, ch: str) -> None:
        self.set_query(set_level(self.query_obj.text, ch or None))

    def _selected_entry(self) -> Entry | None:
        sel = self.view.selected
        return self.store.get(sel) if sel is not None else None

    def _need_selection(self) -> Entry | None:
        e = self._selected_entry()
        if e is None:
            self.notify("Select a line first (↑ ↓ or click)", severity="warning", timeout=2)
        return e

    def action_filter_tag(self, include: int) -> None:
        e = self._need_selection()
        if e is None or not e.tag:
            return
        term = f"{'' if include else '-'}tag=:{quote(e.tag)}"
        self.set_query(toggle_term(self.query_obj.text, term))

    def action_filter_package(self, include: int) -> None:
        e = self._need_selection()
        if e is None:
            return
        if e.pkg:
            term = f"{'' if include else '-'}package=:{quote(e.pkg)}"
        elif e.proc:
            term = f"{'' if include else '-'}process=:{quote(e.proc)}"
        elif e.pid:
            term = f"{'' if include else '-'}pid:{e.pid}"
        else:
            return
        self.set_query(toggle_term(self.query_obj.text, term))

    def action_set_mine(self) -> None:
        e = self._need_selection()
        if e is None:
            return
        if not e.pkg:
            self.notify("That line has no app package", severity="warning")
            return
        self.mine = [e.pkg]
        self.config["mine"] = self.mine
        self.config.save()
        self.notify(f"package:mine = {e.pkg}")
        if self.query_obj.uses_mine:
            self.apply_filter(self.query_obj.text)
        else:
            self.set_query(toggle_term(self.query_obj.text, "package:mine"))

    def action_bookmark(self) -> None:
        e = self._need_selection()
        if e is None:
            return
        e.bookmark = not e.bookmark
        self.view.invalidate()

    def action_next_bookmark(self, forward: int) -> None:
        if not self.view.jump(lambda e: e.bookmark, bool(forward)):
            self.notify("No more bookmarks (m to add one)", timeout=2)

    def action_next_error(self, forward: int) -> None:
        if not self.view.jump(lambda e: e.level >= 4, bool(forward)):
            self.notify("No more errors", timeout=2)

    def action_copy(self, whole: int) -> None:
        e = self._need_selection()
        if e is None:
            return
        lines = self._group(e) if whole else [e]
        text = "\n".join(x.raw for x in lines)
        self.copy_to_clipboard(text)
        self._system_clipboard(text)
        self.notify(f"Copied {len(lines)} line{'s' if len(lines) != 1 else ''}", timeout=2)

    @staticmethod
    def _system_clipboard(text: str) -> None:
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            cmd = ["wl-copy"]
        elif os.environ.get("DISPLAY") and shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        else:
            return

        def run() -> None:
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ detail / stats
    def _group(self, e: Entry) -> list[Entry]:
        """The entry plus its neighbours from the same log call (stack traces)."""
        if e.kind != KIND_LOG:
            return [e]
        key = (e.pid, e.tid, e.tag, e.level, e.ts)
        store = self.store
        before: list[Entry] = []
        after: list[Entry] = []
        for step, out in ((-1, before), (1, after)):
            miss = 0
            seq = e.seq + step
            while miss < 200:
                x = store.get(seq)
                if x is None:
                    break
                if (x.pid, x.tid, x.tag, x.level, x.ts) == key:
                    out.append(x)
                    miss = 0
                else:
                    miss += 1
                seq += step
        before.reverse()
        return before + [e] + after

    @on(LogView.SelectionChanged)
    def _selection_changed(self, event: LogView.SelectionChanged) -> None:
        if self._q("#detail").display:
            self._render_detail(event.entry)

    @on(LogView.Activated)
    def _activated(self) -> None:
        self.action_detail()

    def _render_detail(self, e: Entry | None) -> None:
        text = self._q("#detail-text", Static)
        if e is None:
            text.update(Text("No line selected. Use ↑ ↓ or click a line.", style="italic #7a8290"))
            return
        group = self._group(e)
        t = Text()
        t.append(f" {LEVEL_CHARS[e.level]} ", BADGE[e.level])
        t.append(f" {LEVEL_NAMES[e.level]}  ", "bold")
        t.append(e.tag or "-", name_style(e.tag))
        t.append("   ")
        name = e.pkg or e.proc
        if name:
            t.append(name, name_style(name))
            t.append("   ")
        t.append(f"pid {e.pid}  tid {e.tid}", "#7a8290")
        if e.uid:
            t.append(f"  uid {e.uid}", "#7a8290")
        t.append(f"   {e.ts}", "#7a8290")
        if len(group) > 1:
            t.append(f"   {len(group)} lines", "bold #e0b000")
        t.append("\n\n")
        for x in group:
            st = MSG[x.level]
            if x is e:
                st = st + Style(bold=True, underline=True)
            t.append(x.msg + "\n", st)
        text.update(t)
        self._q("#detail", VerticalScroll).scroll_home(animate=False)

    def _rate(self) -> float:
        w = self._rate_window
        now = time.monotonic()
        while w and now - w[0][0] > 3:
            w.popleft()
        if len(w) < 2:
            return 0.0
        (t0, n0), (_, n1) = w[0], w[-1]
        return (n1 - n0) / (now - t0) if now > t0 else 0.0

    def _render_stats(self) -> None:
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        t.add_column(justify="right", no_wrap=True)
        hdr = "bold #e0b000"
        t.add_row(Text("TRAFFIC", hdr), "")
        t.add_row("Rate", f"{self._rate():,.0f} lines/s")
        t.add_row("Seen", f"{self.total_seen:,}")
        t.add_row("Buffered", f"{len(self.store):,}")
        t.add_row("Shown", f"{len(self.view.filtered):,}")
        if self.store.dropped:
            t.add_row("Rolled off", f"{self.store.dropped:,}")
        t.add_row(Text("Crashes", "#ff7b72" if self.crash_count else ""), str(self.crash_count))
        t.add_row("", "")
        t.add_row(Text("LEVELS", hdr), "")
        total = max(1, sum(self.level_counts))
        for i, n in enumerate(self.level_counts):
            bar = "█" * round(14 * n / total)
            row = Text()
            row.append(f" {LEVEL_CHARS[i]} ", BADGE[i])
            row.append(" " + bar, MSG[i])
            t.add_row(row, f"{n:,}")
        t.add_row("", "")
        t.add_row(Text("TOP TAGS", hdr), "")
        for tag, n in self.tag_counts.most_common(10):
            t.add_row(Text(tag, name_style(tag)), f"{n:,}")
        if self.pkg_counts:
            t.add_row("", "")
            t.add_row(Text("TOP PACKAGES", hdr), "")
            for pkg, n in self.pkg_counts.most_common(8):
                t.add_row(Text(pkg, name_style(pkg)), f"{n:,}")
        self._q("#stats", Static).update(t)

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self._tick_inner()
        except NoMatches:
            pass  # screen is being torn down

    def _tick_inner(self) -> None:
        if self._q("#stats").display:
            self._render_stats()
        if self.query_obj.uses_age:
            self.refilter()
        self._update_status()

    # ------------------------------------------------------------------ status
    def _update_counts(self) -> None:
        shown = len(self.view.filtered)
        total = len(self.store)
        label = f"{shown:,} / {total:,}" if not self.query_obj.empty else f"{total:,} lines"
        self._q("#filter-count", Label).update(label)

    def _update_status(self) -> None:
        t = Text()
        if self.paused:
            t.append(f" PAUSED +{len(self.pending):,} ", "bold #111111 on #e0b000")
        elif self.state == "live":
            t.append(" LIVE ", "bold #ffffff on #3f8a2e")
        elif self.state == "file":
            t.append(" FILE ", "bold #ffffff on #2f6fbf")
        elif self.state == "error":
            t.append(" ERROR ", "bold #ffffff on #d03c3c")
        elif self.state == "loading":
            t.append(" LOADING ", "bold #ffffff on #5c6370")
        else:
            t.append(" WAITING ", "bold #ffffff on #5c6370")
        t.append(" ")
        if self.state in ("waiting", "error"):
            t.append(self.state_detail, "#e0b000")
        else:
            t.append(self.device_label or self.state_detail or "-", "bold #c8ccd4")
        if not self.file:
            t.append("  " + ",".join(self.buffers), "#7a8290")
        t.append("  │  ", "#3a3f4b")
        t.append(f"{self._rate():,.0f}/s", "#a0a7b4")
        view = self.view
        if not view.at_bottom and not self.paused:
            t.append("  │  ", "#3a3f4b")
            t.append("SCROLLED (End to follow)", "#79b8ff")
        if view.wrap:
            t.append("  │  wrap", "#7a8290")
        if self.mine:
            t.append("  │  mine: " + ",".join(self.mine), "#7a8290")
        if self.query_error:
            t.append("  │  ", "#3a3f4b")
            t.append(self.query_error, "#ff7b72")
        self._q("#status", Static).update(t)

    async def action_quit(self) -> None:
        self._closing = True
        self.stop_sources()
        await super().action_quit()

    def on_unmount(self) -> None:
        self._closing = True
        self.stop_sources()
        self.config.save()
