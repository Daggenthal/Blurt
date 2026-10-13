#!/usr/bin/env bash
# Installs Blurt for the current user only (no root, nothing outside your home folder).
# Run it from the folder that contains blurt.py:   bash install.sh
# Add --ai-voice to also install the AI voice engine (PyTorch for CPU, about 1 GB), or
# --ai-voice-gpu for the AMD graphics card build (ROCm, several GB, much faster live voices).
set -euo pipefail
AI_VOICE=0
GPU=0
for arg in "$@"; do
    case "$arg" in
        --ai-voice) AI_VOICE=1 ;;
        --ai-voice-gpu) AI_VOICE=1; GPU=1 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/blurt"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

command -v python3 >/dev/null || { echo "python3 is required."; exit 1; }
command -v pactl >/dev/null || echo "Warning: 'pactl' not found - the virtual microphone needs it (package: libpulse / pulseaudio-utils)."

PY="$APP_DIR/venv/bin/python"
# pip with a visible progress bar for the big downloads (the AI engine is hundreds of MB)
pip_install() { "$PY" -m pip install --progress-bar on --disable-pip-version-check "$@"; }
step() { echo; echo "== $* =="; }

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp "$HERE/blurt.py" "$APP_DIR/blurt.py"
rm -rf "$APP_DIR/blurt_voice"
cp -r "$HERE/blurt_voice" "$APP_DIR/blurt_voice"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "Creating Python environment…"
    python3 -m venv "$APP_DIR/venv"
fi
step "Installing Qt (PySide6) and numpy (about 200 MB the first time)"
"$PY" -m pip install --quiet --upgrade pip
pip_install --upgrade PySide6 numpy

if [ "$AI_VOICE" = 1 ]; then
    INSTALLED=0
    if [ "$GPU" = 1 ]; then
        step "Installing the AI voice engine for AMD graphics cards (PyTorch ROCm, several GB, this takes a while)"
        # A CPU build that is already installed may have a newer version number than the ROCm
        # build, and pip would then keep it. Remove it first.
        if "$APP_DIR/venv/bin/python" -c "import torch, sys; sys.exit(0 if torch.version.hip else 1)" 2>/dev/null; then
            INSTALLED=1
            echo "PyTorch for ROCm is already installed."
        else
            "$APP_DIR/venv/bin/python" -m pip uninstall --quiet -y torch 2>/dev/null || true
        fi
        for rocm in rocm7.2 rocm7.1 rocm7.0 rocm6.4 rocm6.3; do
            [ "$INSTALLED" = 1 ] && break
            echo "Trying PyTorch for $rocm…"
            if pip_install --no-cache-dir torch --index-url "https://download.pytorch.org/whl/$rocm"; then
                INSTALLED=1
                echo "Installed PyTorch for $rocm."
            fi
        done
        [ "$INSTALLED" = 1 ] || echo "The ROCm build could not be installed; using the CPU build."
        if [ -e /dev/kfd ] && { [ ! -r /dev/kfd ] || [ ! -w /dev/kfd ]; }; then
            echo "Note: the graphics card may need your user in the 'render' group:"
            echo "    sudo usermod -aG render,video $USER   (then log out and back in)"
        fi
    fi
    [ "$INSTALLED" = 1 ] || step "Installing the AI voice engine: PyTorch for CPU (about 200 MB)"
    if [ "$INSTALLED" = 0 ] && ! pip_install --upgrade torch \
            --index-url https://download.pytorch.org/whl/cpu; then
        echo "The small CPU-only PyTorch download failed; installing the default build instead"
        echo "(this one is several GB because it includes NVIDIA libraries)."
        pip_install --upgrade torch
    fi
    step "Installing transformers, scipy and faiss (about 100 MB)"
    pip_install --upgrade transformers scipy faiss-cpu
    if [ "$GPU" = 1 ]; then
        step "Checking the graphics card"
        "$PY" -W ignore - <<'PY' || true
import os, sys
sys.path.insert(0, os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share")) + "/blurt")
from blurt_voice.rvc import engine
device = engine.pick_device(True)
lines, advice = engine.gpu_diagnosis()
print("  " + "\n  ".join(lines))
print("  Result: " + ("the graphics card works." if device == "cuda" else "Blurt will use the CPU."))
for a in advice:
    print("  -> " + a)
PY
    fi
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
