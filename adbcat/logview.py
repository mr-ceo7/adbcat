"""The scrolling, virtualised log list."""

from __future__ import annotations

import re
import zlib
from bisect import bisect_left, bisect_right
from typing import Callable

from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from .model import KIND_MARKER, KIND_RAW, LEVEL_CHARS, Entry, LogStore

BG = "#1b1d21"
BADGE = [
    Style(color="#ffffff", bgcolor="#5c6370"),
    Style(color="#ffffff", bgcolor="#2f6fbf"),
    Style(color="#ffffff", bgcolor="#3f8a2e"),
    Style(color="#111111", bgcolor="#e0b000", bold=True),
    Style(color="#ffffff", bgcolor="#d03c3c", bold=True),
    Style(color="#ffffff", bgcolor="#9b2fae", bold=True),
]
MSG = [
    Style(color="#a0a7b4"),
    Style(color="#79b8ff"),
    Style(color="#a6d189"),
    Style(color="#f2cc60"),
    Style(color="#ff7b72"),
    Style(color="#ff79c6", bold=True),
]
PALETTE = [
    "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d19a66",
    "#f78c6c", "#82aaff", "#c3e88d", "#ffcb6b", "#89ddff", "#f07178", "#b39ddb",
    "#7ec699", "#ff9e64", "#73daca", "#bb9af7", "#e0af68", "#9ece6a",
]
TIME_STYLE = Style(color="#6b7380")
DATE_STYLE = Style(color="#4f5663")
PID_STYLE = Style(color="#7a8290")
UID_STYLE = Style(color="#5f6673")
UNKNOWN_STYLE = Style(color="#4f5663", italic=True)
MARKER_STYLE = Style(color="#56b6c2", bold=True)
RAW_STYLE = Style(color="#a0a7b4", italic=True)
HL_STYLE = Style(color="#000000", bgcolor="#c9a227")
FIND_STYLE = Style(color="#000000", bgcolor="#ff8c1a", bold=True)
SEL_STYLE = Style(bgcolor="#2c3e5c")
BOOKMARK = Segment("▌", Style(color="#4fc1ff", bold=True))
NO_BOOKMARK = Segment(" ")

_color_cache: dict[str, Style] = {}


def name_style(name: str) -> Style:
    """A stable colour for a tag / package name."""
    st = _color_cache.get(name)
    if st is None:
        st = Style(color=PALETTE[zlib.crc32(name.encode()) % len(PALETTE)])
        if len(_color_cache) > 5000:
            _color_cache.clear()
        _color_cache[name] = st
    return st


def fit(text: str, width: int) -> str:
    if cell_len(text) > width:
        return set_cell_size(text, width - 1) + "…"
    return set_cell_size(text, width)


