#!/usr/bin/env bash
# install.sh - echo-ai installer (adapted from the deskapp run_template.sh).
#
#   ./install.sh               install, or update an existing install
#   ./install.sh --uninstall   remove echo-ai completely, -R for short
#   ./install.sh --help        show usage
#
# Installs the shared copy at /opt/echo-ai (needs sudo, since it's a system
# directory shared by all users):
#
#   /opt/echo-ai/echo-ai      the launcher: /usr/local/bin/echo-ai and the
#                             app-menu entry point here; it only starts
#                             echo-ai.py, passing its options through
#   /opt/echo-ai/install.sh   a copy of this script, so 'echo-ai --uninstall'
#                             works once the source folder is gone
#   /opt/echo-ai/.venv        Python venv, kept across updates
#
# Each user's own data (just the log) lives separately, in
# ~/.local/state/echo-ai/, not under /opt.
#
# Unlike the web-app deskapps there's no server or port: echo-ai is a
# background process that echo-ai --show talks to over a Unix socket.
# Starting it at login and binding the shortcut are left to the user.

set -euo pipefail

COMMAND_NAME="echo-ai"
INSTALL_DIR="/opt/echo-ai"
LAUNCHER="$INSTALL_DIR/$COMMAND_NAME"
APP_FILE="echo-ai.py"
UI_FILE="echo-ai.html"
REQ_FILE="requirements.txt"
DESKTOP_FILE="/usr/local/share/applications/$COMMAND_NAME.desktop"
# pacman packages echo-ai needs: GTK/WebKit for the popup, xclip/xprop and the
# X libraries it loads with ctypes for reading the selection
SYSTEM_PKGS=(python python-gobject gtk3 webkit2gtk-4.1 xclip xorg-xprop libx11 libxfixes libxtst)

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()  { echo -e "${GREEN}[*]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; }

confirm() {
    local reply
    read -r -p "$1 [y/n]: " reply
    case "$reply" in
        y|Y|yes|Yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

usage() {
    cat <<'EOF'
install.sh - install, update or remove echo-ai

usage:
  ./install.sh                  install echo-ai, or update an existing install
                                (needs sudo, since it installs/updates the
                                shared copy at /opt/echo-ai)
  ./install.sh --uninstall, -R  remove echo-ai completely (app dir + symlink +
                                app-menu entry); same as 'echo-ai --uninstall'
  ./install.sh --help           show this help

once installed, see 'echo-ai --help' for how to use it.
EOF
}

stop_running() {
    # Stop this user's background process, if any, so it doesn't keep
    # running old (or removed) code. Prints "was running" if it was.
    local python="$INSTALL_DIR/.venv/bin/python"
    [ -x "$python" ] && [ -f "$INSTALL_DIR/$APP_FILE" ] || return 0
    if pgrep -u "$(id -u)" -f "$INSTALL_DIR/$APP_FILE\$" >/dev/null 2>&1; then
        "$python" "$INSTALL_DIR/$APP_FILE" --stop >/dev/null 2>&1 || true
        echo "was running"
    fi
}

uninstall() {
    local symlink_path="/usr/local/bin/$COMMAND_NAME"

    warn "This will remove:"
    [ -d "$INSTALL_DIR" ]  && echo "    $INSTALL_DIR (app + venv, needs sudo - system directory)"
    [ -L "$symlink_path" ] && echo "    $symlink_path (symlink, needs sudo)"
    [ -f "$DESKTOP_FILE" ] && echo "    $DESKTOP_FILE (app-menu entry, needs sudo)"
    if [ ! -d "$INSTALL_DIR" ] && [ ! -L "$symlink_path" ] && [ ! -f "$DESKTOP_FILE" ]; then
        info "Nothing to remove - $COMMAND_NAME is not installed."
        exit 0
    fi
    warn "Your echo-ai log is never touched by this uninstall (it lives in ~/.local/state/echo-ai/, not under $INSTALL_DIR)."
    warn "If you start echo-ai at login or bound a key to it, remove that yourself."
    if ! confirm "Remove $COMMAND_NAME completely?"; then
        error "Aborted."
        exit 1
    fi

    [ -n "$(stop_running)" ] && info "Stopped the running $COMMAND_NAME"

    if [ -L "$symlink_path" ]; then
        local target
        target="$(readlink -f "$symlink_path" 2>/dev/null || true)"
        if [ "$target" = "$LAUNCHER" ]; then
            warn "Removing $symlink_path needs sudo, since it is a system directory."
            sudo rm "$symlink_path"
            info "Removed $symlink_path"
        else
            warn "$symlink_path points elsewhere ($target) - leaving it untouched."
        fi
    fi

    if [ -f "$DESKTOP_FILE" ]; then
        sudo rm "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 && sudo update-desktop-database -q "$(dirname "$DESKTOP_FILE")" || true
        info "Removed $DESKTOP_FILE"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        warn "Removing $INSTALL_DIR needs sudo, since it is a system directory."
        sudo rm -rf "$INSTALL_DIR"
        info "Removed $INSTALL_DIR"
    fi

    info "$COMMAND_NAME removed."
    exit 0
}

check_system_deps() {
    if ! command -v pacman >/dev/null 2>&1; then
        warn "pacman not found - make sure these (or your distro's equivalents) are installed: ${SYSTEM_PKGS[*]}"
        return
    fi
    local missing=() pkg
    for pkg in "${SYSTEM_PKGS[@]}"; do
        pacman -Q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        warn "Missing: ${missing[*]} - installing with pacman (needs sudo, since it writes to system directories)."
        sudo pacman -S --needed --noconfirm "${missing[@]}"
    fi
    info "System dependencies OK"
    command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] ||
        warn "Claude Code ('claude') not found - echo-ai needs it to answer; the popup explains how to install it."
}

install_to_opt() {
    local source_dir="$1"

    if [ -d "$INSTALL_DIR" ]; then
        # Replace the app files but keep .venv, so already-installed
        # Python packages are reused (see install_deps).
        info "Updating existing install at $INSTALL_DIR (needs sudo; your $COMMAND_NAME log lives in ~/.local/state/echo-ai/, never under $INSTALL_DIR)"
        sudo find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
    else
        info "Installing to $INSTALL_DIR (needs sudo, since it's a system directory shared by all users)"
    fi

    sudo mkdir -p "$INSTALL_DIR"
    sudo cp "$source_dir/$APP_FILE"   "$INSTALL_DIR/$APP_FILE"
    sudo cp "$source_dir/$UI_FILE"    "$INSTALL_DIR/$UI_FILE"
    sudo cp "$source_dir/$REQ_FILE"   "$INSTALL_DIR/$REQ_FILE"
    sudo cp "$source_dir/install.sh"  "$INSTALL_DIR/install.sh"
    sudo tee "$LAUNCHER" >/dev/null <<EOF
#!/usr/bin/env bash
# echo-ai launcher, written by install.sh: runs echo-ai.py in its venv,
# except --uninstall, which install.sh handles.
case "\${1:-}" in
    --uninstall|-R) exec "$INSTALL_DIR/install.sh" --uninstall ;;
esac
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/$APP_FILE" "\$@"
EOF
    sudo chmod +x "$LAUNCHER" "$INSTALL_DIR/install.sh"
    # Readable by every user whatever the umask was when the files were copied
    sudo chmod -R a+rX "$INSTALL_DIR"
    info "Copied app files to $INSTALL_DIR"
}

