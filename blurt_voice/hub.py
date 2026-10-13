"""Find and download RVC voice models from Hugging Face (public API, no account needed)."""
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HF = "https://huggingface.co"
UA = {"User-Agent": "Blurt"}
MB = 1024 * 1024
SKIP_NAMES = re.compile(r"^(G|D)_\d+|^f0[GD]\d|^[GD]\d+k|rmvpe|hubert|contentvec|pretrain",
                        re.IGNORECASE)
GENERIC_STEMS = {"model", "voice", "weights", "final", "g", "rvc", "added", "out"}


def _open(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def _json(url):
    with _open(url) as r:
        nxt = None
        link = r.headers.get("Link") or ""
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if m:
            nxt = m.group(1)
        return json.loads(r.read().decode("utf-8")), nxt


def parse_link(text):
    """'https://huggingface.co/owner/name/tree/main/sub' -> ('owner/name', 'sub') or None."""
    m = re.match(r"\s*(?:https?://)?(?:www\.)?huggingface\.co/(?!spaces/|datasets/)"
                 r"([\w.-]+/[\w.-]+)(?:/(?:tree|blob|resolve)/[^/]+/?(.*))?\s*$", text)
    if not m:
        return None
    return m.group(1), urllib.parse.unquote(m.group(2) or "").strip("/")


def search_repos(query, limit=40):
    url = (f"{HF}/api/models?search={urllib.parse.quote(query)}&limit={limit}"
           "&sort=downloads&direction=-1")
    data, _ = _json(url)
    return [{"repo": d["id"], "downloads": d.get("downloads", 0), "likes": d.get("likes", 0)}
            for d in data if not d.get("private") and not d.get("gated")]


def list_files(repo, max_pages=20):
    url = f"{HF}/api/models/{repo}/tree/main?recursive=true"
    files = []
    for _ in range(max_pages):
        data, url = _json(url)
        files += [f for f in data if f.get("type") == "file"]
        if not url:
            break
    return files


def _tokens(text):
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


def find_models(repo, files, query=""):
    """Turn a repo's file list into downloadable voices: .pth (+ best .index), or .zip packs."""
    by_folder = {}
    for f in files:
        path = f["path"]
        by_folder.setdefault(str(Path(path).parent), []).append(f)
    terms = _tokens(query)
    out = []
    for folder, items in by_folder.items():
        pths = [f for f in items if f["path"].lower().endswith(".pth")
                and 2 * MB <= f.get("size", 0) <= 400 * MB
                and not SKIP_NAMES.search(Path(f["path"]).name)]
        indexes = [f for f in items if f["path"].lower().endswith(".index")
                   and f.get("size", 0) <= 2048 * MB]
        for p in pths:
            stem = Path(p["path"]).stem
            index = None
            if indexes:
                stem_t = _tokens(stem)
                index = max(indexes, key=lambda i: (
                    len(stem_t & _tokens(Path(i["path"]).stem)),
                    Path(i["path"]).name.startswith("added"), -i.get("size", 0)))
                if len(indexes) > 1 and not stem_t & _tokens(Path(index["path"]).stem):
                    added = [i for i in indexes if Path(i["path"]).name.startswith("added")]
                    # one voice in the folder: its "added_" index is the right one
                    index = added[0] if len(pths) == 1 and len(added) == 1 else None
            name = stem
            if stem.lower() in GENERIC_STEMS and folder not in ("", "."):
                name = Path(folder).name
            out.append({"repo": repo, "kind": "pth", "name": re.sub(r"[_]+", " ", name).strip(),
                        "path": p["path"], "size": p.get("size", 0),
                        "index": index["path"] if index else None,
                        "index_size": index.get("size", 0) if index else 0})
        if not pths:
            for z in items:
                if z["path"].lower().endswith(".zip") and MB <= z.get("size", 0) <= 1536 * MB \
                        and not re.search(r"dataset|drive-download|backup", z["path"], re.I):
                    out.append({"repo": repo, "kind": "zip", "path": z["path"],
                                "name": re.sub(r"[_]+", " ", Path(z["path"]).stem).strip(),
                                "size": z.get("size", 0), "index": None, "index_size": 0})
    if terms:
        for e in out:
            e["match"] = len(terms & (_tokens(e["path"]) | _tokens(repo)))
    return out


def search(query, progress=None):
    """Search by name, or browse one repo when given a Hugging Face link."""
    link = parse_link(query)
    if link:
        repo, sub = link
        files = list_files(repo)
        if sub and Path(sub).suffix and not sub.lower().endswith((".pth", ".zip", ".index")):
            sub = str(Path(sub).parent).strip(".")  # a link to e.g. README.md: use its folder
        if sub.lower().endswith((".pth", ".zip", ".index")):
            sub = str(Path(sub).parent).strip(".")
        if sub:
            files = [f for f in files if f["path"].startswith(sub.rstrip("/") + "/")]
        return find_models(repo, files)
    repos = {r["repo"]: r for r in search_repos(query)}
    # popular RVC collections often hold the voice under a generic repo name
    for r in search_repos("rvc", limit=25):
        repos.setdefault(r["repo"], dict(r, collection=True))
    results = []

    def scan(info):
        try:
            found = find_models(info["repo"], list_files(info["repo"]), query)
        except Exception:
            return []
        if info.get("collection"):
            found = [e for e in found if e.get("match")]
        for e in found:
            e["downloads"], e["likes"] = info["downloads"], info["likes"]
        return found

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, found in enumerate(pool.map(scan, repos.values())):
            results += found
            if progress:
                progress(f"Looked through {i + 1} of {len(repos)} repositories…")
    results.sort(key=lambda e: (-e.get("match", 0), e["kind"] != "pth", e["index"] is None,
                                -e.get("downloads", 0)))
    return results


def file_url(repo, path):
    return f"{HF}/{repo}/resolve/main/{urllib.parse.quote(path)}"


def download(repo, path, dest, progress=None, label=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with _open(file_url(repo, path), timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                pct = f" {done * 100 // total}%" if total else ""
                progress(f"Downloading {label or Path(path).name}{pct} "
                         f"({done // MB} of {total // MB} MB)")
    if total and done != total:
        tmp.unlink()
        raise OSError(f"The download of {Path(path).name} was cut short.")
    tmp.replace(dest)
    return dest


def guess_gender(repo, files, folder):
    """Some repos ship a small metadata.json that says whether the voice is male or female."""
    for f in files:
        p = f["path"]
        if Path(p).name.lower() == "metadata.json" and str(Path(p).parent) in (folder, ".", "") \
                and f.get("size", 0) < 64 * 1024:
            try:
                with _open(file_url(repo, p)) as r:
                    meta = json.loads(r.read().decode("utf-8"))
                g = str(meta.get("gender", "")).lower()
                if g in ("male", "female"):
                    return g
            except Exception:
                return None
    return None