class LogView(ScrollView, can_focus=True):
    DEFAULT_CSS = f"""
    LogView {{
        background: {BG};
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: {BG};
        scrollbar-color: #3a3f4b;
        scrollbar-color-hover: #4f5666;
    }}
    """

    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home,g", "top", "Top", show=False),
        Binding("end,G", "bottom", "Bottom", show=False),
        Binding("left,h", "hscroll(-8)", "Left", show=False),
        Binding("right,l", "hscroll(8)", "Right", show=False),
    ]

    class SelectionChanged(Message):
        def __init__(self, entry: Entry | None) -> None:
            super().__init__()
            self.entry = entry

    class Activated(Message):
        """Enter / double click on a line."""

    def __init__(self, store: LogStore, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.filtered: list[int] = []
        self.wrap = False
        self.columns = {"date": True, "time": True, "pid": True, "uid": False,
                        "package": True, "tag": True}
        self.tag_width = 23
        self.package_width = 28
        self.highlights: list[re.Pattern] = []
        self.find_rx: re.Pattern | None = None
        self.selected: int | None = None
        self._cache: dict[int, tuple[Strip, Strip]] = {}
        self._starts: list[int] = []  # wrap mode: first row of each filtered entry
        self._total = 0
        self._max_msg = 0
        self._layout_width = 0
        self.follow = True  # stick to the newest line as logs arrive
        self._programmatic = 0

    # ------------------------------------------------------------------ geometry
    @property
    def view_width(self) -> int:
        return max(1, self.scrollable_content_region.width)

    @property
    def view_height(self) -> int:
        return max(1, self.scrollable_content_region.height)

    def header_width(self) -> int:
        c = self.columns
        w = 1  # bookmark gutter
        if c["date"]:
            w += 6
        if c["time"]:
            w += 13
        if c["pid"]:
            w += 12
        if c["uid"]:
            w += 6
        if c["package"]:
            w += self.package_width + 1
        if c["tag"]:
            w += self.tag_width + 1
        return w + 4  # level badge + space

    def _wrap_geometry(self) -> tuple[int, int]:
        """(indent, available message width) for wrapped rows."""
        width = self.view_width
        hw = self.header_width()
        if width - hw >= 24:
            return hw, width - hw
        return 0, width

    def _row_counter(self) -> Callable[[Entry | None], int]:
        """Returns a function giving the number of wrapped rows for an entry."""
        indent, avail = self._wrap_geometry()
        extra = 0 if indent else 1  # narrow terminal: the header gets its own row

        def rows(e: Entry | None) -> int:
            if e is None:
                return 1
            msg = e.msg
            n = len(msg) if msg.isascii() else cell_len(msg)
            return extra + (-(-n // avail) if n else 1)
        return rows

    def _relayout(self) -> None:
        """Recompute row geometry for the whole filtered list."""
        get = self.store.get
        if self.wrap:
            rows = self._row_counter()
            starts = []
            total = 0
            for seq in self.filtered:
                starts.append(total)
                total += rows(get(seq))
            self._starts = starts
            self._total = total
        else:
            self._starts = []
            self._total = len(self.filtered)
            mx = 0
            for seq in self.filtered:
                e = get(seq)
                if e:
                    n = len(e.msg)
                    if n > mx:
                        mx = n
            self._max_msg = mx
        self._layout_width = self.view_width
        self._update_virtual_size()

    def _update_virtual_size(self) -> None:
        if self.wrap:
            width = self.view_width
        else:
            width = self.header_width() + self._max_msg
        self.virtual_size = Size(width, self._total)

    def row_to_index(self, row: int) -> tuple[int, int]:
        """Map a virtual row to (filtered index, sub row)."""
        if not self.wrap:
            return row, 0
        i = bisect_right(self._starts, row) - 1
        return i, row - self._starts[i] if i >= 0 else 0

    def index_to_row(self, idx: int) -> int:
        if not self.wrap:
            return idx
        return self._starts[idx] if 0 <= idx < len(self._starts) else self._total

    def index_height(self, idx: int) -> int:
        if not self.wrap:
            return 1
        nxt = self._starts[idx + 1] if idx + 1 < len(self._starts) else self._total
        return nxt - self._starts[idx]

    @property
    def at_bottom(self) -> bool:
        return self.follow

    def _scroll_y(self, y: int) -> None:
        """Scroll without it counting as the user scrolling."""
        self._programmatic += 1
        try:
            self.scroll_to(y=y, animate=False, force=True)
        finally:
            self._programmatic -= 1

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._programmatic:
            return
        if new_value >= self.max_scroll_y:
            self.follow = True
        elif new_value < old_value:
            self.follow = False

    # ------------------------------------------------------------------ data
    def set_filtered(self, seqs: list[int]) -> None:
        self.filtered = seqs
        self._cache.clear()
        self._relayout()
        if self.selected is not None and self.index_of(self.selected) is None:
            self.selected = None
            self.post_message(self.SelectionChanged(None))
        self.refresh()

    def append(self, seqs: list[int]) -> None:
        if not seqs:
            return
        self.filtered.extend(seqs)
        get = self.store.get
        if self.wrap:
            rows = self._row_counter()
            total = self._total
            starts = self._starts
            for seq in seqs:
                starts.append(total)
                total += rows(get(seq))
            self._total = total
        else:
            self._total = len(self.filtered)
            mx = self._max_msg
            for seq in seqs:
                e = get(seq)
                if e and len(e.msg) > mx:
                    mx = len(e.msg)
            self._max_msg = mx
        self._update_virtual_size()
        self.refresh()

    def drop_before(self, base: int) -> None:
        """Forget filtered entries that the store has trimmed away."""
        k = bisect_left(self.filtered, base)
        if not k:
            return
        removed_rows = self.index_to_row(k)
        y = self.scroll_offset.y
        del self.filtered[:k]
        self._cache = {s: v for s, v in self._cache.items() if s >= base}
        self._relayout()
        if self.selected is not None and self.selected < base:
            self.selected = None
            self.post_message(self.SelectionChanged(None))
        self._scroll_y(max(0, y - removed_rows))
        self.refresh()

    def index_of(self, seq: int) -> int | None:
        i = bisect_left(self.filtered, seq)
        if i < len(self.filtered) and self.filtered[i] == seq:
            return i
        return None

    def invalidate(self) -> None:
        """Drop cached renders (format / highlight / process names changed)."""
        self._cache.clear()
        self.refresh()

    def set_format(self, columns: dict, wrap: bool) -> None:
        self.columns = dict(columns)
        self.wrap = wrap
        anchor = self._anchor()
        self._cache.clear()
        self._relayout()
        self._restore_anchor(anchor)
        self.refresh()

    def _anchor(self) -> tuple[int | None, bool]:
        """Remember what is on screen so a relayout does not lose the place."""
        if self.at_bottom:
            return None, True
        if self.selected is not None:
            return self.selected, False
        idx, _ = self.row_to_index(self.scroll_offset.y)
        if 0 <= idx < len(self.filtered):
            return self.filtered[idx], False
        return None, True

    def _restore_anchor(self, anchor: tuple[int | None, bool]) -> None:
        seq, bottom = anchor
        if bottom or seq is None:
            self.scroll_to_bottom()
            return
        idx = self.index_of(seq)
        if idx is None:
            self.scroll_to_bottom()
        else:
            self._scroll_y(self.index_to_row(idx))

    # ------------------------------------------------------------------ rendering
    def _render_entry(self, e: Entry) -> tuple[Strip, Strip]:
        cached = self._cache.get(e.seq)
        if cached is not None:
            return cached
        c = self.columns
        segs: list[Segment] = [BOOKMARK if e.bookmark else NO_BOOKMARK]
        if c["date"]:
            segs.append(Segment(e.date_str.ljust(5) + " ", DATE_STYLE))
        if c["time"]:
            segs.append(Segment(e.time_str.ljust(12) + " ", TIME_STYLE))
        if c["pid"]:
            pid = f"{e.pid:>5}-{e.tid:<5}" if e.pid or e.tid else ""
            segs.append(Segment(fit(pid, 11) + " ", PID_STYLE))
        if c["uid"]:
            segs.append(Segment(fit(e.uid, 5) + " ", UID_STYLE))
        if c["package"]:
            name = e.pkg or e.proc
            if name:
                segs.append(Segment(fit(name, self.package_width), name_style(name)))
            else:
                label = f"pid-{e.pid}" if e.pid else ""
                segs.append(Segment(fit(label, self.package_width), UNKNOWN_STYLE))
            segs.append(Segment(" "))
        if c["tag"]:
            segs.append(Segment(fit(e.tag, self.tag_width), name_style(e.tag)))
            segs.append(Segment(" "))
        lvl = e.level
        segs.append(Segment(f" {LEVEL_CHARS[lvl]} ", BADGE[lvl]))
        segs.append(Segment(" "))
        header = Strip(segs)

        if e.kind == KIND_MARKER:
            style = MARKER_STYLE
        elif e.kind == KIND_RAW:
            style = RAW_STYLE
        else:
            style = MSG[lvl]
        msg = e.msg
        rxs = self.highlights
        if (rxs or self.find_rx) and msg:
            text = Text(msg, style=style, end="")
            for rx in rxs:
                for m in rx.finditer(msg):
                    if m.end() > m.start():
                        text.stylize(HL_STYLE, m.start(), m.end())
            if self.find_rx is not None:
                for m in self.find_rx.finditer(msg):
                    if m.end() > m.start():
                        text.stylize(FIND_STYLE, m.start(), m.end())
            body = Strip(list(text.render(self.app.console, end="")))
        else:
            body = Strip([Segment(msg, style)] if msg else [])
        result = (header, body)
        if len(self._cache) > 6000:
            self._cache.clear()
        self._cache[e.seq] = result
        return result

    def _compact_header(self, e: Entry) -> Strip:
        """Header for terminals too narrow for the column layout."""
        name = e.pkg or e.proc
        segs = [BOOKMARK if e.bookmark else NO_BOOKMARK,
                Segment(e.time_str + " ", TIME_STYLE),
                Segment(f" {LEVEL_CHARS[e.level]} ", BADGE[e.level]),
                Segment(" "),
                Segment(e.tag, name_style(e.tag))]
        if name:
            segs += [Segment("  "), Segment(name, name_style(name))]
        return Strip(segs)

    def render_line(self, y: int) -> Strip:
        width = self.view_width
        if self.wrap and self._layout_width != width:
            self._relayout()
        row = self.scroll_offset.y + y
        if row >= self._total or row < 0:
            return Strip.blank(width, Style(bgcolor=BG))
        idx, sub = self.row_to_index(row)
        if not (0 <= idx < len(self.filtered)):
            return Strip.blank(width, Style(bgcolor=BG))
        e = self.store.get(self.filtered[idx])
        if e is None:
            return Strip.blank(width, Style(bgcolor=BG))
        header, body = self._render_entry(e)
        if self.wrap:
            indent, avail = self._wrap_geometry()
            if indent:
                start = sub * avail
                lead = header if sub == 0 else Strip.blank(indent)
                strip = Strip.join([lead, body.crop(start, start + avail)])
            elif sub == 0:
                strip = self._compact_header(e).crop(0, width)
            else:
                start = (sub - 1) * avail
                strip = body.crop(start, start + avail)
        else:
            sx = self.scroll_offset.x
            strip = Strip.join([header, body]).crop(sx, sx + width)
        selected = e.seq == self.selected
        if selected:
            strip = strip.apply_style(SEL_STYLE)
        strip = strip.extend_cell_length(width, SEL_STYLE if selected else None)
        return strip.apply_style(Style(bgcolor=BG))

    def on_resize(self, event: events.Resize) -> None:
        if self.wrap:
            anchor = self._anchor()
            self._relayout()
            self._restore_anchor(anchor)
        else:
            self._update_virtual_size()
            if self.follow:
                self.scroll_to_bottom()
        self.refresh()

    # ------------------------------------------------------------------ selection / movement
    def _selected_index(self) -> int | None:
        return self.index_of(self.selected) if self.selected is not None else None

    def select_index(self, idx: int, center: bool = False) -> None:
        if not self.filtered:
            return
        idx = max(0, min(idx, len(self.filtered) - 1))
        self.follow = idx == len(self.filtered) - 1
        seq = self.filtered[idx]
        if seq != self.selected:
            self.selected = seq
            self.post_message(self.SelectionChanged(self.store.get(seq)))
        self.ensure_visible(idx, center)
        self.refresh()

    def clear_selection(self) -> None:
        if self.selected is not None:
            self.selected = None
            self.post_message(self.SelectionChanged(None))
            self.refresh()

    def ensure_visible(self, idx: int, center: bool = False) -> None:
        row = self.index_to_row(idx)
        h = self.index_height(idx)
        top = self.scroll_offset.y
        height = self.view_height
        if center and not (top <= row and row + h <= top + height):
            self._scroll_y(max(0, row - height // 2))
        elif row < top:
            self._scroll_y(row)
        elif row + h > top + height:
            self._scroll_y(row + h - height)

    def scroll_to_bottom(self) -> None:
        self.follow = True
        self._scroll_y(max(0, self._total - self.view_height))

    def _current_index(self) -> int:
        idx = self._selected_index()
        if idx is not None:
            return idx
        # nothing selected: start from the last fully visible line
        row = min(self._total - 1, self.scroll_offset.y + self.view_height - 1)
        return self.row_to_index(max(0, row))[0]

    def action_move(self, delta: int) -> None:
        if not self.filtered:
            return
        if self.selected is None:
            self.select_index(self._current_index())
            return
        self.select_index(self._current_index() + delta)

    def action_page(self, direction: int) -> None:
        if not self.filtered:
            return
        self.select_index(self._current_index() + direction * max(1, self.view_height - 1))

    def action_top(self) -> None:
        self.select_index(0)
        self._scroll_y(0)

    def action_bottom(self) -> None:
        self.clear_selection()
        self.scroll_to_bottom()

    def action_hscroll(self, delta: int) -> None:
        if not self.wrap:
            self.scroll_to(x=max(0, self.scroll_offset.x + delta), animate=False, force=True)

    def jump(self, pred: Callable[[Entry], bool], forward: bool) -> bool:
        """Select the next/previous entry matching `pred`. Returns False if none."""
        if not self.filtered:
            return False
        get = self.store.get
        start = self._current_index()
        rng = range(start + 1, len(self.filtered)) if forward else range(start - 1, -1, -1)
        for i in rng:
            e = get(self.filtered[i])
            if e is not None and pred(e):
                self.select_index(i, center=True)
                return True
        return False

    def on_click(self, event: events.Click) -> None:
        row = self.scroll_offset.y + event.y
        if 0 <= row < self._total:
            idx, _ = self.row_to_index(row)
            self.select_index(idx)
            if event.chain >= 2:
                self.post_message(self.Activated())

    def visible_entries(self) -> list[Entry]:
        get = self.store.get
        return [e for e in (get(s) for s in self.filtered) if e is not None]
