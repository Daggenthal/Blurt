#!/usr/bin/env bash
# Installs Blurt for the current user only (no root, nothing outside your home folder).
# Run it from the folder that contains blurt.py:   bash install.sh
# Add --ai-voice to also install the AI voice engine (PyTorch for CPU, about 1 GB), or
# --ai-voice-gpu for the AMD graphics card build (ROCm, several GB, much faster live voices).
# The options you used are remembered, so later "bash install.sh" keeps the AI voice engine.
#
# Blurt brings its own Python (via uv) instead of using the system one, so SteamOS updates
# that change or strip the system Python cannot break it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/blurt"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
OPTIONS_FILE="$APP_DIR/install-options"
PYTHON_VERSION="3.12"

AI_VOICE=0
GPU=0
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ] && [ -f "$OPTIONS_FILE" ]; then
    read -r -a ARGS < "$OPTIONS_FILE" || true
    [ ${#ARGS[@]} -gt 0 ] && echo "Using the options from last time: ${ARGS[*]}"
fi
for arg in "${ARGS[@]+"${ARGS[@]}"}"; do
    case "$arg" in
        --ai-voice) AI_VOICE=1 ;;
        --ai-voice-gpu) AI_VOICE=1; GPU=1 ;;
        --no-ai-voice) AI_VOICE=0; GPU=0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

step() { echo; echo "== $* =="; }
command -v pactl >/dev/null || echo "Warning: 'pactl' not found - the virtual microphone needs it (package: libpulse / pulseaudio-utils)."

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
if [ "$HERE" != "$APP_DIR" ]; then  # (when repairing, the installer runs from APP_DIR itself)
    cp "$HERE/blurt.py" "$APP_DIR/blurt.py"
    rm -rf "$APP_DIR/blurt_voice"
    cp -r "$HERE/blurt_voice" "$APP_DIR/blurt_voice"
    cp "$HERE/install.sh" "$APP_DIR/install.sh"
fi
OPTS=()
[ "$GPU" = 1 ] && OPTS=(--ai-voice-gpu)
[ "$GPU" = 0 ] && [ "$AI_VOICE" = 1 ] && OPTS=(--ai-voice)
echo "${OPTS[*]+"${OPTS[*]}"}" > "$OPTIONS_FILE"

# ---- uv: a small, self-contained tool that provides Python and installs packages ----------
UV="$APP_DIR/bin/uv"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"   # Blurt's own Python lives here
export UV_PYTHON_PREFERENCE=only-managed          # never use (or depend on) the system one
if [ ! -x "$UV" ] || ! "$UV" --version >/dev/null 2>&1; then
    step "Downloading uv (provides Blurt's own Python, about 20 MB)"
    case "$(uname -m)" in
        x86_64) TRIPLE=x86_64-unknown-linux-gnu ;;
        aarch64|arm64) TRIPLE=aarch64-unknown-linux-gnu ;;
        *) echo "Unsupported CPU type: $(uname -m)"; exit 1 ;;
    esac
    URL="https://github.com/astral-sh/uv/releases/latest/download/uv-$TRIPLE.tar.gz"
    TMP="$(mktemp -d)"
    if command -v curl >/dev/null; then
        curl -fL --progress-bar "$URL" -o "$TMP/uv.tar.gz"
    else
        wget -q --show-progress "$URL" -O "$TMP/uv.tar.gz"
    fi
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    mkdir -p "$APP_DIR/bin"
    install -m 755 "$TMP/uv-$TRIPLE/uv" "$UV"
    rm -rf "$TMP"
fi

VENV="$APP_DIR/venv"
PY="$VENV/bin/python"
MARKER="$VENV/.blurt-own-python"
# An older Blurt built its environment on the system Python; rebuild it on Blurt's own.
# Also rebuild it if it has stopped working for any reason.
if [ -d "$VENV" ] && { [ ! -f "$MARKER" ] || ! "$PY" -c "import sys" >/dev/null 2>&1; }; then
    step "Rebuilding Blurt's Python environment (the old one depended on the system Python)"
    rm -rf "$VENV"
