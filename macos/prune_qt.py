"""Removes the parts of PySide6 that Blurt does not use from a site-packages folder.

PySide6 with its add-ons is about 600 MB; Blurt needs QtCore, QtGui, QtWidgets and
QtMultimedia. The Qt frameworks to keep are found by following the libraries those modules
(and the plugins they load) really link against, so nothing they need is removed.

    python macos/prune_qt.py <site-packages>
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODULES = ("QtCore", "QtGui", "QtWidgets", "QtMultimedia", "QtNetwork")
PLUGIN_DIRS = ("platforms", "multimedia", "styles", "imageformats", "iconengines")
PLATFORMS = ("libqcocoa.dylib",)  # (the offscreen/minimal platforms are for testing only)


def linked(binary):
    out = subprocess.run(["otool", "-L", str(binary)], capture_output=True, text=True).stdout
    return [m.group(1) for m in re.finditer(r"^\s+(\S+) \(", out, re.M)]


def main(site):
    pyside = Path(site) / "PySide6"
    qt = pyside / "Qt"
    libdir = qt / "lib"
    keep_so = {f"{m}.abi3.so" for m in MODULES}
    roots = [pyside / name for name in keep_so if (pyside / name).exists()]
    roots += list(pyside.glob("libpyside6.abi3*.dylib"))
    for d in PLUGIN_DIRS:
        for f in (qt / "plugins" / d).glob("*.dylib"):
            if d != "platforms" or f.name in PLATFORMS:
                roots.append(f)

    # follow @rpath links into Qt/lib (frameworks and the FFmpeg dylibs)
    needed, todo = set(), list(roots)
    while todo:
        binary = todo.pop()
        for ref in linked(binary):
            if not ref.startswith(("@rpath/", "@loader_path/")):
                continue
            rel = ref.split("/", 1)[1]
            top = rel.split("/", 1)[0]          # QtCore.framework or libavcodec.63.dylib
            if top in needed:
                continue
            target = libdir / rel
            if not target.exists():
                continue
            needed.add(top)
            todo.append(target)

    removed = 0

    def rm(p):
        nonlocal removed
        if p.is_dir() and not p.is_symlink():
            removed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            removed += p.stat().st_size if p.is_file() and not p.is_symlink() else 0
            p.unlink()

    # Qt libraries nobody links against. (The FFmpeg dylibs come twice, libX.N.dylib and
    # an identical libX.N.M.dylib; only the name that is linked to is kept, plus the target
    # if it is a symbolic link.)
    keep = set(needed)
    for name in needed:
        if (libdir / name).is_symlink():
            keep.add((libdir / name).resolve().name)
    for p in libdir.iterdir():
        if p.name not in keep:
            rm(p)
    # plugins Blurt does not load
    for p in (qt / "plugins").iterdir():
        if p.name not in PLUGIN_DIRS:
            rm(p)
    for f in (qt / "plugins" / "platforms").glob("*"):
        if f.name not in PLATFORMS:
            rm(f)
    for name in ("qml", "translations", "metatypes", "libexec"):
        rm(qt / name)
    # Python bindings for other Qt modules, tools, apps, headers and type stubs
    for p in pyside.iterdir():
        if p.name in ("Qt", "__init__.py", "_config.py", "_git_pyside_version.py",
                      "py.typed", "support", "QtAsyncio") or p.name in keep_so:
            continue
        if p.name.startswith("libpyside6.abi3"):
            continue
        if p.suffix == ".so" or p.suffix == ".pyi" or p.suffix == ".app" or p.is_dir() \
                or p.name.startswith("libpyside6qml") or p.suffix == ".json" or \
                (p.is_file() and not p.suffix):  # command line tools (lupdate, qsb, ...)
            rm(p)
    print(f"PySide6: kept {', '.join(sorted(needed))}")
    print(f"PySide6: removed {removed / 1048576:.0f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
