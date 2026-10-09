#!/usr/bin/env sh
# adbcat installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/mr-ceo7/adbcat/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/mr-ceo7/adbcat/main/install.sh | sh -s -- --uninstall
#
# Installs into its own virtual environment (no sudo, no system packages touched):
#   ~/.local/share/adbcat/venv   the environment
#   ~/.local/bin/adbcat          the command (a symlink)
#
# Environment overrides:
#   ADBCAT_REF      branch or tag to install (default: main)
#   ADBCAT_SOURCE   pip install source, e.g. a local checkout (default: GitHub tarball of ADBCAT_REF)
#   ADBCAT_HOME     install directory (default: ~/.local/share/adbcat)
#   ADBCAT_BIN_DIR  where the command goes (default: ~/.local/bin)
#   PYTHON          interpreter to use (default: newest python3 found)

set -eu

REPO="mr-ceo7/adbcat"
REF="${ADBCAT_REF:-main}"
SOURCE="${ADBCAT_SOURCE:-https://github.com/$REPO/archive/refs/heads/$REF.tar.gz}"
INSTALL_DIR="${ADBCAT_HOME:-$HOME/.local/share/adbcat}"
BIN_DIR="${ADBCAT_BIN_DIR:-$HOME/.local/bin}"
VENV="$INSTALL_DIR/venv"

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; RED="$(printf '\033[31m')"; YELLOW="$(printf '\033[33m')"
    GREEN="$(printf '\033[32m')"; RESET="$(printf '\033[0m')"
else
    BOLD=""; RED=""; YELLOW=""; GREEN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

OS="$(uname -s)"

uninstall() {
    step "Removing adbcat"
    if [ -L "$BIN_DIR/adbcat" ] || [ -f "$BIN_DIR/adbcat" ]; then
        rm -f "$BIN_DIR/adbcat"
        say "  removed $BIN_DIR/adbcat"
    fi
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        say "  removed $INSTALL_DIR"
    fi
    say "Settings in ~/.config/adbcat were kept; delete that folder too if you want them gone."
    say "${GREEN}adbcat uninstalled.${RESET}"
}

case "${1:-}" in
    --uninstall|uninstall) uninstall; exit 0 ;;
    -h|--help)
        say "Usage: install.sh [--uninstall]"
        say "Installs adbcat into $INSTALL_DIR and links it as $BIN_DIR/adbcat."
        say "Env: ADBCAT_REF (branch/tag), ADBCAT_SOURCE, ADBCAT_HOME, ADBCAT_BIN_DIR, PYTHON"
        exit 0 ;;
    "") ;;
    *) die "unknown option: $1 (use --uninstall or --help)" ;;
esac

case "$OS" in
    Linux|Darwin) ;;
    *) die "this installer supports Linux and macOS. On other systems run: pip install $SOURCE" ;;
esac

# --- Python --------------------------------------------------------------------
python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

find_python() {
    if [ -n "${PYTHON:-}" ]; then
        python_ok "$PYTHON" || die "PYTHON=$PYTHON is not Python 3.9 or newer"
        echo "$PYTHON"; return
    fi
    for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$cand" >/dev/null 2>&1 && python_ok "$cand"; then
            command -v "$cand"; return
        fi
    done
    return 1
}

step "Checking Python"
if ! PY="$(find_python)"; then
    if [ "$OS" = Darwin ]; then
        die "Python 3.9+ not found. Install it with: brew install python"
    fi
    die "Python 3.9+ not found. Install it, e.g.: sudo apt install python3 python3-venv"
fi
say "  using $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    ver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    die "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python$ver-venv"
fi

# --- install -------------------------------------------------------------------
step "Creating environment in $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
if [ -x "$VENV/bin/python" ] && ! python_ok "$VENV/bin/python"; then
    rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV" || die "could not create a virtual environment"
fi

step "Installing adbcat ($REF)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV/bin/python" -m pip install --quiet --upgrade --force-reinstall --no-deps "$SOURCE" \
    || ! "$VENV/bin/python" -m pip install --quiet "$SOURCE"; then
    die "pip could not install adbcat from $SOURCE"
fi

ln -sf "$VENV/bin/adbcat" "$BIN_DIR/adbcat"
"$BIN_DIR/adbcat" --help >/dev/null 2>&1 || die "adbcat was installed but does not start"
VERSION="$("$VENV/bin/python" -c 'import adbcat; print(adbcat.__version__)' 2>/dev/null || echo "?")"
say "  installed adbcat $VERSION -> $BIN_DIR/adbcat"

# --- checks --------------------------------------------------------------------
NOTES=0
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        NOTES=1
        shell_rc="~/.profile"
        case "${SHELL:-}" in
            */zsh) shell_rc="~/.zshrc" ;;
            */bash) shell_rc="~/.bashrc" ;;
        esac
        warn "$BIN_DIR is not on your PATH. Add it with:"
        say  "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> $shell_rc"
        say  "  then open a new terminal."
        ;;
esac

if ! command -v adb >/dev/null 2>&1; then
    NOTES=1
    warn "adb was not found. adbcat needs it to read logs from a device."
    if [ "$OS" = Darwin ]; then
        say "  Install it with: brew install android-platform-tools"
    else
        say "  Install it with: sudo apt install adb    (or your distro's equivalent)"
    fi
    say "  If you have Android Studio, add its platform-tools folder to PATH instead."
fi

say ""
say "${GREEN}${BOLD}adbcat is installed.${RESET}"
[ "$NOTES" -eq 1 ] && say "Fix the warnings above, then:"
say "  adbcat              logs from the connected phone"
say "  adbcat --help       all options; press ? inside the app for keys"
say "Uninstall: curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | sh -s -- --uninstall"
