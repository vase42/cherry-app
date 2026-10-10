"""Which service makes the picture, and how to talk to it.

Cherry talks to five services. Each model's exact inputs - which field the
reference pictures go in, which aspect ratios / resolutions / lengths it accepts,
whether a length is sent as "5" or 5 - live in `models.json`, one spec per model,
built from each service's own docs. The request body is generated from that spec,
so adding a model is a data change, not code.

Every provider gets one small adapter with the same jobs:

    build(kind, spec, card, refs)  -> {"api": model id to call, "body": request}
    submit(kind, api, body, key)   -> a task to poll, or the finished bytes
    poll(job, key)                 -> working | success (urls) | fail

Async providers (kie.ai, fal.ai, Replicate) hand back a task id; sync ones
(Gemini, OpenAI) hand back the picture in the response, so the engine runs the
call on a thread instead of making you wait on the request.

Claiming a card, rate limiting, saving the file and the "never bill twice" rules
stay in kie.py, shared by all.
"""

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request

import db

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MODELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")


class ProviderError(Exception):
    pass


# ------------------------------------------------------------ catalog

PROVIDERS = {
    "kie": {"label": "kie.ai", "key_url": "https://kie.ai/api-key",
            "blurb": "One key for dozens of models: Nano Banana, Seedream, Kling, Veo, Seedance, Wan, Hailuo."},
    "fal": {"label": "fal.ai", "key_url": "https://fal.ai/dashboard/keys",
            "blurb": "A wide catalogue of image and video models, billed per run."},
    "replicate": {"label": "Replicate", "key_url": "https://replicate.com/account/api-tokens",
                  "blurb": "Official models from Google, ByteDance, Kling, Luma, Runway and more."},
    "gemini": {"label": "Google Gemini", "key_url": "https://aistudio.google.com/apikey",
               "blurb": "Nano Banana straight from Google. Stills only."},
    "openai": {"label": "OpenAI", "key_url": "https://platform.openai.com/api-keys",
               "blurb": "GPT Image. Stills only."},
}

DEFAULT_PROVIDER = "kie"


