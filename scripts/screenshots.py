"""Regenerate the README screenshots from the synthetic demo log.

    python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adbcat.app import AdbcatApp  # noqa: E402
from adbcat.config import Config  # noqa: E402
from demo_log import generate  # noqa: E402

SIZE = (150, 38)


async def shoot() -> None:
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "demo.log"
    log.write_text("\n".join(generate()) + "\n")
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)

    app = AdbcatApp(file=str(log), config=Config(tmp / "c.json"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(1.5)
        app.set_query("-tag:SurfaceFlinger")
        await pilot.press("s")
        await pilot.pause(0.3)
        app.save_screenshot(str(docs / "screenshot.svg"))

    app = AdbcatApp(file=str(log), config=Config(tmp / "c2.json"), mine=["com.example.notes"],
                    query="package:mine level:W")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(1.5)
        await pilot.press("home", "e")
        while app._selected_entry() and app._selected_entry().tag != "AndroidRuntime":
            await pilot.press("e")
        await pilot.press("enter")
        await pilot.pause(0.3)
        app.save_screenshot(str(docs / "crash.svg"))
    print("wrote docs/screenshot.svg and docs/crash.svg")


if __name__ == "__main__":
    asyncio.run(shoot())
