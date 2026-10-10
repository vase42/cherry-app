"""kie.ai client: payload building, dispatch, uploads and the background poller.

API surface (docs.kie.ai):
    POST /api/v1/jobs/createTask -> {"code":200,"data":{"taskId":"..."}}
    GET  /api/v1/jobs/recordInfo -> data.state in waiting|queuing|generating|success|fail
                                    data.resultJson is a JSON *string*:
                                    {"resultUrls":[...]}

The account limit is 20 new tasks per 10s with no server-side queue - anything
over the line is rejected outright with a 429 - so every dispatch passes through
the token bucket below. Results are polled rather than webhooked, because a
local app has no public callback url, and each asset is downloaded the moment it
succeeds because kie.ai's result urls expire.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db
import providers

BASE = "https://api.kie.ai"
CREATE = BASE + "/api/v1/jobs/createTask"
RECORD = BASE + "/api/v1/jobs/recordInfo"

# Uploads live on a different host - api.kie.ai 404s on this path. That host
# also sits behind Cloudflare, which rejects urllib's default agent with a 1010
# "browser signature banned", so every request sends a real UA.
UPLOAD_BASE = "https://kieai.redpandaai.co"
UPLOAD_B64 = UPLOAD_BASE + "/api/file-base64-upload"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

POLL_SECONDS = 5
RATE_MAX = 18          # 20/10s documented; leave headroom
RATE_WINDOW = 10.0

MAX_REFS = 8           # nano-banana accepts up to eight reference images


class RateLimiter:
    def __init__(self, max_calls, window):
        self.max_calls, self.window = max_calls, window
        self.calls, self.lock = [], threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                t = time.monotonic()
                self.calls = [c for c in self.calls if t - c < self.window]
                if len(self.calls) < self.max_calls:
                    self.calls.append(t)
                    return
                wait = self.window - (t - self.calls[0]) + 0.05
            time.sleep(max(wait, 0.05))


limiter = RateLimiter(RATE_MAX, RATE_WINDOW)


# One error type for every provider, so the routes and the poller catch a single
# thing. The name is kept because the routes, the selftest and a lot of history
# already say KieError.
KieError = providers.ProviderError


class OutOfCredits(KieError):
    """Distinct because nothing is wrong with the request - it just cannot run
    until the account is topped up, and that needs saying plainly rather than
    hiding behind a generic dispatch failure."""


def _request(method, url, api_key, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + (api_key or ""))
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code == 401:
            raise KieError("kie.ai rejected the API key (401) - check it in Services") from None
        if e.code == 429:
            raise KieError("rate limited by kie.ai (429) - backing off") from None
        raise KieError("HTTP " + str(e.code) + ": " + detail) from None
    except urllib.error.URLError as e:
        raise KieError("network error: " + str(e.reason)) from None
    except json.JSONDecodeError:
        raise KieError("kie.ai returned something that is not JSON") from None

    code = payload.get("code")
    if code == 402:
        raise OutOfCredits("kie.ai balance is empty - top up at kie.ai to keep generating")
    if code == 401:
        raise KieError("kie.ai rejected the API key - check it in Services")
    if code not in (200, None):
        raise KieError("kie.ai code " + str(code) + ": " + str(payload.get("msg")))
    return payload.get("data") or {}


# ------------------------------------------------------------ payload

def build_input(card, ref_urls=None):
    """The nano-banana-2 image contract. Every field the card carries is sent
    explicitly rather than left to the model default, so what you see on the
    card is exactly what was asked for."""
    d = db.defaults()
    inp = {
        "prompt": card.get("prompt") or "",
        "aspect_ratio": card.get("aspect_ratio") or d["aspect_ratio"],
        "resolution": card.get("resolution") or d["resolution"],
        "output_format": card.get("output_format") or d["output_format"],
    }
    refs = [u for u in (ref_urls or []) if u]
    # image_input is always present: the API treats a missing key and an empty
    # list the same way, and sending it keeps our payload identical to the
    # docs' shape, which is what the user pastes in.
    inp["image_input"] = refs[:MAX_REFS]
    return inp


# ------------------------------------------------------------ dispatch

def create_task(api_key, model, inp):
    limiter.acquire()
    data = _request("POST", CREATE, api_key, {"model": model, "input": inp})
    task_id = data.get("taskId") or data.get("task_id")
    if not task_id:
        raise KieError("no taskId in response: " + json.dumps(data)[:200])
    return task_id


def fetch_task(api_key, task_id):
    q = urllib.parse.urlencode({"taskId": task_id})
    return _request("GET", RECORD + "?" + q, api_key, timeout=30)


def parse_result_urls(data):
    raw = data.get("resultJson")
    if not raw:
        return []
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    # Most models: {"resultUrls": [...]}. Veo 3.1 at 1080p/4k nests them under
    # data.result_urls, with the 720p original in data.origin_urls.
    inner = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    urls = (obj.get("resultUrls") or obj.get("resultUrl") or inner.get("result_urls")
            or inner.get("resultUrls") or inner.get("origin_urls") or [])
    if isinstance(urls, str):
        urls = [urls]
    return [u for u in urls if isinstance(u, str)]


def card_ref_urls(card_id, api_key=None):
    """Live urls for a card's references, re-uploading any that kie.ai has
    aged out. A url-kind reference is passed through untouched - the user owns
    its lifetime."""
    urls = []
    for ref in db.card_refs(card_id):
        url = ref_url(ref, api_key)
        if url:
            urls.append(url)
    return urls


def ref_url(ref, api_key=None):
    if ref["kind"] == "url" or not ref["source_path"]:
        return ref["upload_url"] or None
    return ensure_upload(ref, api_key)


def ensure_upload(ref, api_key=None):
    """Return a live kie.ai url for a locally-stored reference."""
    if ref["upload_url"] and not db.upload_is_stale(ref):
        return ref["upload_url"]
    path = ref.get("source_path")
    if not path or not os.path.isfile(path):
        return ref["upload_url"]              # stale, but better than nothing
    key = api_key or db.get_setting("kie_api_key")
    if not key:
        return ref["upload_url"]
    try:
        big = os.path.getsize(path) > 9 * 1024 * 1024
        url = upload_stream(path, key) if big else upload_file(path, key)
    except KieError as e:
        db.log("warn", "re-upload of " + str(ref["name"]) + " failed: " + str(e))
        return ref["upload_url"]
    db.set_ref_upload(ref["id"], url)
    return url


class AlreadyRunning(KieError):
    """The card already has a generation of this kind in flight. Raised instead
    of dispatching a second one, because the second one costs money."""


class MissingKey(KieError):
    """No API key for the provider this card is set to. Raised before anything
    is claimed, so nothing is left pending."""

    def __init__(self, provider):
        super().__init__("No " + providers.label(provider) + " API key set. "
                         "Open Services and paste it in.")
        self.provider = provider


def _engine_for(card, kind):
    """Resolve the card's provider, model and key, or refuse before claiming."""
    provider, model = db.effective_engine(card, kind)
    meta = providers.CATALOG.get(provider)
    if not meta:
        raise KieError("this shot is set to an unknown service: " + str(provider))
    if kind not in meta["kinds"]:
        raise KieError(meta["label"] + " can't make " + ("clips" if kind == "video"
                       else "stills") + " - pick another engine for this shot")
    if not model:
        model = providers.default_model(provider, kind)
    key = providers.get_key(provider)
    if not key:
        raise MissingKey(provider)
    return provider, model, key