fi
if [ ! -x "$PY" ]; then
    step "Setting up Python $PYTHON_VERSION for Blurt"
    "$UV" python install --no-bin "$PYTHON_VERSION"
    "$UV" venv --python "$PYTHON_VERSION" "$VENV"
    touch "$MARKER"
fi
pkg() { "$UV" pip install --python "$PY" "$@"; }

step "Installing Qt (PySide6) and numpy (about 200 MB the first time)"
FIX=()
"$PY" -c "import PySide6, numpy" >/dev/null 2>&1 || FIX=(--reinstall)  # repair damaged files
pkg --upgrade "${FIX[@]+"${FIX[@]}"}" PySide6 numpy

if [ "$AI_VOICE" = 1 ]; then
    INSTALLED=0
    if [ "$GPU" = 1 ]; then
        step "Installing the AI voice engine for AMD graphics cards (PyTorch ROCm, several GB, this takes a while)"
        if "$PY" -c "import torch, sys; sys.exit(0 if torch.version.hip else 1)" 2>/dev/null; then
            INSTALLED=1
            echo "PyTorch for ROCm is already installed."
        else
            # a CPU build can have a newer version number than the ROCm one; remove it first
            "$UV" pip uninstall --python "$PY" torch >/dev/null 2>&1 || true
        fi
        for rocm in rocm7.2 rocm7.1 rocm7.0 rocm6.4 rocm6.3; do
            [ "$INSTALLED" = 1 ] && break
            echo "Trying PyTorch for $rocm…"
            if pkg torch --index-url "https://download.pytorch.org/whl/$rocm"; then
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
    if [ "$INSTALLED" = 0 ]; then
        step "Installing the AI voice engine: PyTorch for CPU (about 200 MB)"
        if ! pkg --upgrade torch --index-url https://download.pytorch.org/whl/cpu; then
            echo "The small CPU-only PyTorch download failed; installing the default build instead"
            echo "(this one is several GB because it includes NVIDIA libraries)."
            pkg --upgrade torch
        fi
    fi
    step "Installing transformers, scipy and faiss (about 100 MB)"
    pkg --upgrade transformers scipy faiss-cpu
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

step "Checking the installation"
"$PY" -c "import PySide6, numpy; print('  Python', __import__('sys').version.split()[0], '| PySide6', PySide6.__version__)"

# The launcher checks the environment first. If anything ever breaks it again, it offers to
# repair it instead of failing silently.
cat > "$BIN_DIR/blurt" <<EOF
#!/bin/sh
APP="$APP_DIR"
PY="\$APP/venv/bin/python"
if ! "\$PY" -c "import PySide6" >/dev/null 2>&1; then
    MSG="Blurt's Python environment is not working (this can happen after a system update). Repair it now? It downloads about 200 MB, more with the AI voice engine."
    echo "\$MSG" >&2
    if [ -t 0 ] && [ -t 1 ]; then
        printf "Repair now? [Y/n] "
        read -r answer
        case "\$answer" in n*|N*) exit 1 ;; esac
        bash "\$APP/install.sh" || exit 1
    elif command -v kdialog >/dev/null 2>&1 && kdialog --title Blurt --yesno "\$MSG"; then
        if command -v konsole >/dev/null 2>&1; then
            konsole --hold -e bash -c 'bash "\$0/install.sh"; echo; echo "You can close this window now."' "\$APP"
        else
            bash "\$APP/install.sh"
        fi
    else
        command -v notify-send >/dev/null 2>&1 && \\
            notify-send "Blurt" "Blurt needs repairing. Run: bash \$APP/install.sh"
        exit 1
    fi
fi
exec "\$PY" "\$APP/blurt.py" "\$@"
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
if [ "$AI_VOICE" = 0 ] && ! "$PY" -c "import torch" 2>/dev/null; then
    echo "AI voices (RVC models) are optional. To enable them: bash install.sh --ai-voice"
fi
if ! id -nG | tr ' ' '\n' | grep -qx input; then
    echo
    echo "For hotkeys that work while a game is focused, run this once and then log out/in:"
    echo "    sudo usermod -aG input $USER"
fi
