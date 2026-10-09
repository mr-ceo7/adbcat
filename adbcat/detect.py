"""Detect the Android app project in the current directory and its application id(s)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

GRADLE_FILES = ("build.gradle.kts", "build.gradle")
SETTINGS_FILES = ("settings.gradle.kts", "settings.gradle")
SKIP_DIRS = {"build", ".gradle", ".git", ".idea", "node_modules", ".cxx", "intermediates"}

APP_PLUGIN_RE = re.compile(
    r"""(com\.android\.application|plugins\.android\.application|apply\s+plugin:\s*['"]com\.android\.application)"""
    r"""(?![^\n]*apply\s+false)"""
)
APP_ID_RE = re.compile(r"""\bapplicationId\s*(?:=|\(|\s)\s*["']([A-Za-z][\w.]*)["']""")
SUFFIX_RE = re.compile(r"""\bapplicationIdSuffix\s*(?:=|\(|\s)\s*["'](\.?[\w.]+)["']""")
NAMESPACE_RE = re.compile(r"""\bnamespace\s*(?:=|\(|\s)\s*["']([A-Za-z][\w.]*)["']""")
MANIFEST_PKG_RE = re.compile(r"""<manifest[^>]*\spackage\s*=\s*["']([\w.]+)["']""", re.S)
LINE_COMMENT_RE = re.compile(r"(?m)^\s*//.*$")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


@dataclass
class Project:
    root: Path
    packages: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.root.name


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return LINE_COMMENT_RE.sub("", BLOCK_COMMENT_RE.sub("", text))


def find_project_root(start: Path) -> Path | None:
    """Walk up from `start` to the directory holding settings.gradle(.kts).

    Also looks in an `android/` subfolder (Flutter / React Native layouts).
    """
    start = start.resolve()
    home = Path.home().resolve()
    for d in [start, *start.parents]:
        for base in (d, d / "android"):
            if any((base / f).is_file() for f in SETTINGS_FILES):
                return base
        if d == home or d == d.parent:
            break
    return None


def _module_build_files(root: Path, max_depth: int = 3) -> list[Path]:
    found = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for f in GRADLE_FILES:
            if f in filenames:
                found.append(Path(dirpath) / f)
                break
    return found


def _ids_for_module(build_file: Path) -> list[str]:
    text = _read(build_file)
    if not (APP_PLUGIN_RE.search(text) or APP_ID_RE.search(text)):
        return []
    ids = list(dict.fromkeys(APP_ID_RE.findall(text)))
    if not ids:
        ns = NAMESPACE_RE.search(text)
        if ns:
            ids = [ns.group(1)]
    if not ids:
        manifest = build_file.parent / "src" / "main" / "AndroidManifest.xml"
        m = MANIFEST_PKG_RE.search(_read(manifest))
        if m:
            ids = [m.group(1)]
    # debug / flavor suffixes, e.g. applicationIdSuffix ".debug"
    for suffix in dict.fromkeys(SUFFIX_RE.findall(text)):
        suffix = suffix if suffix.startswith(".") else "." + suffix
        ids += [i + suffix for i in ids if not i.endswith(suffix)]
    return ids


def detect_project(start: Path | None = None) -> Project | None:
    """Return the Android app project around `start` (default: cwd), or None."""
    root = find_project_root(start or Path.cwd())
    if root is None:
        return None
    packages: list[str] = []
    for bf in _module_build_files(root):
        for pkg in _ids_for_module(bf):
            if pkg not in packages:
                packages.append(pkg)
    if not packages:
        return None
    return Project(root, packages)
