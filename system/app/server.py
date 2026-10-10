"""HTTP server: static files, the JSON API, and local asset serving.

Stdlib only, single file, no build step. Start it with:

    python app/server.py

It binds 127.0.0.1 only - this is a local tool holding an API key, and it has
no authentication of its own, so it must never be reachable from the network.
"""

import base64
import io
import json
import mimetypes
import os
import posixpath
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db      # noqa: E402
import kie     # noqa: E402
import providers  # noqa: E402
import updater  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("CHERRY_PORT") or os.environ.get("BANANA_PORT") or "8770")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Windows reads content types from the registry, which some installs get wrong
# (.js as text/plain, no .webp at all). Pin the ones the app serves.
for _ext, _type in ((".js", "text/javascript"), (".css", "text/css"), (".webp", "image/webp"),
                    (".png", "image/png"), (".svg", "image/svg+xml"), (".mp4", "video/mp4")):
    mimetypes.add_type(_type, _ext)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SERVABLE = IMAGE_EXT | VIDEO_EXT


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------ helpers

def save_upload(session_id, filename, data_url, name=None, kind="upload"):
    """Decode a browser data: url into refs/ and register it. The file is kept
    locally as the source of truth; the kie.ai copy is a cache that expires.

    A character is saved with session_id None, which is what makes the cast
    global rather than trapped in one session."""
    if not data_url or "," not in data_url:
        raise ApiError("no file data received")
    header, b64 = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError):
        raise ApiError("that file could not be decoded") from None
    if len(raw) > 9 * 1024 * 1024:
        raise ApiError("that image is over 9MB - kie.ai will not take it")

    stem, ext = os.path.splitext(os.path.basename(filename or "reference"))
    ext = ext.lower()
    if ext not in IMAGE_EXT:
        ext = ".png" if "png" in header else ".jpg"
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem).strip("-")
    safe = safe or "reference"

    folder = db.CHAR_DIR if kind == "character" else db.REF_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe + ext)
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, safe + "-" + str(i) + ext)
        i += 1
    with open(path, "wb") as fh:
        fh.write(raw)

    label = (name or "").strip() or os.path.splitext(os.path.basename(path))[0]
    ref_id = db.create_ref(session_id, label[:80], kind, source_path=path)

    # Upload straight away when a key is set, so the reference is usable the
    # moment it appears on a card rather than at first generate.
    key = db.get_setting("kie_api_key")
    if key:
        try:
            db.set_ref_upload(ref_id, kie.upload_file(path, key))
        except kie.KieError as e:
            db.log("warn", "upload of " + os.path.basename(path) + " deferred: " + str(e))
    return ref_id


MEDIA_LIMIT = {"image": 30, "video": 300, "audio": 30}       # MB
ALLOWED_EXT = IMAGE_EXT | set(db.MEDIA_EXT)


def store_upload(session_id, filename, stream, length, name=None, kind="upload"):
    """Write a raw upload (the request body itself, no base64) to disk and
    register it. This is how videos and audio arrive - a 200MB clip as JSON
    base64 would be 270MB of text held in memory twice over."""
    stem, ext = os.path.splitext(os.path.basename(filename or "file"))
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        raise ApiError("that file type is not supported - use an image, a video "
                       "(mp4, mov, webm) or a sound (mp3, wav, m4a)")
    media = db.media_of("file" + ext)
    cap = MEDIA_LIMIT[media]
    if length > cap * 1024 * 1024:
        raise ApiError("that " + media + " is over " + str(cap) + "MB")
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem).strip("-") or media
    folder = db.CHAR_DIR if kind == "character" else db.REF_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe + ext)
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, safe + "-" + str(i) + ext)
        i += 1
    left = length
    with open(path, "wb") as fh:
        while left > 0:
            chunk = stream.read(min(1 << 20, left))
            if not chunk:
                break
            fh.write(chunk)
            left -= len(chunk)
    if left:
        os.remove(path)
        raise ApiError("the upload was cut short - try again")
    label = (name or "").strip() or os.path.splitext(os.path.basename(path))[0]
    ref_id = db.create_ref(session_id, label[:80], kind, source_path=path, media=media)
    key = db.get_setting("kie_api_key")
    if key and media == "image" and length <= 9 * 1024 * 1024:
        try:
            db.set_ref_upload(ref_id, kie.upload_file(path, key))
        except kie.KieError as e:
            db.log("warn", "upload of " + os.path.basename(path) + " deferred: " + str(e))
    return ref_id