def dispatch(card_id, api_key=None):
    """Fire one card's image. Returns (generation_id, ok).

    The pending row is claimed through db.begin_generation BEFORE the network
    call, and that claim is what makes a double click safe: the second attempt
    finds the card busy and raises rather than spending anything.

    A dispatch failure is not an exception - it is recorded on the generation
    row so the card can show why."""
    card = db.get_card(card_id)
    if not card:
        raise KieError("no such card")
    if not (card["prompt"] or "").strip():
        raise KieError("this card has no prompt yet")
    provider, model, key = _engine_for(card, "image")

    refs = db.card_refs(card_id)
    fp = db.fingerprint(card, db.ref_identity(refs))

    # Claim first. Uploading references can take seconds, and a second click
    # arriving in that window must find the card already taken.
    gen_id = db.begin_generation(card_id, "image", model, card["prompt"] or "", {}, fp,
                                 provider=provider)
    if gen_id is None:
        raise AlreadyRunning("that image is already generating")

    ad = providers.adapter(provider)
    try:
        urls = [u for u in (ad.ref(r, key) for r in refs) if u]
        built = ad.build("image", providers.model_spec(provider, "image", model), card, urls)
    except Exception as e:                       # noqa: BLE001 - never strand the row
        db.update_generation(gen_id, "fail", fail_msg="could not prepare: " + str(e))
        return gen_id, False
    return _send(gen_id, ad, "image", built["api"], built["body"], key, card, "card ")


