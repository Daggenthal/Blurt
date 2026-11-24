"""Blurt's updater: finds newer versions on GitHub and installs them.

A version is a commit on the main branch of github.com/Daggenthal/Blurt.
  * Linux: Blurt downloads that commit's source and runs its install.sh, which updates
    ~/.local/share/blurt with the options used last time (e.g. the AI voice engine), and
    records the commit in ~/.local/share/blurt/version.
  * macOS: GitHub builds Blurt.app for every commit (.github/workflows/mac-app.yml) and
    publishes it as the release "mac-latest". Blurt downloads it, checks its signature,
    and a small script swaps it in once Blurt has quit, then opens the new version.

No Qt here; blurt.py shows the dialog. Everything that waits on the network runs on a
background thread there.
"""
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Daggenthal/Blurt"
API = f"https://api.github.com/repos/{REPO}"
BRANCH = "main"
MAC_TAG = "mac-latest"
IS_MAC = sys.platform == "darwin"
HERE = Path(__file__).resolve().parent
LINUX_APP_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "blurt"


class UpdateError(Exception):
    """Something went wrong; the message is shown to the user as is."""


# ---- where Blurt runs from, and which version it is ----------------------------------------
def mac_bundle():
    """The Blurt.app this Blurt runs from, or None (running from source)."""
    exe = Path(sys.executable).resolve()
    if len(exe.parents) > 2 and exe.parents[1].name == "Contents" and exe.parents[2].suffix == ".app":
        return exe.parents[2]
    return None


def current_commit():
    """The commit this Blurt was built or installed from; None if unknown."""
    try:
        if IS_MAC:
            app = mac_bundle()
            if app:
                with open(app / "Contents" / "Info.plist", "rb") as f:
                    sha = plistlib.load(f).get("BlurtCommit", "")
                return sha if len(sha) == 40 else None
            return None
        sha = (LINUX_APP_DIR / "version").read_text().strip()
        return sha if len(sha) == 40 else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def can_install():
    """(True, "") if this copy can update itself, else (False, why not)."""
    if IS_MAC:
        app = mac_bundle()
        if app is None:
            return False, "Blurt is running from its source code: update it with git pull."
        if "/AppTranslocation/" in str(app):
            return False, ("macOS runs Blurt from a temporary place. Move Blurt.app into "
                           "Applications, open it from there, then update.")
        if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
            return False, f"Blurt cannot replace itself in {app.parent} (no permission there)."
        if platform.machine() != "arm64":
            return False, "Updates are built for Apple silicon Macs only, for now."
        return True, ""
    if HERE != LINUX_APP_DIR.resolve():
        return False, ("Blurt is running from its source folder, not from its installation: "
                       "update it with git pull and bash install.sh.")
    return True, ""


# ---- GitHub ------------------------------------------------------------------------------
def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Blurt-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 403:
            raise UpdateError("GitHub is limiting how often Blurt may ask. Try again in an "
                              "hour.") from e
        raise UpdateError(f"GitHub answered with an error ({e.code}).") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError("Could not reach GitHub. Are you online?") from e


def _first_line(message):
    return (message or "").strip().splitlines()[0] if message and message.strip() else ""


def check():
    """What is newer than this Blurt. Returns a dict:
    available (bool), current, latest (commit SHAs), date, changes [(short sha, title)],
    behind (number of new commits, None if unknown), download (Mac zip URL), size, note."""
    current = current_commit()
    info = {"available": False, "current": current, "latest": None, "date": "",
            "changes": [], "behind": None, "download": None, "size": 0, "note": ""}
    if IS_MAC:
        rel = _get(f"{API}/releases/tags/{MAC_TAG}")
        if not rel:
            info["note"] = "No Mac build has been published on GitHub yet."
            return info
        latest = rel.get("target_commitish", "")
        want = f"Blurt-macos-{platform.machine()}.zip"
        asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
        if len(latest) != 40 or asset is None:
            info["note"] = "The newest Mac build is still being published. Try again soon."
            return info
        info.update(latest=latest, date=rel.get("published_at", ""),
                    download=asset.get("browser_download_url"), size=asset.get("size", 0))
    else:
        head = _get(f"{API}/commits/{BRANCH}")
        if not head:
            raise UpdateError("Could not find Blurt on GitHub.")
        info.update(latest=head["sha"],
                    date=head.get("commit", {}).get("committer", {}).get("date", ""))
    latest = info["latest"]
    if current == latest:
        return info
    cmp = _get(f"{API}/compare/{current}...{latest}") if current else None
    if cmp:
        if cmp.get("status") in ("identical", "behind"):
            return info  # this copy is as new as GitHub's, or newer
        commits = cmp.get("commits", [])
        info["behind"] = cmp.get("ahead_by", len(commits))
    else:  # unknown version (installed before Blurt could update itself)
        commits = list(reversed(_get(f"{API}/commits?sha={latest}&per_page=10") or []))
    info["changes"] = [(c["sha"][:7], _first_line(c.get("commit", {}).get("message")))
                       for c in reversed(commits)][:30]  # newest first
    info["available"] = True
    return info


