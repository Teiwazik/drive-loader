#!/bin/sh
# Install Drive Loader for the current user (no root needed).
#   ./install.sh            install or update
#   ./install.sh --uninstall remove the app (your jobs and settings stay)
set -e
cd "$(dirname "$0")"

APP_ID=io.github.teiwazik.DriveLoader
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BINDIR="$HOME/.local/bin"
APPDIR="$DATA/driveloader"
ICONDIR="$DATA/icons/hicolor/scalable/apps"
DESKTOP="$DATA/applications/$APP_ID.desktop"

refresh() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
}

if [ "$1" = "--uninstall" ]; then
    systemctl --user disable driveloader-resume.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/driveloader-resume.service" \
          "$HOME/.config/autostart/$APP_ID.desktop" \
          "$BINDIR/driveloader" "$DESKTOP" "$ICONDIR/$APP_ID"*.svg
    rm -rf "$APPDIR"
    systemctl --user daemon-reload 2>/dev/null || true
    refresh
    echo "Drive Loader removed. Jobs and settings are kept in ~/.config/driveloader."
    exit 0
fi

missing=""
command -v python3 >/dev/null || missing="$missing python3"
command -v rclone >/dev/null || missing="$missing rclone"
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' 2>/dev/null \
    || missing="$missing python-gobject+gtk4+libadwaita"
[ -n "$missing" ] && echo "Warning: missing dependencies:$missing (see README)" >&2

install -Dm644 driveloader.py "$APPDIR/driveloader.py"
install -d "$APPDIR/i18n"
install -m644 i18n/*.json "$APPDIR/i18n/"
install -d "$BINDIR" "$ICONDIR" "$(dirname "$DESKTOP")"
printf '#!/bin/sh\nexec python3 "%s/driveloader.py" "$@"\n' "$APPDIR" > "$BINDIR/driveloader"
chmod 755 "$BINDIR/driveloader"
install -m644 data/icons/*.svg "$ICONDIR/"
sed "s|@BINDIR@|$BINDIR|" data/$APP_ID.desktop.in > "$DESKTOP"
refresh
echo "Drive Loader installed. Start it from the app menu or run: $BINDIR/driveloader"