def _send(gen_id, ad, kind, model, payload, key, card, noun):
    """Hand a prepared request to its provider. Sync providers (the answer comes
    back in the response) finish on a thread so the click returns at once."""
    db.set_generation_payload(gen_id, providers.audit(payload))
    if ad.sync:
        threading.Thread(target=_run_sync, name="sync-" + str(gen_id),
                         args=(gen_id, ad, kind, model, payload, key, card["seq"]),
                         daemon=True).start()
        return gen_id, True
    try:
        sub = ad.submit(kind, model, payload, key)
    except KieError as e:
        db.update_generation(gen_id, "fail", fail_msg=str(e))
        db.log("error", noun + str(card["seq"]) + " dispatch failed: " + str(e))
        return gen_id, False
    db.attach_task(gen_id, sub["task"], sub.get("meta"))
    db.log("info", noun + str(card["seq"]) + " queued (" + str(sub["task"]) + ")")
    return gen_id, True


def _run_sync(gen_id, ad, kind, model, payload, key, seq):
    try:
        out = ad.submit(kind, model, payload, key)
        job = db.generation_job(gen_id)
        if not job:
            return                              # the card was deleted meanwhile
        if out.get("bytes"):
            path = save_bytes(out["bytes"], job["slug"], job["seq"], job["n"],
                              out.get("ext") or ".jpg", kind)
            db.update_generation(gen_id, "success", local_path=path)
        elif out.get("url"):
            path = download(out["url"], job["slug"], job["seq"], job["n"], "jpg", kind)
            db.update_generation(gen_id, "success", result_url=out["url"], local_path=path)
        else:
            raise KieError("the service returned nothing")
        db.log("info", "card " + str(seq) + " " + kind + " ready (" + ad.name + ")")
    except Exception as e:                      # noqa: BLE001 - a thread must not die silently
        db.update_generation(gen_id, "fail", fail_msg=str(e))
        db.log("error", "card " + str(seq) + " failed: " + str(e))


# ------------------------------------------------------------ video

def build_video_input(card, image_urls):
    """The image-to-video contract.

    Note the vocabularies: a clip is 480p/720p/1080p and a duration in whole
    seconds, never the still's 1K/2K/4K. Sending a still's resolution here is
    silently wrong."""
    vd = db.video_defaults()
    return {
        "prompt": card.get("video_prompt") or "",
        "image_urls": [u for u in (image_urls or []) if u][:MAX_REFS],
        "duration": str(card.get("video_duration") or vd["duration"]),
        "resolution": card.get("video_resolution") or vd["resolution"],
        "aspect_ratio": card.get("video_aspect") or vd["aspect_ratio"],
    }


