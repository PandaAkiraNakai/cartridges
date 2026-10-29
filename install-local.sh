#!/usr/bin/env bash
# Build this branch and install it into ~/.local/opt, side by side with the
# system package: it uses its own GSettings schemas and overrides the desktop
# entry so launchers open this build. Run again after changing the code.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="$SRC/_build"
PREFIX="$HOME/.local/opt/cartridges-rewrite"
APP_ID="page.kramo.Cartridges"
BIN="$HOME/.local/bin/cartridges-rewrite"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"

if [[ -d "$BUILD" ]]; then
    meson setup --reconfigure "$BUILD" "$SRC" --prefix="$PREFIX" -Dprofile=release
else
    meson setup "$BUILD" "$SRC" --prefix="$PREFIX" -Dprofile=release
fi
# The blueprint target outputs a whole directory, whose modification time
# changes with every build, so Ninja doesn't notice when the .blp files change:
# make them newer than it to always recompile them
touch "$SRC"/cartridges/ui/*.blp
meson install -C "$BUILD" --skip-subprojects
glib-compile-schemas "$PREFIX/share/glib-2.0/schemas"

site_packages="$(find "$PREFIX/lib" -maxdepth 3 -type d -name site-packages | head -n1)"

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$HOME/.local/share/icons"
cat > "$BIN" <<EOF
#!/bin/sh
export PYTHONPATH="$site_packages\${PYTHONPATH:+:\$PYTHONPATH}"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
exec "$PREFIX/bin/cartridges" "\$@"
EOF
chmod +x "$BIN"

sed -e "s|^Exec=.*|Exec=$BIN|" -e "s|^DBusActivatable=.*|DBusActivatable=false|" \
    "$PREFIX/share/applications/$APP_ID.desktop" > "$DESKTOP"
# Copy only the app icons: copying the whole directory would replace the
# user's icon-theme.cache and hide every other icon installed there
for size in scalable symbolic; do
    mkdir -p "$HOME/.local/share/icons/hicolor/$size/apps"
    cp "$PREFIX/share/icons/hicolor/$size/apps/"* "$HOME/.local/share/icons/hicolor/$size/apps/"
done
gtk4-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" || true
update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null || true

echo "Installed: $BIN"
