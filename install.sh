#!/usr/bin/env bash
# Installs Blurt for the current user only (no root, nothing outside your home folder).
# Run it from the folder that contains blurt.py:   bash install.sh
# Add --ai-voice to also install the AI voice engine (PyTorch, about 1 GB).
set -euo pipefail
AI_VOICE=0
for arg in "$@"; do
    case "$arg" in
        --ai-voice) AI_VOICE=1 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/blurt"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

command -v python3 >/dev/null || { echo "python3 is required."; exit 1; }
command -v pactl >/dev/null || echo "Warning: 'pactl' not found - the virtual microphone needs it (package: libpulse / pulseaudio-utils)."

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp "$HERE/blurt.py" "$APP_DIR/blurt.py"
rm -rf "$APP_DIR/blurt_voice"
cp -r "$HERE/blurt_voice" "$APP_DIR/blurt_voice"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "Creating Python environment…"
    python3 -m venv "$APP_DIR/venv"
fi
echo "Installing Qt (PySide6) and numpy - this is a large download the first time…"
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade PySide6 numpy

if [ "$AI_VOICE" = 1 ]; then
    echo "Installing the AI voice engine (PyTorch for CPU, transformers, faiss)…"
    if ! "$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade torch \
            --index-url https://download.pytorch.org/whl/cpu; then
        echo "The small CPU-only PyTorch download failed; installing the default build instead"
        echo "(this one is several GB because it includes NVIDIA libraries)."
        "$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade torch
    fi
    "$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade transformers scipy faiss-cpu
fi

cat > "$BIN_DIR/blurt" <<EOF
#!/bin/sh
exec "$APP_DIR/venv/bin/python" "$APP_DIR/blurt.py" "\$@"
EOF
chmod +x "$BIN_DIR/blurt"

cat > "$DESKTOP_DIR/blurt.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Blurt
Comment=Soundboard that plays audio through your microphone
Exec=$BIN_DIR/blurt
Icon=audio-input-microphone
Categories=AudioVideo;Audio;
Terminal=false
EOF

echo
echo "Done. Start it from your app menu (Blurt) or run: $BIN_DIR/blurt"
if [ "$AI_VOICE" = 0 ] && ! "$APP_DIR/venv/bin/python" -c "import torch" 2>/dev/null; then
    echo "AI voices (RVC models) are optional. To enable them: bash install.sh --ai-voice"
fi
if ! id -nG | tr ' ' '\n' | grep -qx input; then
    echo
    echo "For hotkeys that work while a game is focused, run this once and then log out/in:"
    echo "    sudo usermod -aG input $USER"
fi
