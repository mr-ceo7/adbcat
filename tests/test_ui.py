import asyncio

from adbcat.app import AdbcatApp
from adbcat.config import Config

LEVELS = "VDIWEF"


def make_log(path, n=500):
    with open(path, "w") as fh:
        for i in range(n):
            lvl = LEVELS[i % 6]
            fh.write(f"2026-10-09 10:00:{i % 60:02d}.{i % 1000:03d}  1000  {100 + i % 3}  {200 + i % 3} "
                     f"{lvl} Tag{i % 4}: message number {i}\n")


def run(coro):
    return asyncio.run(coro)


def test_file_mode_filter_and_navigation(tmp_path):
    log = tmp_path / "x.log"
    make_log(log)

    async def scenario():
        app = AdbcatApp(file=str(log), config=Config(tmp_path / "c.json"))
        async with app.run_test(size=(140, 30)) as pilot:
            for _ in range(50):
                await pilot.pause(0.05)
                if app.state == "file" and len(app.store) == 500:
                    break
            v = app.view
            assert len(v.filtered) == 500
            assert v.follow and v.scroll_offset.y == v.max_scroll_y

            await pilot.press("5")  # level:E
            await pilot.pause(0.1)
            assert app.query_obj.text == "level:E"
            assert len(v.filtered) == sum(1 for i in range(500) if i % 6 >= 4)

            await pilot.press("home", "t")  # only this tag
            await pilot.pause(0.1)
            assert "tag=:Tag" in app.query_obj.text
            assert all(app.store.get(s).tag == app.store.get(v.selected).tag for s in v.filtered)

            await pilot.press("x", "slash", *"message=:\"message number 42\"", "enter")
            await pilot.pause(0.3)
            assert [app.store.get(s).msg for s in v.filtered] == ["message number 42"]

            await pilot.press("x", "w")
            await pilot.pause(0.1)
            assert v.wrap and v._total >= 500

            await pilot.press("space")
            assert app.paused
            await pilot.press("space", "c")
            await pilot.pause(0.1)
            assert len(app.store) == 0 and v.filtered == []

    run(scenario())


def test_bad_query_keeps_previous_filter(tmp_path):
    log = tmp_path / "x.log"
    make_log(log, 60)

    async def scenario():
        app = AdbcatApp(file=str(log), config=Config(tmp_path / "c.json"))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.5)
            app.set_query("level:W")
            n = len(app.view.filtered)
            await pilot.press("slash", *" tag~:(", "enter")
            await pilot.pause(0.3)
            assert app.query_error
            assert len(app.view.filtered) == n

    run(scenario())


def test_timers_and_actions_work_behind_a_dialog(tmp_path):
    log = tmp_path / "x.log"
    make_log(log, 100)

    async def scenario():
        app = AdbcatApp(file=str(log), config=Config(tmp_path / "c.json"))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("s", "question_mark")
            await pilot.pause(0.1)
            app._tick()  # must not fail while HelpScreen is on top
            app.set_query("level:E")
            assert len(app.view.filtered) == sum(1 for i in range(100) if i % 6 >= 4)
            await pilot.press("escape")

    run(scenario())