manage_symlink() {
    local symlink_path="/usr/local/bin/$COMMAND_NAME"
    local target="$LAUNCHER"

    if [ -L "$symlink_path" ]; then
        local current_target
        current_target="$(readlink -f "$symlink_path" 2>/dev/null || true)"
        if [ "$current_target" = "$target" ]; then
            info "'$COMMAND_NAME' already points to installed copy - nothing to do"
            return
        fi
        warn "'$COMMAND_NAME' exists but points elsewhere ($current_target)."
        warn "Updating $symlink_path needs sudo, since it is a system directory."
        sudo ln -sf "$target" "$symlink_path"
        info "Updated: $COMMAND_NAME -> $target"
    elif [ -e "$symlink_path" ]; then
        error "$symlink_path exists and isn't a symlink this script manages - leaving it untouched."
    else
        warn "Creating $symlink_path needs sudo, since it is a system directory."
        sudo ln -s "$target" "$symlink_path"
        info "Created: $COMMAND_NAME -> $target (launch from anywhere: terminal, dmenu)"
    fi
}

install_desktop_file() {
    # App-menu entry for every user; it opens the assistant like the shortcut does
    sudo mkdir -p "$(dirname "$DESKTOP_FILE")"
    sudo tee "$DESKTOP_FILE" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Echo AI
GenericName=AI Assistant
Comment=Ask Claude about the text you have selected
Icon=dialog-question
Exec=$LAUNCHER --show
Terminal=false
Categories=Utility;
Keywords=claude;ai;assistant;selection;
StartupNotify=false
EOF
    sudo chmod 644 "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 && sudo update-desktop-database -q "$(dirname "$DESKTOP_FILE")" || true
    info "App-menu entry: $DESKTOP_FILE"
}