def _load_models():
    try:
        with open(MODELS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    return {pid: {"image": (data.get(pid) or {}).get("image") or [],
                  "video": (data.get(pid) or {}).get("video") or []} for pid in PROVIDERS}


MODELS = _load_models()

# What the rest of the app (and the selftest) reads: label, kinds and model list.
CATALOG = {}
for _pid, _meta in PROVIDERS.items():
    CATALOG[_pid] = dict(_meta, image=MODELS[_pid]["image"], video=MODELS[_pid]["video"],
                         kinds=[k for k in ("image", "video") if MODELS[_pid][k]])


# Models a service has retired, and what replaces them. Sessions and cards that
# still name the old one are moved across on start, so nothing points at a dead
# endpoint.
RENAMED = {
    ("fal", "video", "fal-ai/kling-video/v2.1/standard/image-to-video"):
        "fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
    ("fal", "video", "fal-ai/kling-video/v2.1/pro/image-to-video"):
        "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    ("fal", "image", "fal-ai/imagen4/preview"): "fal-ai/nano-banana-2",
    ("replicate", "video", "openai/sora-2"): "kwaivgi/kling-v2.6",
    ("openai", "image", "dall-e-3"): "gpt-image-2",
    ("openai", "image", "gpt-image-1"): "gpt-image-2",
}


def migrate_retired():
    """Point sessions and cards at a live model when theirs was retired."""
    with db._write_lock:
        c = db.conn()
        for (pid, kind, old), new in RENAMED.items():
            c.execute("UPDATE sessions SET " + kind + "_model=? WHERE " + kind
                      + "_provider=? AND " + kind + "_model=?", (new, pid, old))
            col, mcol = ("provider", "model") if kind == "image" else ("video_provider", "video_model")
            c.execute("UPDATE cards SET " + mcol + "=? WHERE " + col + "=? AND " + mcol + "=?",
                      (new, pid, old))
            if db.get_setting(kind + "_provider") == pid and db.get_setting(kind + "_model") == old:
                c.execute("UPDATE settings SET value=? WHERE key=?", (new, kind + "_model"))
        c.commit()


def provider_ids():
    return list(CATALOG)


def label(provider):
    return CATALOG.get(provider, {}).get("label", provider)


def key_setting(provider):
    """kie.ai keeps its original settings key so existing installs still work."""
    return "kie_api_key" if provider == "kie" else "key_" + provider


def get_key(provider):
    return (db.get_setting(key_setting(provider)) or "").strip()


def model_spec(provider, kind, model):
    """The spec for a model - or, for a custom id typed in by hand, a copy of the
    provider's first spec with the id swapped in, so an unfamiliar model is sent
    in the shape that provider's usual ones take."""
    entries = (CATALOG.get(provider) or {}).get(kind) or []
    for m in entries:
        if m["id"] == model:
            return m
    spec = json.loads(json.dumps(entries[0])) if entries else {"controls": {}, "fixed": {}}
    spec.update(id=model, api=model, label=model, custom=True)
    spec.pop("edit", None)
    return spec


def default_model(provider, kind):
    entries = (CATALOG.get(provider) or {}).get(kind) or []
    return entries[0]["id"] if entries else ""


def public_catalog():
    """What the browser needs to draw the pickers - no keys, no field names."""
    out = {}
    for pid, p in CATALOG.items():
        def ctls(cs):
            return {k: {"values": c["values"], "default": c["default"]} for k, c in (cs or {}).items()}

        def pub(m):
            refs = m.get("refs") or {}
            out = {"id": m["id"], "label": m["label"], "mode": m.get("mode", "both"),
                   "refs": refs.get("max") or 0, "note": m.get("note", ""),
                   "controls": ctls(m.get("controls"))}
            if m.get("modes"):
                md = {}
                for name, v in m["modes"].items():
                    e = {"controls": ctls(v.get("controls"))}
                    if name == "frames":
                        e["last"] = bool(v.get("last") or v.get("flf"))
                    if name == "refs":
                        for kind in ("images", "videos", "audio"):
                            if v.get(kind):
                                e[kind] = v[kind]["max"]
                        e["syntax"] = v.get("syntax") or {}
                    md[name] = e
                out["modes"] = md
            return out
        out[pid] = {
            "label": p["label"], "blurb": p["blurb"], "key_url": p["key_url"],
            "kinds": p["kinds"], "image": [pub(m) for m in p["image"]],
            "video": [pub(m) for m in p["video"]],
            "has_key": bool(get_key(pid)), "key_hint": _mask(get_key(pid)),
        }
    return out


def _mask(key):
    if not key:
        return ""
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "set"


# ------------------------------------------------------------ shared bits

def http(method, url, headers, body=None, timeout=60, who="provider"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in headers.items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code in (401, 403):
            raise ProviderError(who + " rejected the API key (" + str(e.code)
                                + ") - check it in Services") from None
        if e.code == 402:
            raise ProviderError(who + " says the account is out of credit") from None
        if e.code == 429:
            raise ProviderError("rate limited by " + who + " (429) - try again shortly") from None
        raise ProviderError(who + " HTTP " + str(e.code) + ": " + _clean(detail)) from None
    except urllib.error.URLError as e:
        raise ProviderError("network error: " + str(e.reason)) from None
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise ProviderError(who + " returned something that is not JSON") from None


def _clean(text):
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    for k in ("detail", "error", "message", "title"):
        v = obj.get(k) if isinstance(obj, dict) else None
        if v:
            return v if isinstance(v, str) else json.dumps(v)[:300]
    return text


def data_uri(path, max_mb=15):
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) > max_mb * 1024 * 1024:
        raise ProviderError("reference image is over " + str(max_mb) + "MB")
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode()


def _boundary():
    return "----cherry" + base64.b16encode(os.urandom(12)).decode().lower()


def multipart_post(url, headers, path, field="file", extra=None, who="provider", timeout=600):
    """POST one file as multipart/form-data, stdlib only. The file is read in
    one go - fine for clips of a few hundred MB on a desktop."""
    b = _boundary()
    parts = []
    for k, v in (extra or {}).items():
        parts.append(("--" + b + "\r\nContent-Disposition: form-data; name=\"" + k
                      + "\"\r\n\r\n" + str(v) + "\r\n").encode())
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        raw = fh.read()
    parts.append(("--" + b + "\r\nContent-Disposition: form-data; name=\"" + field
                  + "\"; filename=\"" + os.path.basename(path).replace('"', "") + "\"\r\n"
                  + "Content-Type: " + mime + "\r\n\r\n").encode() + raw + b"\r\n")
    parts.append(("--" + b + "--\r\n").encode())
    data = b"".join(parts)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + b)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ProviderError(who + " upload HTTP " + str(e.code) + ": "
                            + _clean(e.read().decode("utf-8", "replace")[:400])) from None
    except urllib.error.URLError as e:
        raise ProviderError("network error: " + str(e.reason)) from None
    try:
        return json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        raise ProviderError(who + " upload returned something that is not JSON") from None


