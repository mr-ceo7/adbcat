"""Write a synthetic logcat file for trying adbcat without a device.

    python scripts/demo_log.py demo.log
    adbcat -f demo.log

Every line is made up; no real device data is involved.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

APPS = {
    2841: ("com.example.notes", 10211),
    3107: ("com.example.weather", 10187),
}
SYSTEM = {1080: ("system_server", 1000), 612: ("surfaceflinger", 1000), 498: ("netd", 0)}

NOISE = [
    (1080, "D", "ActivityManager", "Received broadcast android.intent.action.BATTERY_CHANGED"),
    (1080, "I", "PowerManagerService", "Waking up from Dozing (uid=1000 reason=WAKE_REASON_POWER_BUTTON)"),
    (1080, "W", "PackageManager", "Not granting permission android.permission.QUERY_ALL_PACKAGES"),
    (612, "D", "SurfaceFlinger", "Display 0 HWC layers: 4, client composition: 0"),
    (612, "V", "SurfaceFlinger", "onMessageInvalidate vsyncId=81723"),
    (498, "I", "netd", "setDnsConfiguration netId=101 servers=2"),
    (1080, "I", "WifiService", "RSSI changed: -61 dBm"),
    (1080, "D", "AlarmManager", "set(PendingIntent{9c1f2 com.example.weather}) type=3 interval=900000"),
    (1080, "E", "BatteryStatsService", "power: Missing API: getPowerStatsMeter"),
]
NOTES = [
    ("D", "NotesRepository", "Loaded 42 notes from database in 18ms"),
    ("I", "SyncWorker", "Sync started (account=demo@example.com)"),
    ("D", "OkHttp", "--> POST https://api.example.com/v1/notes/sync (1.2-kB body)"),
    ("D", "OkHttp", "<-- 200 OK https://api.example.com/v1/notes/sync (231ms)"),
    ("I", "SyncWorker", "Sync finished: 3 uploaded, 1 downloaded"),
    ("V", "EditorFragment", "onTextChanged length=318"),
    ("W", "ImageLoader", "Bitmap 4032x3024 decoded on main thread; consider downsampling"),
    ("D", "Choreographer", "Skipped 3 frames! The application may be doing too much work on its main thread."),
]
WEATHER = [
    ("D", "LocationClient", "Last known location accuracy=12.0m age=41s"),
    ("I", "ForecastApi", "Fetching forecast for 51.5072,-0.1276"),
    ("W", "ForecastApi", "Request timed out after 10000ms, retrying (attempt 2/3)"),
    ("E", "ForecastApi", "Forecast request failed: java.net.SocketTimeoutException: timeout"),
    ("I", "WidgetProvider", "Updated 2 home screen widgets"),
]
CRASH = [
    "FATAL EXCEPTION: main",
    "Process: com.example.notes, PID: 2841",
    "java.lang.IllegalStateException: Note 117 has no attachment directory",
    "\tat com.example.notes.data.AttachmentStore.dirFor(AttachmentStore.kt:58)",
    "\tat com.example.notes.ui.EditorViewModel.loadAttachments(EditorViewModel.kt:131)",
    "\tat com.example.notes.ui.EditorFragment.onViewCreated(EditorFragment.kt:74)",
    "\tat androidx.fragment.app.Fragment.performViewCreated(Fragment.java:3128)",
    "\tat android.os.Handler.dispatchMessage(Handler.java:106)",
    "\tat android.os.Looper.loop(Looper.java:288)",
    "\tat android.app.ActivityThread.main(ActivityThread.java:7918)",
    "Caused by: java.io.FileNotFoundException: /data/user/0/com.example.notes/files/att/117",
    "\t... 7 more",
]


def line(t: datetime, uid: int, pid: int, tid: int, level: str, tag: str, msg: str) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} {uid:5d} {pid:5d} {tid:5d} {level} {tag:<8}: {msg}"


def generate(n: int = 4000, seed: int = 7) -> list[str]:
    rnd = random.Random(seed)
    t = datetime(2026, 10, 9, 9, 30, 0)
    out = [
        line(t, 1000, 1080, 1131, "I", "ActivityManager",
             f"Start proc {pid}:{pkg}/u0a{uid - 10000} for top-activity {{{pkg}/.MainActivity}}")
        for pid, (pkg, uid) in APPS.items()
    ]
    crash_at = int(n * 0.8)
    for i in range(n):
        t += timedelta(milliseconds=rnd.randint(1, 40))
        r = rnd.random()
        if i == crash_at:
            for msg in CRASH:
                out.append(line(t, 10211, 2841, 2841, "E", "AndroidRuntime", msg))
            out.append(line(t, 1000, 1080, 1102, "W", "ActivityManager",
                            "Force finishing activity com.example.notes/.MainActivity"))
            continue
        if r < 0.45:
            pid, level, tag, msg = rnd.choice(NOISE)
            uid = SYSTEM[pid][1]
            tid = pid + rnd.randint(0, 60)
        elif r < 0.8:
            pid, uid = 2841, 10211
            level, tag, msg = rnd.choice(NOTES)
            tid = pid if rnd.random() < 0.6 else pid + rnd.randint(10, 40)
        else:
            pid, uid = 3107, 10187
            level, tag, msg = rnd.choice(WEATHER)
            tid = pid + rnd.randint(0, 20)
        out.append(line(t, uid, pid, tid, level, tag, msg))
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "demo.log"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(generate()) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