SRC_OK = ("", "self", "none")


def check_src(v):
    """A frame source is "", "self", "none", "card:<id>" or "ref:<id>"."""
    if v is None or v in SRC_OK:
        return v or None
    for p in ("card:", "ref:"):
        if isinstance(v, str) and v.startswith(p) and v[len(p):].isdigit():
            return v
    raise ApiError("not a frame source: " + str(v))


def safe_name(text, fallback="image"):
    keep = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in str(text or ""))
    # Trim the separators too, or a title of pure punctuation becomes "---".
    return keep.strip().replace(" ", "-").strip("-_")[:60].strip("-_") or fallback


def build_zip(session_id, latest_only=False, kind="image"):
    """Every finished asset of one kind in a session, as one archive.

    Stored, not deflated: JPEG, PNG and MP4 are already compressed, so deflating
    costs CPU on a large batch and saves nothing worth having."""
    s = db.get_session(session_id)
    if not s:
        raise ApiError("no such session", 404)

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for card in db.get_cards(session_id):
            gens = [g for g in db.card_generations(card["id"], kind)
                    if g["kie_state"] == "success" and g["local_path"]
                    and os.path.isfile(g["local_path"])]
            if latest_only:
                gens = gens[:1]
            for g in gens:
                ext = os.path.splitext(g["local_path"])[1] or (
                    ".mp4" if kind == "video" else ".jpg")
                name = "%02d_%s_v%d%s" % (card["seq"], safe_name(card["title"]),
                                          g["n"], ext)
                z.write(g["local_path"], name)
                count += 1
    if not count:
        raise ApiError("there are no finished " + kind + "s in this session yet", 404)
    suffix = "-videos" if kind == "video" else ""
    return s["slug"] + suffix + ".zip", buf.getvalue()


def generate_card(card_id, key=None, kind="image"):
    """One card, one dispatch. Returns a per-card result the UI can show.

    `skipped` marks the one case that is not a failure: the card was already
    generating, so nothing was sent and nothing was charged."""
    fn = kie.dispatch if kind == "image" else kie.dispatch_video
    try:
        gen_id, ok = fn(card_id, key)
    except kie.AlreadyRunning as e:
        return {"card_id": card_id, "ok": False, "skipped": True, "error": str(e)}
    except kie.MissingKey as e:
        return {"card_id": card_id, "ok": False, "no_key": e.provider, "error": str(e)}
    except kie.KieError as e:
        return {"card_id": card_id, "ok": False, "error": str(e)}
    if not ok:
        gen = db.get_generation(gen_id) or {}
        return {"card_id": card_id, "ok": False,
                "error": gen.get("fail_msg") or "dispatch failed"}
    return {"card_id": card_id, "ok": True, "generation_id": gen_id}


# ------------------------------------------------------------ routes

def route_get(path, query):
    if path == "/api/update":
        # Is there a newer release on GitHub? Cached for an hour; never fails.
        return updater.check(force=query.get("force", [""])[0] == "1")

    if path == "/api/state":
        s = db.all_settings()
        return {
            "version": updater.current_version(),
            "sessions": db.list_sessions(),
            "settings": {
                # Kept for the older client paths; the real keys state is in
                # `providers`, one entry per service.
                "has_key": bool(providers.get_key("kie")),
                "key_hint": mask(providers.get_key("kie")),
                "image_provider": s.get("image_provider") or "kie",
                "image_model": s.get("image_model") or db.DEFAULTS["image_model"],
                "video_provider": s.get("video_provider") or "kie",
                "video_model": s.get("video_model") or db.DEFAULTS["video_model"],
                "aspect_ratio": s.get("aspect_ratio") or db.DEFAULTS["aspect_ratio"],
                "resolution": s.get("resolution") or db.DEFAULTS["resolution"],
                "output_format": s.get("output_format") or db.DEFAULTS["output_format"],
            },
            "providers": providers.public_catalog(),
            "options": {
                "aspects": db.ASPECTS,
                "resolutions": db.RESOLUTIONS,
                "formats": db.FORMATS,
                "video_resolutions": db.VIDEO_RESOLUTIONS,
                "durations": db.VIDEO_DURATIONS,
            },
        }

    if path.startswith("/api/session/"):
        rest = path[len("/api/session/"):]
        parts = rest.split("/")
        sid = to_int(parts[0])
        if len(parts) == 1:
            view = db.session_view(sid)
            if not view:
                raise ApiError("no such session", 404)
            return view
        if parts[1] == "export":
            return {"payload": db.export_session(sid)}

    if path == "/api/log":
        return {"entries": db.recent_log(int(query.get("limit", ["80"])[0]))}

    raise ApiError("unknown endpoint: " + path, 404)