def put_bytes(url, path, mime, who="provider", timeout=600):
    with open(path, "rb") as fh:
        raw = fh.read()
    req = urllib.request.Request(url, data=raw, method="PUT")
    req.add_header("Content-Type", mime)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        raise ProviderError(who + " upload HTTP " + str(e.code)) from None
    except urllib.error.URLError as e:
        raise ProviderError("network error: " + str(e.reason)) from None


def fetch_data_uri(url, max_mb=15):
    """Pull a linked image down and inline it, for services that will not fetch
    a url themselves."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(max_mb * 1024 * 1024 + 1)
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
    except (urllib.error.URLError, ValueError) as e:
        raise ProviderError("could not fetch the reference image: " + str(e)) from None
    if len(raw) > max_mb * 1024 * 1024:
        raise ProviderError("reference image is over " + str(max_mb) + "MB")
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode()


def split_data_uri(uri):
    head, b64 = uri.split(",", 1)
    mime = head[5:].split(";")[0] or "image/jpeg"
    return mime, b64


def audit(payload):
    """The body as stored for inspection: inline images become a placeholder
    rather than megabytes of base64 in the database."""
    def walk(v):
        if isinstance(v, str) and v.startswith("data:"):
            return "<inline image, " + str(max(1, len(v) // 1024)) + " KB>"
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(payload)


def _urls_from(output):
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [u for u in output if isinstance(u, str)]
    return []


# ------------------------------------------------------------ the generic builder

# card field for each control, per stage
CARD_FIELDS = {
    "image": (("aspect", "aspect_ratio"), ("resolution", "resolution"), ("format", "output_format")),
    "video": (("duration", "video_duration"), ("resolution", "video_resolution"),
              ("aspect", "video_aspect")),
}


def snap(key, ctl, value):
    """What a card's stored value becomes for this model. Mirrors ctlValue() in
    app.js, so the dropdown always shows what is sent: an exact match, jpg/jpeg
    treated as one, the nearest length for a duration, otherwise the default."""
    vals = [str(v) for v in ctl["values"]]
    v = "" if value is None else str(value)
    if v in vals:
        return v
    if key == "format":
        alias = {"jpg": "jpeg", "jpeg": "jpg"}.get(v)
        if alias in vals:
            return alias
    if key == "duration":
        try:
            n = float(v)
            nums = [x for x in vals if x.lstrip("-").isdigit()]
            if nums:
                return min(nums, key=lambda x: abs(int(x) - n))
        except ValueError:
            pass
    return str(ctl["default"])


def _cast(ctl, v):
    v = (ctl.get("map") or {}).get(v, v)
    if ctl.get("type") == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    return v


def put(body, field, value):
    """Set a field; a dotted name nests (generationConfig.imageConfig.aspectRatio)."""
    parts = field.split(".")
    for p in parts[:-1]:
        body = body.setdefault(p, {})
    body[parts[-1]] = value


def control_values(kind, controls, card):
    """{control: value as sent} for every control the model has. Always sent, so
    a model that requires a field never gets a request without it."""
    d = db.defaults() if kind == "image" else db.video_defaults()
    fallback = {"aspect_ratio": d.get("aspect_ratio"), "resolution": d.get("resolution"),
                "output_format": d.get("output_format"), "video_duration": d.get("duration"),
                "video_resolution": d.get("resolution"), "video_aspect": d.get("aspect_ratio")}
    out = {}
    for key, field in CARD_FIELDS[kind]:
        ctl = (controls or {}).get(key)
        if not ctl or not ctl.get("values"):
            continue
        out[key] = (ctl, _cast(ctl, snap(key, ctl, card.get(field) or fallback.get(field))))
    return out


def generic_body(kind, spec, card, refs):
    """The request for any spec-described model. Returns (api, body)."""
    refs = [r for r in refs if r]
    api = spec.get("api") or spec["id"]
    controls, fixed, rspec = spec.get("controls") or {}, spec.get("fixed") or {}, spec.get("refs")
    if kind == "image" and refs and spec.get("edit"):
        e = spec["edit"]
        api, controls, fixed, rspec = e["api"], e.get("controls") or controls, e.get("fixed") or {}, e.get("refs")

    body = json.loads(json.dumps(fixed))
    prompt = card.get("video_prompt" if kind == "video" else "prompt") or ""
    put(body, spec.get("prompt") or "prompt", prompt)
    for key, (ctl, value) in control_values(kind, controls, card).items():
        put(body, ctl["field"], value)

    if kind == "video":
        ff = spec.get("first_frame")
        if not ff:
            raise ProviderError(spec["label"] + " cannot start from a picture")
        if not refs:
            raise ProviderError("a clip needs a picture to start from")
        first, extra = refs[0], refs[1:]
        if rspec and extra and rspec["field"] == ff["field"]:
            put(body, ff["field"], [first] + extra[:max(0, (rspec.get("max") or 8) - 1)])
        else:
            put(body, ff["field"], [first] if ff.get("list") else first)
            if extra and rspec:
                put(body, rspec["field"], extra[:rspec.get("max") or 8] if rspec.get("list") else extra[0])
            elif extra:
                raise ProviderError(spec["label"] + " takes only the starting picture - "
                                    "remove the extra references")
        return api, body

    if refs:
        if not rspec:
            raise ProviderError(spec["label"] + " cannot take reference images - "
                                "remove them, or pick a model that can")
        cap = rspec.get("max") or 8
        if len(refs) > cap:
            raise ProviderError(spec["label"] + " takes at most " + str(cap) + " reference "
                                "image" + ("" if cap == 1 else "s") + " - this shot has "
                                + str(len(refs)))
        put(body, rspec["field"], refs if rspec.get("list", True) else refs[0])
    elif rspec and rspec.get("always"):
        put(body, rspec["field"], [])
    elif spec.get("mode") == "edit":
        raise ProviderError(spec["label"] + " edits a picture - attach a reference first")
    return api, body


# ------------------------------------------------------------ clips

MEDIA_KINDS = ("images", "videos", "audio")


def video_modes(spec):
    """The modes a clip model can run in. An old-style or hand-typed spec
    (no "modes") is read as frames-only, with its refs as extras."""
    if spec.get("modes"):
        return spec["modes"]
    ff = spec.get("first_frame")
    if not ff:
        return {}
    return {"frames": {"api": spec.get("api") or spec["id"], "controls": spec.get("controls"),
                       "fixed": spec.get("fixed"), "first": ff, "last": None}}


def pick_mode(spec, wanted, has_first=False, has_refs=False):
    """The mode a clip runs in: the one asked for if the model has it;
    otherwise references (the default whenever the model has them), then
    frames when a first frame is set, then text."""
    modes = video_modes(spec)
    if wanted in modes:
        return wanted
    if "refs" in modes:
        return "refs"
    if has_first and "frames" in modes:
        return "frames"
    for m in ("text", "frames", "refs"):
        if m in modes:
            return m
    return None


def mode_variant(spec, mode, has_last=False):
    """{api, controls, fixed, ...} for a mode; a separate first+last endpoint
    takes over when a last frame is given and the main one has none."""
    v = video_modes(spec).get(mode)
    if not v:
        return None
    if mode == "frames" and has_last and not v.get("last") and v.get("flf"):
        v = v["flf"]
    return v


def _place(body, f, url):
    """Put one frame where the model wants it: a field, a list, or a slot in
    a shared list (Veo's image_urls[0] / [1])."""
    if f.get("index") is not None:
        lst = body.get(f["field"])
        if not isinstance(lst, list):
            lst = []
        while len(lst) <= f["index"]:
            lst.append(None)
        lst[f["index"]] = url
        body[f["field"]] = lst
    elif f.get("list"):
        body[f["field"]] = [url]
    else:
        body[f["field"]] = url


def video_body(spec, mode, card, inputs):
    """The request for a clip. `inputs` holds resolved links:
    {first, last, images: [...], videos: [...], audio: [...]}. Returns
    (api, body). Anything the mode cannot take is a refusal, never a trim."""
    label = spec.get("label") or spec["id"]
    v = mode_variant(spec, mode, bool(inputs.get("last")))
    if not v:
        raise ProviderError(label + " cannot make a clip that way - pick another model or mode")
    api = v.get("api") or spec.get("api") or spec["id"]
    body = json.loads(json.dumps(v.get("fixed") or {}))
    put(body, spec.get("prompt") or "prompt", card.get("video_prompt") or "")
    for key, (ctl, value) in control_values("video", v.get("controls") or spec.get("controls"), card).items():
        put(body, ctl["field"], value)

    if mode == "frames":
        if not inputs.get("first"):
            raise ProviderError("this clip needs a first frame - choose one, or switch to text only")
        _place(body, v["first"], inputs["first"])
        if inputs.get("last"):
            if not v.get("last"):
                raise ProviderError(label + " cannot take a last frame")
            _place(body, v["last"], inputs["last"])
    elif mode == "refs":
        total = 0
        for kind in MEDIA_KINDS:
            items = [u for u in inputs.get(kind) or [] if u]
            if not items:
                continue
            r = v.get(kind)
            if not r:
                raise ProviderError(label + " cannot take reference " + kind)
            if len(items) > r["max"]:
                raise ProviderError(label + " takes at most " + str(r["max"]) + " reference "
                                    + kind + " - this clip has " + str(len(items)))
            put(body, r["field"], items if r.get("list", True) else items[0])
            total += len(items)
        if not total:
            raise ProviderError("reference mode needs at least one reference")
        if inputs.get("videos") and "audio" in body and body.get("audio") is not False \
                and spec["id"].startswith("kling-3.0-omni"):
            body["audio"] = False          # Kling Omni rejects sound together with a video
    return api, body


# ------------------------------------------------------------ adapters

class Adapter:
    sync = False
    name = ""

    def ref(self, ref, key):
        """A reference as this provider wants to receive it. Everything but
        kie.ai takes the picture inline, so there is no upload step to expire."""
        if ref.get("kind") == "url" or not ref.get("source_path"):
            return ref.get("upload_url")
        if not os.path.isfile(ref["source_path"]):
            return ref.get("upload_url")
        return data_uri(ref["source_path"])

    def image_ref(self, gen, key):
        """One of our own finished images, as the first frame of a clip."""
        path = gen.get("local_path")
        if not path or not os.path.isfile(path):
            raise ProviderError("the image file for that take is missing from disk")
        return data_uri(path)

    def media(self, ref, key):
        """A reference video or audio file as a link the service can fetch.
        Pasted links pass straight through; local files are uploaded with the
        service's own upload API, and the link is cached on the ref."""
        if ref.get("kind") == "url" or not ref.get("source_path"):
            return ref.get("upload_url")
        if ref.get("upload_url") and not db.upload_is_stale(ref, days=self.media_days):
            return ref["upload_url"]
        path = ref["source_path"]
        if not os.path.isfile(path):
            raise ProviderError("the file for reference '" + str(ref.get("name")) + "' is missing")
        url = self.upload(path, key)
        db.set_ref_upload(ref["id"], url)
        return url

    media_days = 2.5

    def upload(self, path, key):
        raise ProviderError(self.name + " cannot take reference videos or audio here")

    def build(self, kind, spec, card, refs):
        api, body = generic_body(kind, spec, card, refs)
        return {"api": api, "body": body}

    def submit(self, kind, api, body, key):
        raise NotImplementedError

    def poll(self, job, key):
        raise NotImplementedError


class FalAdapter(Adapter):
    name = "fal.ai"
    QUEUE = "https://queue.fal.run/"

    def _h(self, key):
        return {"Authorization": "Key " + key}

    media_days = 30

    def upload(self, path, key):
        """fal storage: ask for an upload slot, PUT the bytes, keep the link."""
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        slot = http("POST", "https://rest.alpha.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3",
                    self._h(key), {"content_type": mime, "file_name": os.path.basename(path)},
                    who="fal.ai")
        if not slot.get("upload_url") or not slot.get("file_url"):
            raise ProviderError("fal.ai gave no upload slot: " + json.dumps(slot)[:200])
        put_bytes(slot["upload_url"], path, mime, who="fal.ai")
        return slot["file_url"]

    def submit(self, kind, api, body, key):
        data = http("POST", self.QUEUE + api, self._h(key), body, who="fal.ai")
        rid = data.get("request_id")
        if not rid:
            raise ProviderError("fal.ai returned no request_id: " + json.dumps(data)[:200])
        return {"task": rid, "meta": {"status_url": data.get("status_url"),
                                      "response_url": data.get("response_url")}}

    def poll(self, job, key):
        meta = json.loads(job.get("task_meta") or "{}")
        if not meta.get("status_url"):
            return {"state": "fail", "msg": "lost the fal.ai status link"}
        st = http("GET", meta["status_url"], self._h(key), who="fal.ai")
        status = (st.get("status") or "").upper()
        if status == "IN_QUEUE":
            return {"state": "working", "text": "queuing"}
        if status == "IN_PROGRESS":
            return {"state": "working", "text": "generating"}
        if status != "COMPLETED":
            return {"state": "working", "text": "waiting"}
        try:
            res = http("GET", meta["response_url"], self._h(key), who="fal.ai")
        except ProviderError as e:
            return {"state": "fail", "msg": str(e)}
        urls = []
        for img in res.get("images") or []:
            if isinstance(img, dict) and img.get("url"):
                urls.append(img["url"])
        for k in ("image", "video"):
            v = res.get(k)
            if isinstance(v, dict) and v.get("url"):
                urls.append(v["url"])
        if not urls:
            return {"state": "fail", "msg": "fal.ai finished with no file: " + json.dumps(res)[:200]}
        return {"state": "success", "urls": urls}


class ReplicateAdapter(Adapter):
    name = "Replicate"
    API = "https://api.replicate.com/v1/"

    def _h(self, key):
        return {"Authorization": "Bearer " + key}

    media_days = 0.9                     # Replicate deletes uploaded files after a day

    def upload(self, path, key):
        data = multipart_post(self.API + "files", self._h(key), path, field="content",
                              who="Replicate")
        url = (data.get("urls") or {}).get("get")
        if not url:
            raise ProviderError("Replicate returned no file link: " + json.dumps(data)[:200])
        return url

    def submit(self, kind, api, body, key):
        if "/" not in api:
            raise ProviderError("Replicate models look like owner/name - got '" + api + "'")
        data = http("POST", self.API + "models/" + api + "/predictions", self._h(key),
                    {"input": body}, who="Replicate")
        rid = data.get("id")
        if not rid:
            raise ProviderError("Replicate returned no id: " + json.dumps(data)[:200])
        get = (data.get("urls") or {}).get("get") or (self.API + "predictions/" + rid)
        return {"task": rid, "meta": {"get": get}}

    def poll(self, job, key):
        meta = json.loads(job.get("task_meta") or "{}")
        url = meta.get("get") or (self.API + "predictions/" + str(job.get("kie_task_id")))
        data = http("GET", url, self._h(key), who="Replicate")
        status = data.get("status")
        if status in ("starting", "processing"):
            return {"state": "working",
                    "text": "queuing" if status == "starting" else "generating"}
        if status == "succeeded":
            urls = _urls_from(data.get("output"))
            if not urls:
                return {"state": "fail", "msg": "Replicate finished with no output"}
            return {"state": "success", "urls": urls}
        if status in ("failed", "canceled"):
            return {"state": "fail", "msg": str(data.get("error") or status)}
        return {"state": "working", "text": "waiting"}


class GeminiAdapter(Adapter):
    name = "Google Gemini"
    sync = True
    API = "https://generativelanguage.googleapis.com/v1beta/models/"

    def ref(self, ref, key):
        out = Adapter.ref(self, ref, key)
        return fetch_data_uri(out) if out and out.startswith("http") else out

    def build(self, kind, spec, card, refs):
        if kind != "image":
            raise ProviderError("Gemini makes stills here, not clips")
        refs = [r for r in refs if r]
        cap = (spec.get("refs") or {}).get("max") or 3
        if len(refs) > cap:
            raise ProviderError(spec["label"] + " takes at most " + str(cap) + " reference images")
        parts = [{"text": card.get("prompt") or ""}]
        for r in refs:
            if not r.startswith("data:"):
                raise ProviderError("Gemini could not read a reference image")
            mime, b64 = split_data_uri(r)
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"]}}
        cfg = {}
        for key, (ctl, value) in control_values("image", spec.get("controls"), card).items():
            if not (key == "aspect" and value == "auto"):
                cfg[ctl["field"]] = value
        if cfg:
            body["generationConfig"]["imageConfig"] = cfg
        return {"api": spec.get("api") or spec["id"], "body": body}

    def submit(self, kind, api, body, key):
        data = http("POST", self.API + api + ":generateContent",
                    {"x-goog-api-key": key}, body, timeout=240, who="Gemini")
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    return {"bytes": base64.b64decode(inline["data"]),
                            "ext": mimetypes.guess_extension(mime) or ".png"}
        why = ((data.get("promptFeedback") or {}).get("blockReason")
               or ((data.get("candidates") or [{}])[0]).get("finishReason")
               or "no image came back")
        raise ProviderError("Gemini returned no image (" + str(why) + ")")


class OpenAIAdapter(Adapter):
    name = "OpenAI"
    sync = True
    API = "https://api.openai.com/v1/images/"
    SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536",
             "16:9": "1536x864", "9:16": "864x1536", "4:3": "1536x1152", "3:4": "1152x1536"}

    def build(self, kind, spec, card, refs):
        if kind != "image":
            raise ProviderError("OpenAI makes stills here, not clips")
        refs = [r for r in refs if r]
        body = {"model": spec.get("api") or spec["id"], "prompt": card.get("prompt") or ""}
        body.update(spec.get("fixed") or {})
        for key, (ctl, value) in control_values("image", spec.get("controls"), card).items():
            body[ctl["field"]] = self.SIZES.get(value, "auto") if key == "aspect" else value
        if refs:
            if len(refs) > 16:
                raise ProviderError("OpenAI takes at most 16 reference images")
            body["images"] = [{"image_url": r} for r in refs]
        return {"api": "edits" if refs else "generations", "body": body}

    def submit(self, kind, api, body, key):
        data = http("POST", self.API + api, {"Authorization": "Bearer " + key}, body,
                    timeout=300, who="OpenAI")
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            fmt = body.get("output_format") or "png"
            return {"bytes": base64.b64decode(item["b64_json"]),
                    "ext": "." + ("jpg" if fmt == "jpeg" else fmt)}
        if item.get("url"):
            return {"url": item["url"]}
        raise ProviderError("OpenAI returned no image")


