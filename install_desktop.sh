#!/bin/sh
# Legt einen Starter für A3 Plot im Ubuntu-Anwendungsmenü an.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/a3plot.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=A3 Plot
Comment=SVG-Dateien mit dem EleksMaker A3 Pro plotten
Exec=/usr/bin/python3 "$DIR/a3plot_gui.py" %f
Path=$DIR
Icon=printer
Terminal=false
Categories=Graphics;Utility;
MimeType=image/svg+xml;
DESKTOP
update-desktop-database "$APPS" 2>/dev/null || true
echo "Starter angelegt: $APPS/a3plot.desktop"
