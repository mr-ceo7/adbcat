"""Command line entry point: python -m adbcat"""

from __future__ import annotations

import argparse
import shutil
import sys

from .adb import ALL_BUFFERS, DEFAULT_BUFFERS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="adbcat",
        description="Terminal UI for adb logcat with Android Studio style filtering.",
    )
    ap.add_argument("-s", "--serial", help="device serial (default: ask if several are connected)")
    ap.add_argument("-p", "--package", action="append", default=[],
                    help="package used by package:mine (repeatable); also adds package:mine "
                         "to the filter if no -q is given")
    ap.add_argument("-q", "--query", default="", help='initial filter, e.g. "level:W -tag:chatty"')
    ap.add_argument("-b", "--buffers", default=",".join(DEFAULT_BUFFERS),
                    help=f"comma separated log buffers ({', '.join(ALL_BUFFERS)})")
    ap.add_argument("-f", "--file", help="open a saved logcat file instead of a device")
    ap.add_argument("-a", "--all", action="store_true",
                    help="show every app's logs even when run inside an Android project")
    ap.add_argument("-T", "--tail", type=int, help="only load the last N lines of the device buffer")
    ap.add_argument("--max-lines", type=int, help="lines kept in memory (default 200000)")
    args = ap.parse_args(argv)

    buffers = tuple(b.strip() for b in args.buffers.split(",") if b.strip())
    bad = [b for b in buffers if b not in ALL_BUFFERS]
    if bad:
        ap.error(f"unknown buffer(s): {', '.join(bad)}")
    if not args.file and shutil.which("adb") is None:
        print("adbcat: `adb` was not found on PATH", file=sys.stderr)
        return 1

    from .app import AdbcatApp
    from .config import Config

    config = Config()
    mine = list(args.package)
    banner = None
    if args.package:
        config["mine"] = args.package
    elif not args.file:
        from .detect import detect_project

        project = detect_project()
        if project:
            mine = project.packages  # not saved to config: it belongs to this directory
            banner = f"Android project '{project.name}': {', '.join(mine)}"
            if args.all:
                banner += "  (--all: showing every app; filter with package:mine)"
    query = args.query or ("package:mine" if mine and not args.all else "")
    app = AdbcatApp(serial=args.serial, buffers=buffers, query=query, file=args.file,
                    mine=mine or None, tail=args.tail, max_lines=args.max_lines,
                    config=config, banner=banner)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
