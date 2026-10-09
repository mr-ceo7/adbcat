"""Modal dialogs."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Input, Label, OptionList, RadioButton, RadioSet,
                             SelectionList, Static)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from .adb import ALL_BUFFERS, Device, list_devices

MODAL_CSS = """
ModalScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.55);
}
.dialog {
    width: 72;
    max-width: 95%;
    height: auto;
    max-height: 90%;
    border: tall #3a3f4b;
    background: #22252b;
    padding: 1 2;
}
.dialog .title {
    text-style: bold;
    color: #e6e6e6;
    margin-bottom: 1;
}
.dialog .hint {
    color: #7a8290;
    margin-top: 1;
}
.dialog OptionList, .dialog SelectionList {
    height: auto;
    max-height: 20;
    background: #1b1d21;
}
.dialog .buttons {
    height: auto;
    margin-top: 1;
    align-horizontal: right;
}
.dialog .buttons Button {
    margin-left: 2;
}
.dialog Input {
    margin-bottom: 1;
}
"""


class _Dialog(ModalScreen):
    DEFAULT_CSS = MODAL_CSS
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class DevicePicker(_Dialog):
    BINDINGS = [Binding("r", "refresh", "Refresh")]

    def __init__(self, current: str | None) -> None:
        super().__init__()
        self.current = current
        self.devices: list[Device] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("SELECT DEVICE", classes="title")
            yield OptionList(id="devices")
            yield Label("enter select   r refresh   esc cancel", classes="hint")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.devices = list_devices()
        ol = self.query_one(OptionList)
        ol.clear_options()
        if not self.devices:
            ol.add_option(Option(Text("No devices found. Connect one and press r.",
                                      style="italic #7a8290"), id="__none__", disabled=True))
            return
        for d in self.devices:
            t = Text()
            t.append("* " if d.serial == self.current else "  ", style="bold #4fc1ff")
            t.append(f"{d.label:<22}", style="bold")
            t.append(f" {d.serial:<24}", style="#a0a7b4")
            t.append(f" {d.state}", style="#a6d189" if d.state == "device" else "#ff7b72")
            ol.add_option(Option(t, id=d.serial))
        ol.focus()

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id != "__none__":
            self.dismiss(event.option.id)


class BufferPicker(_Dialog):
    BINDINGS = [Binding("enter", "apply", "Apply", priority=True)]

    def __init__(self, current: tuple[str, ...]) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("LOG BUFFERS", classes="title")
            yield SelectionList[str](*[Selection(b, b, b in self.current) for b in ALL_BUFFERS])
            yield Label("space toggle   enter apply   esc cancel\n"
                        "kernel usually needs a rooted device", classes="hint")

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def action_apply(self) -> None:
        chosen = tuple(b for b in ALL_BUFFERS if b in self.query_one(SelectionList).selected)
        if not chosen:
            self.notify("Pick at least one buffer", severity="warning")
            return
        self.dismiss(chosen)


COLUMN_LABELS = {
    "date": "Date", "time": "Time", "pid": "PID-TID", "uid": "UID",
    "package": "Package / process", "tag": "Tag",
}


class FormatPicker(_Dialog):
    BINDINGS = [Binding("enter", "apply", "Apply", priority=True)]

    def __init__(self, columns: dict, wrap: bool) -> None:
        super().__init__()
        self.columns = columns
        self.wrap = wrap

    def compose(self) -> ComposeResult:
        items = [Selection(label, key, bool(self.columns.get(key))) for key, label in COLUMN_LABELS.items()]
        items.append(Selection("Soft wrap long messages", "wrap", self.wrap))
        with Vertical(classes="dialog"):
            yield Label("DISPLAY COLUMNS", classes="title")
            yield SelectionList[str](*items)
            yield Label("space toggle   enter apply   esc cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def action_apply(self) -> None:
        sel = set(self.query_one(SelectionList).selected)
        self.dismiss(({k: k in sel for k in COLUMN_LABELS}, "wrap" in sel))


class SavedFilters(_Dialog):
    """Pick, add or delete saved filter queries. Dismisses with a query string to apply."""

    BINDINGS = [Binding("delete,x", "delete", "Delete")]

    def __init__(self, saved: list[dict], current: str) -> None:
        super().__init__()
        self.saved = saved  # mutated in place; the caller persists it
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("SAVED FILTERS", classes="title")
            yield OptionList(id="saved")
            yield Input(placeholder="Name for the current filter", id="name")
            with Horizontal(classes="buttons"):
                yield Button("Save current filter", id="save", variant="primary")
            yield Label("enter apply   x delete   esc close", classes="hint")

    def on_mount(self) -> None:
        self._reload()
        if not self.current:
            self.query_one("#save", Button).disabled = True
            self.query_one("#name", Input).disabled = True

    def _reload(self) -> None:
        ol = self.query_one(OptionList)
        ol.clear_options()
        if not self.saved:
            ol.add_option(Option(Text("Nothing saved yet.", style="italic #7a8290"),
                                 id="__none__", disabled=True))
            self.query_one("#name", Input).focus()
            return
        for i, item in enumerate(self.saved):
            t = Text()
            t.append(f"{item['name']:<20}", style="bold")
            t.append(" " + item["query"], style="#a0a7b4")
            ol.add_option(Option(t, id=str(i)))
        ol.focus()

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id != "__none__":
            self.dismiss(self.saved[int(event.option.id)]["query"])

    @on(Button.Pressed, "#save")
    @on(Input.Submitted, "#name")
    def _save(self) -> None:
        if not self.current:
            return
        name = self.query_one("#name", Input).value.strip() or self.current[:20]
        self.saved[:] = [s for s in self.saved if s["name"] != name]
        self.saved.append({"name": name, "query": self.current})
        self.query_one("#name", Input).value = ""
        self.notify(f"Saved filter '{name}'")
        self._reload()

    def action_delete(self) -> None:
        ol = self.query_one(OptionList)
        if not ol.has_focus or ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        if opt.id and opt.id != "__none__":
            del self.saved[int(opt.id)]
            self._reload()


class ExportScreen(_Dialog):
    """Dismisses with (path, filtered_only) or None."""

    def __init__(self, default_path: str, shown: int, total: int) -> None:
        super().__init__()
        self.default_path = default_path
        self.shown = shown
        self.total = total

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("EXPORT LOGS", classes="title")
            yield Input(self.default_path, id="path")
            with RadioSet(id="which"):
                yield RadioButton(f"Lines matching the filter ({self.shown:,})", value=True)
                yield RadioButton(f"Everything in the buffer ({self.total:,})")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    @on(Input.Submitted, "#path")
    def _save(self) -> None:
        path = self.query_one("#path", Input).value.strip()
        if not path:
            self.notify("Enter a file path", severity="warning")
            return
        filtered = self.query_one(RadioSet).pressed_index == 0
        self.dismiss((path, filtered))


class InputScreen(_Dialog):
    def __init__(self, title: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.value = value
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="title")
            yield Input(self.value, placeholder=self.placeholder)
            yield Label("enter confirm   esc cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class ConfirmScreen(_Dialog):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n", "cancel", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="title")
            with Horizontal(classes="buttons"):
                yield Button("No", id="no")
                yield Button("Yes", id="yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


KEYS = [
    ("Navigation", [
        ("↑ ↓  j k", "move selection"),
        ("PgUp PgDn", "page"),
        ("Home g / End G", "top / bottom (End resumes auto-scroll)"),
        ("← →  h l", "scroll sideways (wrap off)"),
        ("Enter / double-click", "toggle detail pane for the selected line"),
        ("Esc", "clear selection / leave input"),
    ]),
    ("Filtering", [
        ("/", "focus the filter bar (↑ ↓ for history, → accept suggestion)"),
        ("1 … 6", "minimum level V D I W E F   (0 removes it)"),
        ("t / T", "show only / hide the selected line's tag"),
        ("p / P", "show only / hide the selected line's package"),
        ("M", "mark the selected line's package as package:mine"),
        ("x", "clear the filter"),
        ("F", "saved filters"),
    ]),
    ("Searching", [
        ("Ctrl+F", "find (highlights, does not filter)"),
        ("n / N", "next / previous find match"),
        ("e / E", "next / previous error or crash"),
        ("m", "toggle bookmark"),
        ("] / [", "next / previous bookmark"),
    ]),
    ("View", [
        ("Space", "pause / resume the stream"),
        ("w", "soft wrap"),
        ("o", "columns shown"),
        ("s", "stats panel"),
        ("c", "clear the screen"),
        ("C", "clear the device log buffer (adb logcat -c)"),
    ]),
    ("Device & files", [
        ("d", "pick device"),
        ("b", "pick log buffers"),
        ("Ctrl+S", "export to file"),
        ("Ctrl+O", "open a saved log file"),
        ("y / Y", "copy line / whole stack trace"),
        ("?", "this help"),
        ("q", "quit"),
    ]),
]

QUERY_HELP = [
    ("tag:Foo  tag~:regex  tag=:Exact", "tag contains / matches / equals"),
    ("package:com.app  package:mine", "package name (mine = -p or M key)"),
    ("process:name", "process name"),
    ("message:text  msg~:regex", "message text"),
    ("line:text", "the whole raw line"),
    ("level:W", "minimum level"),
    ("age:30s  age:5m  age:2h", "recent entries only"),
    ("is:crash  is:stacktrace  is:anr  is:bookmarked", "special matchers"),
    ("pid:123  tid:123  uid:10123", "numeric ids"),
    ("plain words", "match tag, message or package"),
    ("-tag:chatty", "negate any term"),
    ("a | b   a & b   -( ... )", "or / and / grouped not"),
    ('message:"two words"', "quote values with spaces or | & ( )"),
]


class HelpScreen(_Dialog):
    DEFAULT_CSS = MODAL_CSS + """
    HelpScreen .dialog { width: 96; height: 90%; }
    HelpScreen VerticalScroll { height: 1fr; }
    """
    BINDINGS = [Binding("question_mark,q", "cancel", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("ADBCAT  KEYS AND QUERY SYNTAX", classes="title")
            with VerticalScroll():
                yield Static(self._build_table())
            yield Label("Repeated positive keys are ORed: tag:A tag:B shows both. "
                        "Matching is case-insensitive unless the value has an uppercase letter.",
                        classes="hint")

    def _build_table(self) -> Table:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold #79b8ff", no_wrap=True)
        t.add_column(style="#c8ccd4")
        for section, rows in KEYS:
            t.add_row(Text(section.upper(), style="bold #e0b000"), "")
            for k, v in rows:
                t.add_row(k, v)
            t.add_row("", "")
        t.add_row(Text("QUERY SYNTAX", style="bold #e0b000"), "")
        for k, v in QUERY_HELP:
            t.add_row(k, v)
        return t
