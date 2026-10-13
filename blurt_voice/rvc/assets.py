"""The shared helper networks AI voices need, and downloading them. No torch needed here,
so the main Blurt window can check for them without starting the voice engine."""
import os
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
ASSETS = {
    "rmvpe.pt": [
        f"{HF}/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/predictors/rmvpe.pt",
    ],
    "contentvec/config.json": [
        f"{HF}/lengyue233/content-vec-best/resolve/main/config.json",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/embedders/contentvec/config.json",
    ],
    "contentvec/pytorch_model.bin": [
        f"{HF}/lengyue233/content-vec-best/resolve/main/pytorch_model.bin",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/embedders/contentvec/pytorch_model.bin",
    ],
}

def assets_missing(folder):
    return [name for name in ASSETS if not (Path(folder) / name).is_file()]


def download_assets(folder, progress=None):
    """Fetch the helper networks. progress(name, done_bytes, total_bytes)."""
    folder = Path(folder)
    for name in assets_missing(folder):
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for url in ASSETS[name]:
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Blurt"})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(name, done, total)
                if total and done != total:
                    raise OSError(f"download of {name} was cut short")
                os.replace(tmp, dest)
                last_error = None
                break
            except Exception as e:  # try the next mirror
                last_error = e
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if last_error is not None:
            raise RuntimeError(f"Could not download {name}: {last_error}")