def ensure_image_upload(gen, api_key):
    """A live kie.ai url for one of our own finished images.

    The result url kie.ai handed back expires, so the clip stage re-uploads the
    file we downloaded. The upload expires too, hence the same staleness check
    the references use."""
    if gen.get("upload_url") and not db.upload_is_stale(gen):
        return gen["upload_url"]
    path = gen.get("local_path")
    if not path or not os.path.isfile(path):
        raise KieError("the image file for that take is missing from disk")
    url = upload_file(path, api_key)
    db.set_generation_upload(gen["id"], url)
    return url


def dispatch_video(card_id, api_key=None):
    """Fire one card's clip in whatever mode it runs in - first/last frames,
    references, or text alone. Same claim-then-spend order as the image path:
    every refusal that can be known up front happens before the claim, so it
    costs nothing and leaves nothing pending."""
    card = db.get_card(card_id)
    if not card:
        raise KieError("no such card")
    if not (card["video_prompt"] or "").strip():
        raise KieError("this clip has no prompt yet")
    provider, model, key = _engine_for(card, "video")
    session = db.get_session(card["session_id"])
    cs = db.clip_state(card, session)
    if cs["problem"]:
        raise KieError(cs["problem"])
    mode = cs["mode"]
    fp = db.clip_fingerprint(card, cs)
    first_gen = ((cs["first"] or {}).get("gen")
                 if mode == "frames" or cs.get("first_as_ref") else None)

    gen_id = db.begin_generation(card_id, "video", model, card["video_prompt"] or "",
                                 {}, fp, source_gen_id=first_gen["id"] if first_gen else None,
                                 provider=provider)
    if gen_id is None:
        raise AlreadyRunning("that clip is already generating")

    ad = providers.adapter(provider)

    def link(frame):
        if not frame:
            return None
        if frame.get("gen"):
            return ad.image_ref(frame["gen"], key)
        return ad.ref(frame["ref"], key)

    try:
        inputs = {"first": None, "last": None, "images": [], "videos": [], "audio": []}
        if mode == "frames":
            inputs["first"] = link(cs["first"])
            if not inputs["first"]:
                raise KieError("the first frame could not be uploaded")
            if cs["last"] and (cs["variant"] or {}).get("last"):
                inputs["last"] = link(cs["last"])
        elif mode == "refs":
            if cs.get("first_as_ref"):
                inputs["images"].append(link(cs["first"]))
            for r in cs["media"]["images"]:
                inputs["images"].append(ad.ref(r, key))
            for kind in ("videos", "audio"):
                for r in cs["media"][kind]:
                    inputs[kind].append(ad.media(r, key))
        api, body = providers.video_body(cs["spec"], mode, card, inputs)
    except Exception as e:                       # noqa: BLE001
        db.update_generation(gen_id, "fail", fail_msg="could not prepare: " + str(e))
        return gen_id, False
    return _send(gen_id, ad, "video", api, body, key, card, "card ")


# ------------------------------------------------------------ files

def download(url, slug, seq, take, fmt="jpg", kind="image"):
    """Pull the asset local immediately - kie.ai result urls expire."""
    folder = os.path.join(db.OUT_DIR, slug, "videos" if kind == "video" else "")
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1]
    if not ext:
        ext = ".mp4" if kind == "video" else "." + (fmt or "jpg")
    suffix = "" if take <= 1 else "_v" + str(take)
    name = "%02d%s%s" % (seq, suffix, ext)
    return _fetch_to(url, os.path.join(folder, name))


def save_bytes(raw, slug, seq, take, ext, kind="image"):
    """Same naming as download(), for providers that answer with the file
    itself instead of a link."""
    folder = os.path.join(db.OUT_DIR, slug, "videos" if kind == "video" else "")
    os.makedirs(folder, exist_ok=True)
    suffix = "" if take <= 1 else "_v" + str(take)
    path = os.path.join(folder, "%02d%s%s" % (seq, suffix, ext))
    with open(path, "wb") as fh:
        fh.write(raw)
    return path