def route_post(path, body):
    # -------------------------------------------------- settings
    if path == "/api/settings":
        # {keys: {provider: "key"}} saves any number at once; a blank value
        # leaves that key alone, and {clear: [provider]} removes one.
        keys = dict(body.get("keys") or {})
        if "kie_api_key" in body:                     # the original, single-key form
            keys["kie"] = body.get("kie_api_key")
        for pid, value in keys.items():
            if pid in providers.CATALOG and (value or "").strip():
                db.set_setting(providers.key_setting(pid), value.strip())
        for pid in body.get("clear") or []:
            if pid in providers.CATALOG:
                db.set_setting(providers.key_setting(pid), "")
        for k in ("image_model", "aspect_ratio", "resolution", "output_format"):
            if body.get(k):
                db.set_setting(k, body[k])
        return {"ok": True, "providers": providers.public_catalog(),
                "settings": {"has_key": bool(providers.get_key("kie")),
                             "key_hint": mask(providers.get_key("kie"))}}

    # -------------------------------------------------- updates
    if path == "/api/update/apply":
        try:
            new = updater.install()
        except Exception as e:                      # noqa: BLE001 - shown to the user as is
            raise ApiError(str(e)) from None
        updater.restart_soon()
        return {"ok": True, "version": new}

    # -------------------------------------------------- cast
    if path == "/api/characters":
        ref_id = save_upload(None, body.get("filename"), body.get("data"),
                             name=body.get("name"), kind="character")
        sid = body.get("session_id")
        return {"ok": True, "ref_id": ref_id,
                "characters": db.list_characters(),
                "refs": db.list_refs(to_int(sid)) if sid else []}

    # -------------------------------------------------- sessions
    if path == "/api/sessions":
        engines = {k: body.get(k) for k in ("image_provider", "image_model",
                                            "video_provider", "video_model")}
        for kind in ("image", "video"):
            p = engines.get(kind + "_provider")
            if p and p not in providers.CATALOG:
                raise ApiError("unknown service: " + str(p))
            if p and not engines.get(kind + "_model"):
                engines[kind + "_model"] = providers.default_model(p, kind)
        sid = db.create_session(body.get("name"), engines)
        return {"ok": True, "session_id": sid}

    if path.startswith("/api/session/"):
        parts = path[len("/api/session/"):].split("/")
        sid = to_int(parts[0])
        action = parts[1] if len(parts) > 1 else ""

        if action == "rename":
            db.rename_session(sid, body.get("name"))
            return {"ok": True}
        if action == "engine":
            kind = "video" if body.get("kind") == "video" else "image"
            p = body.get("provider")
            if p not in providers.CATALOG:
                raise ApiError("unknown service: " + str(p))
            if kind not in providers.CATALOG[p]["kinds"]:
                raise ApiError(providers.label(p) + " does not make "
                               + ("clips" if kind == "video" else "stills"))
            m = (body.get("model") or "").strip() or providers.default_model(p, kind)
            db.set_session_engine(sid, kind, p, m)
            return {"ok": True}
        if action == "delete":
            db.delete_session(sid)
            return {"ok": True}
        if action == "clips":
            # A standalone clip - no image prompt behind it. `after` puts it
            # right behind that card in the running order.
            after = body.get("after")
            card_id = db.create_card(sid, kind="clip", after=to_int(after) if after else None,
                                     video_prompt=body.get("video_prompt") or "")
            return {"ok": True, "card_id": card_id}
        if action == "cards":
            card_id = db.create_card(sid, prompt=body.get("prompt", ""),
                                     title=body.get("title"))
            return {"ok": True, "card_id": card_id}
        if action == "import":
            # "auto" reads whichever it is. JSON wins when the paste actually
            # contains prompts; otherwise it is treated as blank-line-separated
            # prose, which is the other way people paste these.
            text = body.get("text", "")
            mode = body.get("mode") or "auto"
            as_json = mode == "json" or (mode == "auto" and db.looks_like_json(text))
            created, skipped, warnings = (db.import_json(sid, text) if as_json
                                          else db.import_text(sid, text))
            if not created:
                raise ApiError(warnings[0] if warnings else "nothing to import")
            return {"ok": True, "created": created, "skipped": skipped,
                    "warnings": warnings, "read_as": "json" if as_json else "text"}
        if action == "apply_all":
            n = db.apply_to_all(sid, **{k: body.get(k) for k in
                                        ("model", "aspect_ratio", "resolution",
                                         "output_format")})
            return {"ok": True, "updated": n}
        if action == "refs":
            ref_id = save_upload(sid, body.get("filename"), body.get("data"))
            return {"ok": True, "ref_id": ref_id, "refs": db.list_refs(sid)}
        if action == "ref_url":
            url = (body.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise ApiError("that does not look like an image url")
            name = url.rsplit("/", 1)[-1].split("?")[0][:80] or "reference"
            media = body.get("media") if body.get("media") in ("image", "video", "audio") else None
            ref_id = db.create_ref(sid, name, "url", upload_url=url, media=media)
            return {"ok": True, "ref_id": ref_id, "refs": db.list_refs(sid)}
        if action in ("generate_all", "generate_all_videos"):
            video = action == "generate_all_videos"
            only_new = bool(body.get("only_new"))
            results, view = [], db.session_view(sid)
            for card in view["cards"]:
                text = card["video_prompt"] if video else card["prompt"]
                if not (text or "").strip():
                    continue
                if card["video_busy"] if video else card["busy"]:
                    continue
                if video and not card["can_clip"]:
                    continue                       # no image to animate yet
                # "Only what changed" skips cards whose newest take already came
                # from exactly this prompt - the point of Generate all is to
                # catch up, not to re-bill every card.
                if only_new:
                    latest = card["video_latest"] if video else card["latest"]
                    dirty = card["video_dirty"] if video else card["dirty"]
                    if latest and not dirty:
                        continue
                # Every dispatch still re-checks under a lock inside
                # db.begin_generation, so a second Generate all arriving mid-run
                # cannot double up on a card this one already claimed.
                results.append(generate_card(card["id"], None,
                                             "video" if video else "image"))
            missing = [r for r in results if r.get("no_key")]
            if missing and not any(r["ok"] for r in results):
                raise ApiError(missing[0]["error"], 428)
            return {"ok": True, "results": results,
                    "queued": sum(1 for r in results if r["ok"]),
                    "skipped": sum(1 for r in results if r.get("skipped"))}

        if action == "import_videos":
            text = body.get("text", "")
            mode = body.get("mode") or "auto"
            as_json = mode == "json" or (mode == "auto" and db.looks_like_json(text))
            filled, unmatched, warnings = (db.import_video_json(sid, text) if as_json
                                           else db.import_video_text(sid, text))
            if not filled:
                raise ApiError(warnings[0] if warnings else "nothing to import")
            return {"ok": True, "filled": filled, "unmatched": unmatched,
                    "warnings": warnings, "read_as": "json" if as_json else "text"}

        if action == "video_defaults":
            fields = {k: body.get(k) for k in
                      ("video_duration", "video_resolution", "video_aspect", "video_model")
                      if body.get(k)}
            if not fields:
                return {"ok": True, "updated": 0}
            sets = ", ".join(k + "=?" for k in fields)
            vals = list(fields.values()) + [db.now(), sid]
            with db._write_lock:
                c = db.conn()
                cur = c.execute("UPDATE cards SET " + sets + ", updated_at=? "
                                "WHERE session_id=?", vals)
                c.commit()
            return {"ok": True, "updated": cur.rowcount}

    # -------------------------------------------------- cards
    if path.startswith("/api/card/"):
        parts = path[len("/api/card/"):].split("/")
        cid = to_int(parts[0])
        action = parts[1] if len(parts) > 1 else ""

        if not db.get_card(cid):
            raise ApiError("no such card", 404)

        if action == "":
            fields = {k: body[k] for k in db.CARD_FIELDS if k in body}
            for k in ("first_src", "last_src"):
                if k in fields:
                    fields[k] = check_src(fields[k])
            if fields.get("video_mode") not in (None, "", "frames", "refs", "text"):
                raise ApiError("not a clip mode: " + str(fields["video_mode"]))
            if "video_mode" in fields:
                fields["video_mode"] = fields["video_mode"] or None
            db.update_card(cid, **fields)
            return {"ok": True, "fingerprint": db.card_fingerprint(cid)}
        if action == "delete":
            db.delete_card(cid)
            return {"ok": True}
        if action == "duplicate":
            return {"ok": True, "card_id": db.duplicate_card(cid)}
        if action == "move":
            db.move_card(cid, body.get("direction", "up"),
                         "shots" if body.get("scope") == "shots" else "all")
            return {"ok": True}
        if action == "refs":
            db.set_card_refs(cid, [to_int(r) for r in (body.get("ref_ids") or [])])
            return {"ok": True, "refs": db.card_refs(cid)}
        if action == "base_image":
            rid = body.get("ref_id")
            db.set_base_image(cid, to_int(rid) if rid else None)
            return {"ok": True}
        if action == "video_refs":
            db.set_card_video_refs(cid, [to_int(r) for r in (body.get("ref_ids") or [])])
            return {"ok": True, "refs": db.card_video_refs(cid)}
        if action in ("generate", "generate_video"):
            result = generate_card(cid, None,
                                   "video" if action == "generate_video" else "image")
            if not result["ok"]:
                if result.get("no_key"):
                    raise ApiError(result["error"], 428)
                # 409 for "already running" so the UI can say so quietly rather
                # than shouting about a failure that cost nothing.
                raise ApiError(result["error"], 409 if result.get("skipped") else 400)
            return result

    # -------------------------------------------------- references
    if path.startswith("/api/ref/"):
        parts = path[len("/api/ref/"):].split("/")
        rid = to_int(parts[0])
        if len(parts) > 1 and parts[1] == "delete":
            db.delete_ref(rid)
            return {"ok": True}

    # -------------------------------------------------- generations
    if path.startswith("/api/generation/"):
        parts = path[len("/api/generation/"):].split("/")
        gid = to_int(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        gen = db.get_generation(gid)
        if not gen:
            raise ApiError("no such generation", 404)

        if action == "delete":
            # Only ever a local row. Nothing is deleted at kie.ai, and the file
            # we downloaded stays on disk.
            card = db.get_card(gen["card_id"])
            db.delete_generation(gid)
            if card and card.get("active_image_id") == gid:
                db.set_active_image(card["id"], None)   # fall back to the newest
            return {"ok": True}
        if action == "activate":
            if gen["kind"] != "image":
                raise ApiError("only an image can be the one a clip is made from")
            if gen["kie_state"] != "success":
                raise ApiError("that take is not finished")
            db.set_active_image(gen["card_id"], gid)
            return {"ok": True}
        if action == "use_as_ref":
            # Chaining: a finished image becomes a reference for other cards.
            # It points at the downloaded file, so it survives kie.ai expiring
            # the original result url and is re-uploaded on demand.
            if gen["kie_state"] != "success" or not gen["local_path"]:
                raise ApiError("that image is not finished yet")
            card = db.get_card(gen["card_id"])
            name = "shot " + str(card["seq"]) + " v" + str(gen["n"])
            ref_id = db.create_ref(card["session_id"], name, "generation",
                                   source_path=gen["local_path"])
            return {"ok": True, "ref_id": ref_id,
                    "refs": db.list_refs(card["session_id"])}

    raise ApiError("unknown endpoint: " + path, 404)


def mask(key):
    if not key:
        return ""
    key = str(key)
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "set"


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError("bad id: " + str(v)) from None


# ------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "Cherry"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                                   # the app has its own log

    # ---- plumbing

    def send_json(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_bytes(self, raw, ctype, status=200, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def fail(self, message, status=400):
        self.send_json({"ok": False, "error": str(message)}, status)

    # ---- GET

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self.send_json(route_get(path, query))
            if path == "/file":
                return self.serve_local(query.get("p", [""])[0])
            if path == "/download":
                name, raw = build_zip(to_int(query.get("s", [""])[0]),
                                      query.get("latest", ["0"])[0] == "1",
                                      query.get("kind", ["image"])[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="' + name + '"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            return self.serve_static(path)
        except ApiError as e:
            return self.fail(e, e.status)
        except Exception as e:                          # noqa: BLE001
            db.log("error", "GET " + path + ": " + type(e).__name__ + ": " + str(e))
            return self.fail(type(e).__name__ + ": " + str(e), 500)

    def serve_static(self, path):
        if path in ("/", "", "/index.html"):
            rel = "index.html"
        else:
            rel = posixpath.normpath(path).lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self.fail("not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            return self.send_bytes(fh.read(), ctype)

    def serve_local(self, rel):
        """Serve a generated image or a local reference.

        The browser only ever holds paths relative to the project root, and this
        re-anchors them there and refuses anything that escapes - so a crafted
        ?p= cannot read the rest of the disk."""
        if not rel:
            return self.fail("not found", 404)
        rel = urllib.parse.unquote(rel)
        full = os.path.normpath(os.path.join(db.ROOT, rel))
        root = os.path.normpath(db.ROOT)
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self.fail("not found", 404)
        if os.path.splitext(full)[1].lower() not in SERVABLE:
            return self.fail("not a servable asset", 403)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            return self.send_bytes(fh.read(), ctype, cache=True)

    # ---- POST

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if path == "/api/upload":
                # Raw body upload: ?session=<id>&filename=<name>&name=<label>&kind=upload|character|frame
                q = urllib.parse.parse_qs(parsed.query)
                one = lambda k: (q.get(k) or [""])[0]   # noqa: E731
                kind = one("kind") if one("kind") in ("upload", "character", "frame") else "upload"
                sid = to_int(one("session")) if one("session") and kind != "character" else None
                ref_id = store_upload(sid, one("filename"), self.rfile, length,
                                      name=one("name"), kind=kind)
                return self.send_json({"ok": True, "ref_id": ref_id,
                                       "refs": db.list_refs(sid) if sid else [],
                                       "characters": db.list_characters()})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise ApiError("request body was not valid JSON") from None
            if not isinstance(body, dict):
                raise ApiError("request body must be an object")
            return self.send_json(route_post(path, body))
        except ApiError as e:
            return self.fail(e, e.status)
        except kie.KieError as e:
            return self.fail(e, 502)
        except Exception as e:                          # noqa: BLE001
            db.log("error", "POST " + path + ": " + type(e).__name__ + ": " + str(e))
            return self.fail(type(e).__name__ + ": " + str(e), 500)


URL = "http://" + HOST + ":" + str(PORT) + "/"


def already_running():
    """True if something is already listening on our port.

    Double-clicking the launcher twice should not fail with a stack trace; the
    second one just points you at the window that is already there."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


def banner(lines):
    width = max(len(x) for x in lines) + 4
    print("\n  " + "┌" + "─" * width + "┐")
    for x in lines:
        print("  │  " + x.ljust(width - 4) + "  │")
    print("  " + "└" + "─" * width + "┘\n")


def main():
    if already_running():
        banner(["Cherry is already running.", "", "   " + URL])
        webbrowser.open(URL)
        return

    db.init()
    providers.migrate_retired()
    restarted = updater.consume_restart_marker()
    db.log("info", "cherry starting on port " + str(PORT))
    kie.poller.start()

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        banner(["Could not start on port " + str(PORT) + ".", str(e)])
        input("Press Enter to close. ")
        return

    banner([
        "Cherry is running.",
        "",
        "   " + URL,
        "",
        "Cherry " + updater.current_version() + "  -  your work is in the Output folder.",
        "Close this window to stop the app.",
    ])
    if not restarted and "1" not in (os.environ.get("CHERRY_NO_BROWSER"),
                                     os.environ.get("BANANA_NO_BROWSER")):
        threading.Timer(0.8, lambda: webbrowser.open(URL)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("  stopping…")
    finally:
        kie.poller.stop_flag.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
