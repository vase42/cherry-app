"""Updates from GitHub Releases: check, download, install over the program.

The latest release of the public repo carries Cherry.zip (built by
tools/make_release.py). Installing it rewrites the program files only - the zip
never contains data/, refs/, characters/ or Output/, and anything under those
paths is skipped anyway - then the server exits with code 3, which tells
Start Cherry.bat to start it again.

A copy that is a git checkout (the developer's) never installs updates over
itself: it would overwrite work in progress with an older release.
"""

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile

import db

REPO = os.environ.get("CHERRY_REPO") or "vase42/cherry-app"
API = "https://api.github.com/repos/" + REPO + "/releases/latest"
RESTART_CODE = 3
KEEP = ("system/data/", "system/refs/", "system/characters/", "Output/")
_cache = {"at": 0, "data": None}
_lock = threading.Lock()


def current_version():
    try:
        with open(os.path.join(db.SYSTEM, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _vt(v):
    out = []
    for part in str(v).lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def is_dev_copy():
    return os.path.isdir(os.path.join(db.ROOT, ".git"))


def check(force=False):
    """{current, latest, newer, notes, page, dev} - cached for an hour, and a
    failed check (offline, rate limited) is reported, never raised."""
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < 3600:
            # The release is cached, the comparison is not: the installed
            # version can change under a running server (an update, a reset).
            d = dict(_cache["data"], current=current_version())
            d["newer"] = bool(d.get("asset")) and _vt(d.get("latest") or "0") > _vt(d["current"])
            return d
    cur = current_version()
    out = {"current": cur, "latest": None, "newer": False, "notes": "", "page": None,
           "dev": is_dev_copy(), "error": None}
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "cherry-updater"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read().decode("utf-8", "replace"))
        tag = rel.get("tag_name") or ""
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == "Cherry.zip"), None)
        out.update(latest=tag.lstrip("vV"), notes=(rel.get("body") or "").strip()[:2000],
                   page=rel.get("html_url"),
                   asset=asset.get("browser_download_url") if asset else None)
        out["newer"] = bool(asset) and _vt(tag) > _vt(cur)
    except urllib.error.HTTPError as e:
        out["error"] = "no release yet" if e.code == 404 else "GitHub said " + str(e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = "could not reach GitHub: " + str(getattr(e, "reason", e))
    with _lock:
        _cache.update(at=time.time(), data=out)
    return out


def install():
    """Download the latest Cherry.zip and write it over the program. Returns the
    new version. The caller restarts the server."""
    if is_dev_copy():
        raise RuntimeError("this is a development copy (it has a .git folder) - "
                           "update it with git instead")
    info = check(force=True)
    if not info.get("newer") or not info.get("asset"):
        raise RuntimeError("already up to date" if not info.get("error") else info["error"])
    req = urllib.request.Request(info["asset"], headers={"User-Agent": "cherry-updater"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = z.namelist()
    if "Cherry/system/app/server.py" not in names or "Cherry/system/VERSION" not in names:
        raise RuntimeError("that download does not look like a Cherry release")
    root = os.path.realpath(db.ROOT)
    written = 0
    for name in names:
        if not name.startswith("Cherry/") or name.endswith("/"):
            continue
        rel = name[len("Cherry/"):]
        if rel.startswith(KEEP) or ".." in rel.split("/"):
            continue
        dest = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not dest.startswith(root + os.sep):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".new"
        with open(tmp, "wb") as fh:
            fh.write(z.read(name))
        os.replace(tmp, dest)
        written += 1
    db.log("info", "updated to " + info["latest"] + " (" + str(written) + " files)")
    return info["latest"]


def restart_soon(delay=0.6):
    """Leave a marker (so the restarted server does not open a second browser
    tab) and exit with the code Start Cherry.bat loops on."""
    try:
        with open(os.path.join(db.DATA_DIR, ".restarting"), "w") as fh:
            fh.write("1")
    except OSError:
        pass
    threading.Timer(delay, lambda: os._exit(RESTART_CODE)).start()


def consume_restart_marker():
    p = os.path.join(db.DATA_DIR, ".restarting")
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
        return True
    return False