class KieAdapter(Adapter):
    """kie.ai: every model goes through jobs/createTask with {model, input}. The
    low-level calls stay in kie.py - the self-test stubs them there."""
    name = "kie.ai"

    def ref(self, ref, key):
        import kie
        return kie.ref_url(ref, key)

    def image_ref(self, gen, key):
        import kie
        return kie.ensure_image_upload(gen, key)

    def upload(self, path, key):
        import kie
        return kie.upload_stream(path, key)

    def submit(self, kind, api, body, key):
        import kie
        return {"task": kie.create_task(key, api, body), "meta": {}}

    def poll(self, job, key):
        import kie
        data = kie.fetch_task(key, job["kie_task_id"])
        state = data.get("state")
        if state in ("waiting", "queuing", "generating"):
            return {"state": "working", "text": state}
        if state == "fail":
            msg = data.get("failMsg") or data.get("failCode") or "unknown failure"
            return {"state": "fail", "msg": str(msg)}
        if state != "success":
            return {"state": "working", "text": job.get("kie_state") or "waiting"}
        urls = kie.parse_result_urls(data)
        if not urls:
            return {"state": "fail", "msg": "success with no resultUrls"}
        return {"state": "success", "urls": urls,
                "credits": data.get("creditsConsumed")}


ADAPTERS = {
    "kie": KieAdapter(),
    "fal": FalAdapter(),
    "replicate": ReplicateAdapter(),
    "gemini": GeminiAdapter(),
    "openai": OpenAIAdapter(),
}


def adapter(provider):
    a = ADAPTERS.get(provider or DEFAULT_PROVIDER)
    if not a:
        raise ProviderError("unknown provider: " + str(provider))
    return a