setup_venv() {
    local venv_dir="$INSTALL_DIR/.venv"
    if [ ! -d "$venv_dir" ]; then
        # --system-site-packages: GTK/WebKit (python-gobject) only come from pacman
        info "Creating virtual environment at $venv_dir (needs sudo, since $INSTALL_DIR is a system directory)"
        sudo python3 -m venv --system-site-packages "$venv_dir"
    fi
}

missing_requirements() {
    # Print each package from requirements.txt that the venv doesn't have yet
    local line pkg
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        line="$(echo "$line" | xargs)"
        [ -z "$line" ] && continue
        pkg="$(echo "$line" | sed -E 's/[<>=!~;[ ].*//')"
        if ! "$INSTALL_DIR/.venv/bin/pip" show "$pkg" >/dev/null 2>&1; then
            echo "$line"
        fi
    done < "$INSTALL_DIR/$REQ_FILE"
}

install_deps() {
    local missing=()
    mapfile -t missing < <(missing_requirements)
    if [ "${#missing[@]}" -eq 0 ]; then
        info "Python dependencies already satisfied"
    else
        info "Installing missing Python dependencies: ${missing[*]} (needs sudo, since the venv lives under $INSTALL_DIR)"
        sudo "$INSTALL_DIR/.venv/bin/pip" install "${missing[@]}"
    fi
}

next_steps() {
    cat <<EOF

  echo-ai is installed. Two things are up to you:

  1. A shortcut: bind  echo-ai --show  to a key (e.g. Super+I) in your
     window manager or desktop's keyboard settings.

  2. Optionally, start it at login: add  echo-ai --start &  to ~/.xinitrc
     (before the exec line), or your desktop's autostart settings.
     Without it, the first echo-ai --show starts it.

  See echo-ai --help for all commands.

EOF
}

main() {
    case "${1:-}" in
        "")                     ;;
        --help|-h)              usage; exit 0 ;;
        --uninstall|-R|--R)     uninstall ;;
        *)                      error "Unknown option $1"; usage; exit 1 ;;
    esac

    local source_dir
    source_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
    if [ "$source_dir" = "$INSTALL_DIR" ]; then
        error "This is the installed copy - run install.sh from the source folder to update."
        exit 1
    fi

    check_system_deps
    local was_running
    was_running="$(stop_running)"
    install_to_opt "$source_dir"
    manage_symlink
    install_desktop_file
    setup_venv
    install_deps
    if [ -n "$was_running" ]; then
        "$LAUNCHER" --start
        info "Restarted the running $COMMAND_NAME with the new version"
    fi
    next_steps
}

main "$@"
