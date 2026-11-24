"""Draws Blurt's app icon (a speech bubble with sound bars) and writes a macOS .icns file.

    python macos/make_icon.py out/Blurt.icns
"""
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtCore, QtGui  # noqa: E402

S = 1024


def draw(size):
    img = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.scale(size / S, size / S)

    # macOS icon grid: an 824 px rounded square centred on the 1024 px canvas, with a shadow
    body = QtCore.QRectF(100, 100, 824, 824)
    for i, alpha in enumerate((18, 14, 10, 6)):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, alpha))
        p.drawRoundedRect(body.translated(0, 6 + 4 * i).adjusted(-2 * i, -2 * i, 2 * i, 2 * i),
                          185, 185)
    grad = QtGui.QLinearGradient(body.topLeft(), body.bottomRight())
    grad.setColorAt(0.0, QtGui.QColor("#7c4dff"))
    grad.setColorAt(0.55, QtGui.QColor("#d6409f"))
    grad.setColorAt(1.0, QtGui.QColor("#ff8a3d"))
    p.setBrush(grad)
    p.drawRoundedRect(body, 185, 185)

    # speech bubble with a tail at the bottom left
    bubble = QtGui.QPainterPath()
    bubble.addRoundedRect(QtCore.QRectF(222, 262, 580, 420), 150, 150)
    tail = QtGui.QPainterPath()
    tail.moveTo(318, 600)
    tail.lineTo(262, 790)
    tail.lineTo(470, 660)
    tail.closeSubpath()
    bubble = bubble.united(tail)
    p.setBrush(QtGui.QColor(0, 0, 0, 40))
    p.drawPath(bubble.translated(0, 14))
    p.setBrush(QtGui.QColor("#ffffff"))
    p.drawPath(bubble)

    # sound bars
    heights = (110, 220, 300, 190, 260, 140)
    bar_w, gap = 46, 30
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = 512 - total / 2
    bars = QtGui.QLinearGradient(0, 330, 0, 620)
    bars.setColorAt(0.0, QtGui.QColor("#7c4dff"))
    bars.setColorAt(1.0, QtGui.QColor("#d6409f"))
    p.setBrush(bars)
    for h in heights:
        p.drawRoundedRect(QtCore.QRectF(x, 472 - h / 2, bar_w, h), bar_w / 2, bar_w / 2)
        x += bar_w + gap
    p.end()
    return img


def main(out):
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "Blurt.iconset")
    os.mkdir(iconset)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{base}x{base}" + ("@2x" if scale == 2 else "") + ".png"
            draw(base * scale).save(os.path.join(iconset, name))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Blurt.icns")
