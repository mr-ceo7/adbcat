"""Talking to adb: devices, the logcat stream and the pid -> process map."""

from __future__ import annotations

import re
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .model import KIND_LOG, make_marker, parse_line

ADB = "adb"
ALL_BUFFERS = ("main", "system", "crash", "events", "radio", "kernel")
DEFAULT_BUFFERS = ("main", "system", "crash")
APP_USER_RE = re.compile(r"^(u\d+_?[ai]\d+|\d{5,})$")

StatusCallback = Callable[[str, str], None]  # (state, detail)


@dataclass
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    transport: str = ""

    @property
    def label(self) -> str:
        return self.model.replace("_", " ") or self.product or self.serial


def run_adb(args: list[str], serial: str | None = None, timeout: float = 10) -> subprocess.CompletedProcess:
    cmd = [ADB] + (["-s", serial] if serial else []) + args
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)


def list_devices() -> list[Device]:
    try:
        out = run_adb(["devices", "-l"], timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        d = Device(parts[0], parts[1])
        for p in parts[2:]:
            k, _, v = p.partition(":")
            if k == "model":
                d.model = v
            elif k == "product":
                d.product = v
            elif k == "usb" or k == "transport_id":
                d.transport = d.transport or p
        devices.append(d)
    return devices


def device_state(serial: str) -> str:
    try:
        r = run_adb(["get-state"], serial, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return r.stdout.strip() or ("offline" if "offline" in r.stderr else "missing")


def clear_device_log(serial: str, buffers: tuple[str, ...]) -> str:
    """Run `adb logcat -c`. Returns an error message, or "" on success."""
    try:
        r = run_adb(["logcat", "-c", "-b", ",".join(buffers)], serial)
    except (OSError, subprocess.TimeoutExpired) as err:
        return str(err)
    return r.stderr.strip() if r.returncode else ""


class LogcatSource:
    """Streams `adb logcat` for one device into `sink`, reconnecting when the device drops."""

    def __init__(self, serial: str | None, buffers: tuple[str, ...], sink: deque,
                 on_status: StatusCallback, tail: int | None = None):
        self.serial = serial
        self.buffers = buffers
        self.sink = sink
        self.on_status = on_status
        self.tail = tail
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name="logcat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _sleep(self, secs: float) -> bool:
        """Sleep unless stopped. Returns False if we were stopped."""
        return not self._stop.wait(secs)

    def _pick_device(self) -> str | None:
        while not self._stop.is_set():
            online = [d for d in list_devices() if d.state == "device"]
            if online:
                return online[0].serial
            self.on_status("waiting", "no device connected - plug one in")
            if not self._sleep(1):
                return None
        return None

    def _run(self) -> None:
        if self.serial is None:
            self.serial = self._pick_device()
            if self.serial is None:
                return
            self.on_status("device", self.serial)
        fmt = ["-v", "threadtime", "-v", "year", "-v", "uid"]
        since: str | None = None
        recent: deque[str] = deque(maxlen=4000)
        recent_set: set[str] = set()
        was_connected = False

        while not self._stop.is_set():
            state = device_state(self.serial)
            if state != "device":
                self.on_status("waiting", f"waiting for {self.serial} ({state})")
                if not self._sleep(1):
                    return
                continue

            cmd = [ADB, "-s", self.serial, "logcat", *fmt, "-b", ",".join(self.buffers)]
            if since:
                cmd += ["-T", since]
            elif self.tail:
                cmd += ["-T", str(self.tail)]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, errors="replace", bufsize=1,
                )
            except OSError as err:
                self.on_status("error", f"cannot run adb: {err}")
                return
            if was_connected:
                self.sink.append(make_marker("---- device reconnected ----", level=3))
            self.on_status("live", self.serial)
            resume_ts = since
            count = 0
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                e = parse_line(line)
                if e is None:
                    continue
                if resume_ts and e.kind == KIND_LOG and e.ts <= resume_ts and e.raw in recent_set:
                    continue
                if len(recent) == recent.maxlen:
                    recent_set.discard(recent[0])
                recent.append(e.raw)
                recent_set.add(e.raw)
                if e.kind == KIND_LOG:
                    since = e.ts
                self.sink.append(e)
                count += 1
            rc = self._proc.wait()
            if self._stop.is_set():
                return
            err = (self._proc.stderr.read() if self._proc.stderr else "").strip()
            if count == 0 and rc != 0 and len(fmt) > 2 and device_state(self.serial) == "device":
                # old Android without `-v year` / `-v uid`
                fmt = ["-v", "threadtime"]
                continue
            if count == 0 and rc != 0 and err and device_state(self.serial) == "device":
                self.on_status("error", err.splitlines()[-1])
                return
            was_connected = True
            self.sink.append(make_marker("---- device disconnected ----", level=3))
            self.on_status("waiting", f"{self.serial} disconnected - waiting")
            if not self._sleep(1):
                return


class FileSource:
    """Loads a saved logcat file."""

    def __init__(self, path: str, sink: deque, on_status: StatusCallback, max_lines: int = 0):
        self.path = path
        self.max_lines = max_lines
        self.sink = sink
        self.on_status = on_status
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="file", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.on_status("loading", self.path)
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                # only the newest max_lines would survive in the store anyway
                lines = deque(fh, maxlen=self.max_lines) if self.max_lines else fh
                for line in lines:
                    if self._stop.is_set():
                        return
                    e = parse_line(line)
                    if e is not None:
                        self.sink.append(e)
        except OSError as err:
            self.on_status("error", str(err))
            return
        self.on_status("file", self.path)


@dataclass
class Proc:
    pid: int
    user: str
    name: str

    @property
    def is_app(self) -> bool:
        return bool(APP_USER_RE.match(self.user)) and "." in self.name

    @property
    def package(self) -> str:
        return self.name.split(":", 1)[0]


def parse_ps(out: str) -> dict[int, Proc]:
    lines = out.splitlines()
    if not lines:
        return {}
    header = lines[0].split()
    try:
        pid_i = header.index("PID")
    except ValueError:
        return {}
    user_i = header.index("USER") if "USER" in header else None
    procs: dict[int, Proc] = {}
    for line in lines[1:]:
        cols = line.split()
        if len(cols) <= pid_i:
            continue
        try:
            pid = int(cols[pid_i])
        except ValueError:
            continue
        name = cols[-1]
        if name.startswith("[") or len(cols) < 2:
            continue
        procs[pid] = Proc(pid, cols[user_i] if user_i is not None else "", name)
    return procs


class ProcessTracker:
    """Polls `ps` on the device and reports the pid -> process map."""

    def __init__(self, serial_getter: Callable[[], str | None],
                 on_update: Callable[[dict[int, Proc]], None], interval: float = 2.0):
        self.serial_getter = serial_getter
        self.on_update = on_update
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ps", daemon=True)
        self._args = ["shell", "ps", "-A", "-o", "PID,USER,NAME"]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            serial = self.serial_getter()
            if serial:
                try:
                    r = run_adb(self._args, serial, timeout=10)
                    procs = parse_ps(r.stdout)
                    if len(procs) < 5 and self._args[2] == "-A":
                        self._args = ["shell", "ps"]  # pre-Oreo toybox/toolbox ps
                    elif procs:
                        self.on_update(procs)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._stop.wait(self.interval)


START_PROC_RE = re.compile(r"^Start proc (\d+):([^/\s]+)/(\S+)")