def _download(url, dest, progress, what="Downloading"):
    req = urllib.request.Request(url, headers={"User-Agent": "Blurt-updater"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    progress(what, done, total)
            progress(what, done, total)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"The download failed: {e}") from e


# ---- Linux -------------------------------------------------------------------------------
def install_linux(info, log, progress):
    """Downloads the new source and runs its install.sh. log(line) gets the installer's
    output. Returns when done; raises UpdateError on failure."""
    sha = info["latest"]
    with tempfile.TemporaryDirectory(prefix="blurt-update-") as tmp:
        tgz = Path(tmp) / "blurt.tar.gz"
        _download(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", tgz, progress)
        log("Unpacking…")
        try:
            with tarfile.open(tgz) as t:
                t.extractall(tmp, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise UpdateError(f"The download is damaged: {e}") from e
        src = next((d for d in Path(tmp).iterdir() if (d / "install.sh").is_file()), None)
        if src is None:
            raise UpdateError("The download does not contain Blurt's installer.")
        log("Running the installer (it keeps your options, e.g. the AI voice engine)…")
        env = dict(os.environ, BLURT_COMMIT=sha)
        proc = subprocess.Popen(["bash", "install.sh"], cwd=src, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        for line in proc.stdout:
            log(line.rstrip())
        if proc.wait() != 0:
            raise UpdateError("The installer failed (see the messages above). Blurt was not "
                              "changed, or only partly; running the update again fixes that.")
    try:  # (installers from before Blurt could update itself do not record it)
        (LINUX_APP_DIR / "version").write_text(sha + "\n")
    except OSError:
        pass


def restart_command():
    """How to start this Blurt again after an update."""
    return [sys.executable, str(HERE / "blurt.py")]


# ---- macOS -------------------------------------------------------------------------------
def _codesign(*args):
    return subprocess.run(["codesign", *args], capture_output=True, text=True)


def _requirement(app):
    """This app's designated requirement when it is signed with a certificate (so the new
    version must come from the same signer); None for ad-hoc signed (self-built) copies."""
    r = _codesign("-d", "-r-", str(app))
    text = (r.stdout + r.stderr).strip()
    line = next((l for l in text.splitlines() if l.startswith("designated =>")), "")
    req = line.split("=>", 1)[1].strip() if line else ""
    return req if "certificate" in req or "anchor" in req else None


SWAP_SCRIPT = r'''#!/bin/bash
# Swaps in the new Blurt.app once the running one has quit, then opens it.
pid="$1"; old="$2"; new="$3"; stage="$4"
for _ in $(seq 1 600); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
backup="$stage/Blurt-previous.app"
rm -rf "$backup"
if mv "$old" "$backup" && mv "$new" "$old"; then
    rm -rf "$stage"
else
    [ -e "$old" ] || mv "$backup" "$old"
fi
xattr -dr com.apple.quarantine "$old" 2>/dev/null
open "$old"
'''


def prepare_mac(info, log, progress):
    """Downloads and checks the new Blurt.app next to the running one. Returns the command
    that swaps it in after Blurt quits (run it detached, then quit)."""
    app = mac_bundle()
    ok, why = can_install()
    if not ok:
        raise UpdateError(why)
    stage = app.parent / ".Blurt-update"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    zip_path = stage / "Blurt.zip"
    _download(info["download"], zip_path, progress)
    log("Unpacking…")
    r = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(stage)], capture_output=True,
                       text=True)
    zip_path.unlink(missing_ok=True)
    new = stage / "Blurt.app"
    if r.returncode != 0 or not new.is_dir():
        raise UpdateError(f"The download is damaged: {r.stderr.strip()}")
    log("Checking the new version's signature…")
    v = _codesign("--verify", "--deep", "--strict", str(new))
    if v.returncode != 0:
        raise UpdateError(f"The new version's signature is not valid: {v.stderr.strip()}")
    req = _requirement(app)
    if req:  # must be signed by the same certificate as this one
        v = _codesign("--verify", "--deep", "--strict", f"-R={req}", str(new))
        if v.returncode != 0:
            raise UpdateError("The new version is not signed by Blurt's certificate, so it was "
                              "not installed.")
    with open(new / "Contents" / "Info.plist", "rb") as f:
        plist = plistlib.load(f)
    if plist.get("CFBundleIdentifier") != "io.github.daggenthal.blurt" or \
            plist.get("BlurtCommit") != info["latest"]:
        raise UpdateError("The download is not the expected version of Blurt.")
    script = stage / "swap.sh"
    script.write_text(SWAP_SCRIPT)
    log("Ready: Blurt restarts as the new version.")
    return ["/bin/bash", str(script), str(os.getpid()), str(app), str(new), str(stage)]


def start_detached(cmd):
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
