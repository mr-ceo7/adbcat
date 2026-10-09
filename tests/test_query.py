import pytest

from adbcat.model import KIND_RAW, parse_line
from adbcat.query import Query, QueryError, set_level, toggle_term

LINES = [
    "2026-10-09 07:36:30.373  1000  1080  1746 D BroadcastQueue: Finished with ordered broadcast",
    "2026-10-09 07:36:30.776  1000  1080  1110 E UnisocWatchdog: WDT_Monitor: duration=50",
    "2026-10-09 07:36:31.000 10123  4321  4321 E AndroidRuntime: FATAL EXCEPTION: main",
    "2026-10-09 07:36:31.000 10123  4321  4321 E AndroidRuntime: \tat com.foo.Bar.baz(Bar.java:12)",
    "2026-10-09 07:36:32.000 10123  4321  4400 I chatty  : uid=10123 expire 3 lines",
    "10-09 07:36:33.100  4321  4321 W MyTag   : hello world",
]


def entries():
    out = []
    for line in LINES:
        e = parse_line(line)
        if e.pid == 4321:
            e.pkg, e.proc = "com.foo", "com.foo"
        out.append(e)
    return out


def run(q, mine=()):
    query = Query(q, mine)
    return [e.tag for e in entries() if query.matches(e)]


def test_parse_threadtime_year_uid():
    e = parse_line(LINES[1])
    assert (e.ts, e.uid, e.pid, e.tid, e.level_char, e.tag) == (
        "2026-10-09 07:36:30.776", "1000", 1080, 1110, "E", "UnisocWatchdog")
    assert e.msg == "WDT_Monitor: duration=50"  # only the first ": " splits the tag


def test_parse_plain_threadtime_and_raw():
    e = parse_line(LINES[5])
    assert (e.uid, e.pid, e.tag, e.msg, e.date_str, e.time_str) == (
        "", 4321, "MyTag", "hello world", "10-09", "07:36:33.100")
    assert parse_line("--------- beginning of main") is None
    assert parse_line("garbage").kind == KIND_RAW
    assert "\t" not in parse_line(LINES[3]).msg


def test_keys():
    assert run("tag:Android") == ["AndroidRuntime", "AndroidRuntime"]
    assert run("tag=:chatty") == ["chatty"]
    assert run("-tag:chatty level:E") == ["UnisocWatchdog", "AndroidRuntime", "AndroidRuntime"]
    assert run("level:WARN") == ["UnisocWatchdog", "AndroidRuntime", "AndroidRuntime", "MyTag"]
    assert run("package:foo") == ["AndroidRuntime", "AndroidRuntime", "chatty", "MyTag"]
    assert run("package:mine", mine=["com.foo"]) == ["AndroidRuntime", "AndroidRuntime", "chatty", "MyTag"]
    assert run("package:mine") == []
    assert run("is:crash") == ["AndroidRuntime", "AndroidRuntime"]
    assert run("is:stacktrace") == ["AndroidRuntime"]
    assert run("pid:1080") == ["UnisocWatchdog", "BroadcastQueue"][::-1]
    assert run("tid:4400") == ["chatty"]
    assert run("uid:10123 -tag:chatty") == ["AndroidRuntime", "AndroidRuntime"]
    assert run("message~:dur.*=\\d+") == ["UnisocWatchdog"]
    assert run('message:"hello world"') == ["MyTag"]


def test_same_key_or_and_operators():
    assert run("tag:chatty tag:MyTag") == ["chatty", "MyTag"]
    assert run("tag:chatty | tag:MyTag") == ["chatty", "MyTag"]
    assert run("(tag:chatty | tag:MyTag) & level:W") == ["MyTag"]
    assert run("-(tag:chatty | tag:MyTag) package:foo") == ["AndroidRuntime", "AndroidRuntime"]
    assert run('tag~:"chatty|MyTag"') == ["chatty", "MyTag"]


def test_free_text_smart_case():
    assert run("fatal") == ["AndroidRuntime"]
    assert run("Fatal") == []
    assert run("-broadcast -wdt -fatal -at -uid") == ["MyTag"]
    q = Query("hello message:world")
    assert [rx.pattern for rx in q.highlights] == ["hello", "world"]
    assert Query("-hello").highlights == []


def test_errors():
    for bad in ["level:X", "tag~:(", "(tag:a", "age:5x", "is:nope", "pid:x", '"open', "a | ", "-tag"]:
        if bad == "-tag":
            Query(bad)  # free text "tag" negated - allowed
            continue
        with pytest.raises(QueryError):
            Query(bad)


def test_age():
    e = parse_line(LINES[0])
    assert not Query("age:5m").matches(e)
    assert Query("age:5000d").matches(e)


def test_helpers():
    assert set_level("tag:a level:W x", "E") == "tag:a x level:E"
    assert set_level("level:W", None) == ""
    assert toggle_term("tag:a", "-tag=:b") == "tag:a -tag=:b"
    assert toggle_term("tag:a -tag=:b", "-tag=:b") == "tag:a"
