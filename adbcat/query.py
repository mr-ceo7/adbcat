"""Android Studio style logcat query language.

Supported terms (combined with implicit AND, or explicit `&`, `|` and parentheses):

    tag:foo        tag contains "foo"            tag~:regex   tag=:exact
    package:foo    package contains "foo"        package:mine
    process:foo    process name contains "foo"
    message:foo    message contains "foo"        (alias: msg)
    line:foo       the raw line contains "foo"
    level:WARN     minimum level (V D I W E F or full names)
    age:5m         only entries from the last 30s / 5m / 2h / 1d
    is:crash  is:stacktrace  is:anr  is:bookmarked
    pid:123  tid:123  uid:10123
    foo            free text, matched against tag, message, package and process
    -term          negates any term,   -( ... ) negates a group

Repeating a positive key ORs the terms, so `tag:foo tag:bar` shows both tags.
Matching is smart-case: case-insensitive unless the value contains an uppercase letter.
Quote values that contain spaces or special characters: message:"hello world".
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterable

from .model import KIND_MARKER, LEVEL_ALIASES, Entry

KEYS = ("tag", "package", "process", "message", "msg", "line", "level", "age", "is",
        "pid", "tid", "uid")
ORABLE = {"tag", "package", "process", "message", "line", "pid", "tid", "uid"}
IS_VALUES = ("crash", "stacktrace", "anr", "bookmarked")

NATIVE_CRASH_TAGS = {"DEBUG", "libc", "crash_dump32", "crash_dump64", "tombstoned"}
STACK_RE = re.compile(
    r"^\s*(at [\w$.<>\[\]/ -]+\(|Caused by: |Suppressed: |\.\.\. \d+ more|#\d{2,} pc |backtrace:)"
)
TERM_RE = re.compile(r"^(-?)([A-Za-z]+)([~=]?):(.*)$", re.S)
AGE_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

Matcher = Callable[[Entry], bool]


class QueryError(ValueError):
    pass


def is_crash(e: Entry) -> bool:
    if e.level >= 4 and e.tag == "AndroidRuntime":
        return True
    if e.level >= 5 and e.tag in NATIVE_CRASH_TAGS:
        return True
    return e.level >= 4 and e.tag == "libc" and "Fatal signal" in e.msg


def is_stacktrace(e: Entry) -> bool:
    return STACK_RE.match(e.msg) is not None


def is_anr(e: Entry) -> bool:
    return e.tag == "ActivityManager" and "ANR in" in e.msg


# --------------------------------------------------------------------------- tokens

class _Tok:
    __slots__ = ("kind", "text", "key_limit")

    def __init__(self, kind: str, text: str = "", key_limit: int = 0):
        self.kind = kind
        self.text = text
        self.key_limit = key_limit  # chars of `text` that were outside quotes at the start


def tokenize(q: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
            continue
        if c in "()|&":
            toks.append(_Tok(c))
            i += 1
            continue
        if c == "-" and i + 1 < n and q[i + 1] == "(":
            toks.append(_Tok("NOT"))
            i += 1
            continue
        buf: list[str] = []
        key_limit = -1
        in_q = False
        while i < n:
            c = q[i]
            if in_q:
                if c == "\\" and i + 1 < n:
                    buf.append(q[i + 1])
                    i += 2
                    continue
                if c == '"':
                    in_q = False
                    i += 1
                    continue
                buf.append(c)
                i += 1
                continue
            if c == '"':
                if key_limit < 0:
                    key_limit = len(buf)
                in_q = True
                i += 1
                continue
            if c.isspace() or c in "()|&":
                break
            buf.append(c)
            i += 1
        if in_q:
            raise QueryError("unterminated quote")
        text = "".join(buf)
        toks.append(_Tok("TERM", text, len(text) if key_limit < 0 else key_limit))
    return toks


# --------------------------------------------------------------------------- matchers

def _smart(value: str) -> bool:
    """True if matching should be case sensitive."""
    return any(ch.isupper() for ch in value)


def _text_matcher(op: str, value: str) -> Callable[[str], bool]:
    """Build a str -> bool predicate for op '' (contains), '~' (regex) or '=' (exact)."""
    case = _smart(value)
    if op == "~":
        try:
            rx = re.compile(value, 0 if case else re.I)
        except re.error as err:
            raise QueryError(f"bad regex {value!r}: {err}") from None
        return lambda s: s is not None and rx.search(s) is not None
    if op == "=":
        if case:
            return lambda s: s == value
        low = value.lower()
        return lambda s: s is not None and s.lower() == low
    if case:
        return lambda s: s is not None and value in s
    low = value.lower()
    return lambda s: s is not None and low in s.lower()


def _highlight_rx(op: str, value: str) -> re.Pattern | None:
    if not value:
        return None
    flags = 0 if _smart(value) else re.I
    try:
        if op == "~":
            return re.compile(value, flags)
        return re.compile(re.escape(value), flags)
    except re.error:
        return None


class _Term:
    __slots__ = ("key", "neg", "fn")

    def __init__(self, key: str, neg: bool, fn: Matcher):
        self.key = key
        self.neg = neg
        self.fn = fn


class Query:
    """A compiled query. Call `matches(entry)`; call `refresh()` before a batch if using age:."""

    def __init__(self, text: str = "", mine: Iterable[str] = ()):
        self.text = text.strip()
        self.mine = set(mine)
        self.now = time.time()
        self.highlights: list[re.Pattern] = []
        self.uses_package = False
        self.uses_mine = False
        self.uses_age = False
        self._toks = tokenize(self.text)
        self._pos = 0
        if self._toks:
            fn = self._parse_or(negated=False)
            if self._pos != len(self._toks):
                raise QueryError(f"unexpected {self._toks[self._pos].kind!r}")
            self.matches: Matcher = fn
        else:
            self.matches = lambda e: True
        self._toks = []

    def refresh(self) -> None:
        self.now = time.time()

    @property
    def empty(self) -> bool:
        return not self.text

    # -- recursive descent
    def _peek(self) -> str | None:
        return self._toks[self._pos].kind if self._pos < len(self._toks) else None

    def _parse_or(self, negated: bool) -> Matcher:
        parts = [self._parse_and(negated)]
        while self._peek() == "|":
            self._pos += 1
            parts.append(self._parse_and(negated))
        if len(parts) == 1:
            return parts[0]
        return lambda e: any(p(e) for p in parts)

    def _parse_and(self, negated: bool) -> Matcher:
        items: list[Matcher | _Term] = []
        while self._peek() not in (None, "|", ")"):
            if self._peek() == "&":
                self._pos += 1
                continue
            items.append(self._parse_unary(negated))
        if not items:
            raise QueryError("empty expression")
        # positive terms with the same key are ORed (Android Studio behaviour)
        groups: dict[str, list[Matcher]] = {}
        fns: list[Matcher] = []
        for it in items:
            if isinstance(it, _Term):
                if not it.neg and it.key in ORABLE:
                    groups.setdefault(it.key, []).append(it.fn)
                else:
                    fns.append(it.fn)
            else:
                fns.append(it)
        for g in groups.values():
            if len(g) == 1:
                fns.append(g[0])
            else:
                fns.append(lambda e, g=g: any(f(e) for f in g))
        if len(fns) == 1:
            return fns[0]
        return lambda e: all(f(e) for f in fns)

    def _parse_unary(self, negated: bool) -> Matcher | _Term:
        kind = self._peek()
        if kind == "NOT":
            self._pos += 1
            if self._peek() != "(":
                raise QueryError("expected '(' after '-'")
            inner = self._parse_unary(not negated)
            assert not isinstance(inner, _Term)
            return lambda e: not inner(e)
        if kind == "(":
            self._pos += 1
            inner = self._parse_or(negated)
            if self._peek() != ")":
                raise QueryError("missing ')'")
            self._pos += 1
            return inner
        if kind == ")":
            raise QueryError("unexpected ')'")
        tok = self._toks[self._pos]
        self._pos += 1
        return self._term(tok, negated)

    def _term(self, tok: _Tok, negated: bool) -> _Term:
        text = tok.text
        m = TERM_RE.match(text)
        if m and len(m.group(1)) + len(m.group(2)) + len(m.group(3)) < tok.key_limit \
                and m.group(2).lower() in KEYS:
            neg = bool(m.group(1))
            key = m.group(2).lower()
            op = m.group(3)
            value = m.group(4)
            if key == "msg":
                key = "message"
        else:
            neg = text.startswith("-") and tok.key_limit > 0
            key, op, value = "text", "", text[1:] if neg else text
        fn = self._build(key, op, value, highlight=not (neg ^ negated))
        if neg:
            inner = fn
            fn = lambda e: not inner(e)
        return _Term(key, neg, fn)

    def _build(self, key: str, op: str, value: str, highlight: bool) -> Matcher:
        if key in ("level", "age", "is", "pid", "tid", "uid") and op == "~":
            raise QueryError(f"{key}: does not support regex")
        if not value and key not in ("tag", "message", "text"):
            raise QueryError(f"{key}: needs a value")

        if key == "level":
            lv = LEVEL_ALIASES.get(value.upper())
            if lv is None:
                raise QueryError(f"unknown level {value!r}")
            return lambda e: e.level >= lv

        if key == "age":
            am = AGE_RE.match(value)
            if not am:
                raise QueryError("age: expects e.g. 30s, 5m, 2h, 1d")
            secs = float(am.group(1)) * AGE_UNITS[am.group(2)]
            self.uses_age = True
            return lambda e: e.epoch >= self.now - secs

        if key == "is":
            v = value.lower()
            if v == "crash":
                return is_crash
            if v == "stacktrace":
                return is_stacktrace
            if v == "anr":
                return is_anr
            if v in ("bookmarked", "bookmark"):
                return lambda e: e.bookmark
            raise QueryError(f"is: expects one of {', '.join(IS_VALUES)}")

        if key in ("pid", "tid"):
            try:
                num = int(value)
            except ValueError:
                raise QueryError(f"{key}: expects a number") from None
            if key == "pid":
                return lambda e: e.pid == num
            return lambda e: e.tid == num

        if key == "uid":
            tm = _text_matcher("=", value)
            return lambda e: tm(e.uid)

        if key == "package":
            self.uses_package = True
            if value == "mine" and op == "":
                self.uses_mine = True
                mine = self.mine
                return lambda e: e.pkg in mine
            tm = _text_matcher(op, value)
            return lambda e: tm(e.pkg)

        if highlight and key in ("message", "text", "line"):
            rx = _highlight_rx(op, value)
            if rx is not None:
                self.highlights.append(rx)

        if key == "tag":
            tm = _text_matcher(op, value)
            return lambda e: tm(e.tag)
        if key == "process":
            tm = _text_matcher(op, value)
            return lambda e: tm(e.proc)
        if key == "message":
            tm = _text_matcher(op, value)
            return lambda e: tm(e.msg)
        if key == "line":
            tm = _text_matcher(op, value)
            return lambda e: tm(e.raw)
        # free text
        tm = _text_matcher(op, value)
        return lambda e: (tm(e.msg) or tm(e.tag) or tm(e.pkg) or tm(e.proc)
                          or e.kind == KIND_MARKER and tm(e.raw))


def set_level(query: str, level_char: str | None) -> str:
    """Return `query` with its top-level `level:` term replaced (or removed if None)."""
    q = re.sub(r"(?:(?<=\s)|^)level:\S+\s*", "", query).strip()
    if level_char:
        q = f"{q} level:{level_char}".strip()
    return q


def quote(value: str) -> str:
    if not value or any(c.isspace() or c in '()|&"' for c in value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def toggle_term(query: str, term: str) -> str:
    """Add `term` to the query, or remove it if it is already there."""
    parts = query.split()
    if term in parts:
        parts.remove(term)
        return " ".join(parts)
    return f"{query} {term}".strip()