def _fetch_to(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "cherry/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as fh:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            fh.write(chunk)
    return path


def upload_file(path, api_key):
    """Push a local reference to kie.ai storage so the model can fetch it.
    Uploads expire after three days, so this is re-run on demand."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) > 9 * 1024 * 1024:
        raise KieError("reference image is over 9MB - use a smaller file")

    ext = (os.path.splitext(path)[1] or ".jpg").lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get(ext, "image/jpeg")
    # The upload name becomes part of the url the model fetches, so keep it
    # url-safe - a space here silently breaks the reference at generate time.
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem).strip("-")
    payload = {
        "base64Data": "data:" + mime + ";base64," + base64.b64encode(raw).decode(),
        "uploadPath": "images/cherry-refs",
        "fileName": (safe or "ref") + "." + ext,
    }
    limiter.acquire()
    data = _request("POST", UPLOAD_B64, api_key, payload, timeout=180)
    url = data.get("downloadUrl") or data.get("fileUrl")
    if not url:
        raise KieError("upload returned no url: " + json.dumps(data)[:200])
    return url


def upload_stream(path, api_key):
    """Push a reference video or audio file (or any big file) to kie.ai with the
    multipart stream upload - no base64, no 10MB ceiling. Same 3-day expiry as
    every kie upload, so callers cache the link with the usual staleness rule."""
    if not api_key:
        raise KieError("no kie.ai key to upload with")
    stem = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem).strip("-") or "media"
    limiter.acquire()
    data = providers.multipart_post(
        UPLOAD_BASE + "/api/file-stream-upload", {"Authorization": "Bearer " + api_key}, path,
        field="file", extra={"uploadPath": "media/cherry-refs", "fileName": safe + ext},
        who="kie.ai")
    if data.get("code") not in (200, None):
        raise KieError("kie.ai upload failed: " + str(data.get("msg") or data.get("code")))
    inner = data.get("data") or {}
    url = inner.get("downloadUrl") or inner.get("fileUrl")
    if not url:
        raise KieError("kie.ai upload returned no link: " + json.dumps(data)[:200])
    return url


# ------------------------------------------------------------ poller

class Poller(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="kie-poller")
        self.stop_flag = threading.Event()

    def run(self):
        db.init()
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as e:                     # keep the thread alive
                db.log("error", "poller: " + type(e).__name__ + ": " + str(e))
            self.stop_flag.wait(POLL_SECONDS)

    def tick(self):
        db.expire_orphans()
        for job in db.inflight():
            provider = job.get("provider") or "kie"
            key = providers.get_key(provider)
            if key:
                self.advance(job, key)

    def advance(self, job, api_key):
        try:
            res = providers.adapter(job.get("provider") or "kie").poll(job, api_key)
        except KieError as e:
            db.log("warn", "poll card " + str(job["seq"]) + ": " + str(e))
            return

        state = res.get("state")
        if state == "working":
            text = res.get("text") or "generating"
            if text != job["kie_state"]:
                db.update_generation(job["id"], text)
            return

        if state == "fail":
            msg = res.get("msg") or "unknown failure"
            db.update_generation(job["id"], "fail", fail_msg=str(msg))
            db.log("error", "card " + str(job["seq"]) + " failed: " + str(msg))
            return

        if state != "success":
            return

        urls = res.get("urls") or []
        kind = job.get("kind") or "image"
        fmt = "jpg"
        try:
            fmt = (json.loads(job["params"] or "{}").get("output_format") or "jpg")
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        try:
            path = download(urls[0], job["slug"], job["seq"], job["n"], fmt, kind)
        except Exception as e:
            db.update_generation(job["id"], "fail", result_url=urls[0],
                                 fail_msg="download failed: " + str(e))
            db.log("error", "card " + str(job["seq"]) + " download failed: " + str(e))
            return

        db.update_generation(job["id"], "success", result_url=urls[0], local_path=path,
                             credits=res.get("credits"))
        db.log("info", "card " + str(job["seq"]) + " " + kind + " ready -> "
               + os.path.basename(path))


poller = Poller()
