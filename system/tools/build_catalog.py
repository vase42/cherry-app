"""Turn the research JSON into app/models.json - one normalised spec per model.

Normalised spec:
  id        key stored on cards (unique within provider+kind)
  api       model string sent to the service (default: id)
  label, mode ("text"|"edit"|"both"), note
  prompt    prompt field name
  refs      {field, list, max, required}   images: reference images; video: extra refs
  first_frame {field, list}               video only
  edit      {api, refs, controls, fixed}  the variant used when references are attached
  controls  {aspect|resolution|format|duration: {field, values[str], default str, type, map?}}
  fixed     fields always sent
"""
import json, os, re, sys

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research")
from math import ceil, gcd  # noqa: E402
OUT = sys.argv[1]

SIZE_TO_RATIO = {
    "square_hd": "1:1", "square": "1:1", "portrait_4_3": "3:4", "portrait_3_2": "2:3",
    "portrait_16_9": "9:16", "landscape_4_3": "4:3", "landscape_3_2": "3:2",
    "landscape_16_9": "16:9", "landscape_21_9": "21:9",
}


def load(name):
    p = os.path.join(R, name)
    return json.load(open(p)) if os.path.exists(p) else None


def num_range(spec):
    """'integer 4-30 or -1' / {'min':2,'max':15} -> list of str seconds."""
    lo = hi = None
    if isinstance(spec, dict) and isinstance(spec.get("values"), dict):
        spec = spec["values"]
    if isinstance(spec, dict) and spec.get("min") is not None:
        lo, hi = ceil(spec["min"]), int(spec["max"])
    elif isinstance(spec, str):
        m = re.search(r"(\d+)\s*-\s*(\d+)", spec)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
    if lo is None:
        return None
    lo = max(int(lo), 1)
    vals = list(range(lo, min(int(hi), 15) + 1))
    vals += [v for v in (20, 25, 30) if lo <= v <= int(hi) and v > 15]
    return [str(v) for v in vals]


def ctl(raw, kind_key, prefer=None):
    if not raw:
        return None
    field = raw.get("field")
    vals = raw.get("values")
    typ = (raw.get("type") or "").lower()
    smap = {}
    if kind_key == "duration":
        if isinstance(vals, (str, dict)) or not vals:
            vals = num_range(vals if isinstance(vals, str) else raw)
        if not vals:
            return None
        vals = [str(v) for v in vals if str(v) not in ("-1", "auto", "0")]
        # "4s"/"6s" on the wire, 4/6 on screen
        if vals and all(re.fullmatch(r"\d+s", v) for v in vals):
            smap = {v[:-1]: v for v in vals}
            vals = list(smap)
        is_int = typ in ("number", "integer", "int") or all(isinstance(v, int) for v in (raw.get("values") or []) if not isinstance(raw.get("values"), str))
        if typ == "string" or smap:
            is_int = False
        out = {"field": field, "values": vals, "type": "int" if is_int else "str"}
        if smap:
            out["map"] = smap
    else:
        if not vals or isinstance(vals, str):
            return None
        out = {"field": field, "values": [str(v) for v in vals], "type": "str"}
    d = raw.get("default")
    d = None if d is None or isinstance(d, (dict, list)) else str(d)
    if d and smap and d.endswith("s"):
        d = d[:-1]
    if prefer:
        for p in prefer:
            if p in out["values"] and (d is None or d not in out["values"]):
                d = p
                break
    if d not in out["values"]:
        d = out["values"][0]
    out["default"] = d
    return out


def size_ctl(raw):
    """image_size presets shown as ratios; the preset name is what gets sent."""
    if not raw or not raw.get("values"):
        return None
    m, vals = {}, []
    for v in raw["values"]:
        v = str(v)
        r = SIZE_TO_RATIO.get(v)
        wh = re.fullmatch(r"(\d+)x(\d+)", v)
        if not r and wh:
            w, h = int(wh.group(1)), int(wh.group(2))
            g = gcd(w, h)
            r = str(w // g) + ":" + str(h // g)
        if not r and v == "auto":
            r = "auto"
        if r and r not in m:
            m[r] = v
            vals.append(r)
    if not vals:
        return None
    d = raw.get("default")
    dflt = next((r for r, v in m.items() if v == d), None) if isinstance(d, str) else None
    dflt = dflt or ("9:16" if "9:16" in vals else vals[0])
    return {"field": raw["field"], "values": vals, "default": dflt, "type": "str", "map": m}


def controls_of(m, video=False):
    c = {}
    a = ctl(m.get("aspect"), "aspect", ["9:16", "16:9", "1:1", "auto"]) or size_ctl(m.get("size"))
    if a:
        c["aspect"] = a
    r = ctl(m.get("resolution"), "resolution", ["1080p", "1080P", "720p", "2K", "1K"] if video else ["1K", "2K"])
    if r:
        c["resolution"] = r
    f = ctl(m.get("format"), "format", ["jpg", "jpeg", "png"])
    if f:
        c["format"] = f
    if video:
        d = ctl(m.get("duration"), "duration", ["5", "6", "8"])
        if d:
            c["duration"] = d
    return c


def refs_of(r, dmax=8):
    if not r:
        return None
    return {"field": r["field"], "list": bool(r.get("list", True)),
            "max": r.get("max") or (dmax if r.get("list", True) else 1),
            "required": bool(r.get("required"))}


def clean_fixed(f):
    out = {}
    for k, v in (f or {}).items():
        k2 = k.replace("(top-level)", "").replace("input.", "").replace(" (inside input)", "").strip()
        if k2 in ("model",) and isinstance(v, str) and "/" not in v and v.startswith(("veo", "flux-kontext")):
            out["model"] = v
        elif k2 == "model":
            continue
        else:
            out[k2] = v
    return out


def base(m, video=False, note=None):
    spec = {
        "id": m["id"], "label": m.get("label") or m["id"],
        "mode": m.get("mode") or ("both" if video else "text"),
        "prompt": m.get("prompt_field") or "prompt",
        "controls": controls_of(m, video),
        "fixed": clean_fixed(m.get("fixed")),
    }
    if video:
        ff = m.get("first_frame") or {}
        spec["first_frame"] = {"field": ff.get("field") or "image_url", "list": bool(ff.get("list"))}
        if m.get("extra_refs"):
            spec["refs"] = refs_of(m["extra_refs"])
    else:
        spec["refs"] = refs_of(m.get("refs"))
    if note:
        spec["note"] = note
    return spec


def pair(text, edit):
    """One entry for a text-to-image model and its edit sibling."""
    s = base(text)
    e = base(edit)
    s["mode"] = "both"
    s["edit"] = {"api": edit["id"], "refs": e["refs"], "controls": e["controls"], "fixed": e["fixed"]}
    s["refs"] = {"field": e["refs"]["field"], "list": e["refs"]["list"], "max": e["refs"]["max"], "required": False}
    return s


# ------------------------------------------------------------------ kie.ai
def kie():
    img = {m["id"]: m for m in load("kie_image.json")}
    lab = lambda s, t: dict(s, label=t)   # noqa: E731
    out_i = []
    for i in ("nano-banana-2", "nano-banana-2-1", "nano-banana-pro", "nano-banana-2-lite"):
        out_i.append(base(img[i]))
    out_i[0]["label"] = "Nano Banana 2"
    out_i[1]["label"] = "Nano Banana 2.1"
    out_i[2]["label"] = "Nano Banana Pro"
    out_i[3]["label"] = "Nano Banana 2 Lite"
    # nano-banana-2 always sends image_input, even empty - the original contract
    out_i[0]["refs"]["always"] = True
    out_i.append(lab(pair(img["google/nano-banana"], img["google/nano-banana-edit"]), "Nano Banana"))
    out_i.append(lab(pair(img["seedream/5-pro-text-to-image"], img["seedream/5-pro-image-to-image"]), "Seedream 5.0 Pro"))
    out_i.append(lab(pair(img["seedream/5-lite-text-to-image"], img["seedream/5-lite-image-to-image"]), "Seedream 5.0 Lite"))
    out_i.append(lab(pair(img["seedream/4.5-text-to-image"], img["seedream/4.5-edit"]), "Seedream 4.5"))
    out_i.append(lab(pair(img["bytedance/seedream-v4-text-to-image"], img["bytedance/seedream-v4-edit"]), "Seedream 4.0"))
    out_i.append(lab(pair(img["gpt-image-2-text-to-image"], img["gpt-image-2-image-to-image"]), "GPT Image 2"))
    g15 = lab(pair(img["gpt-image/1.5-text-to-image"], img["gpt-image/1.5-image-to-image"]), "GPT Image 1.5")
    g15["controls"]["resolution"] = {"field": "quality", "values": ["medium", "high"], "default": "medium", "type": "str"}
    g15["fixed"].pop("quality", None)
    g15["edit"]["controls"]["resolution"] = g15["controls"]["resolution"]
    g15["edit"]["fixed"].pop("quality", None)
    out_i.append(g15)
    out_i.append(lab(base(img["4o-image-api"]), "GPT-4o Image"))
    out_i.append(lab(pair(img["flux-2/pro-text-to-image"], img["flux-2/pro-image-to-image"]), "FLUX.2 Pro"))
    for tier, name in (("flux-kontext-pro", "FLUX Kontext Pro"), ("flux-kontext-max", "FLUX Kontext Max")):
        k = base(img["flux1-kontext"])
        k.update(id="flux1-kontext:" + tier.split("-")[-1], api="flux1-kontext", label=name)
        k["fixed"] = {"model": tier}
        out_i.append(k)
    out_i.append(lab(pair(img["grok-imagine/text-to-image"], img["grok-imagine/image-to-image"]), "Grok Imagine"))
    out_i.append(lab(pair(img["ideogram/v3-text-to-image"], img["ideogram/v3-remix"]), "Ideogram V3"))
    out_i.append(lab(pair(img["qwen/text-to-image"], img["qwen/image-edit"]), "Qwen Image"))
    out_i.append(lab(base(img["wan/2-7-image"]), "Wan 2.7 Image"))
    out_i.append(lab(base(img["z-image"]), "Z-Image"))
    for i, n in (("google/imagen4-ultra", "Imagen 4 Ultra"), ("google/imagen4", "Imagen 4"), ("google/imagen4-fast", "Imagen 4 Fast")):
        out_i.append(lab(base(img[i], note="Google retired Imagen on its own API; kie.ai may still serve it"), n))

    vid = load("kie_video.json")
    out_v = []
    seen = set()
    for m in vid:
        s = base(m, video=True)
        if m["id"] == "veo-3-1":
            tier = m["fixed"].get("input.model")
            s.update(id="veo-3-1:" + tier, api="veo-3-1")
            s["fixed"] = {"model": tier, "generation_type": m["fixed"].get("generation_type", "FIRST_AND_LAST_FRAMES_2_VIDEO")}
            s["note"] = "Google retires Veo 3.1 on its own API on 22 Oct 2026"
        if m["id"] == "runway":
            s["fixed"] = {}
        if m["id"] in ("kling-2.6/image-to-video",):
            s["fixed"]["sound"] = False
        if m["id"] == "kling-3.0/video":
            s["fixed"].update(sound=False, multi_shots=False)
        if m["id"] == "wan/2-6-flash-image-to-video":
            s["fixed"]["audio"] = False
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        s["label"] = re.sub(r"\s*Image to Video\s*", " ", s["label"]).strip()
        out_v.append(s)
    # The model Banana shipped with: reference-image driven, not an exact first frame.
    out_v.append({
        "id": "gemini-omni-video", "label": "Gemini Omni Video (original)", "mode": "both",
        "prompt": "prompt", "first_frame": {"field": "image_urls", "list": True},
        "refs": {"field": "image_urls", "list": True, "max": 7, "required": False},
        "controls": {
            "duration": {"field": "duration", "values": ["4", "6", "8", "10"], "default": "8", "type": "str"},
            "resolution": {"field": "resolution", "values": ["480p", "720p", "1080p"], "default": "1080p", "type": "str"},
            "aspect": {"field": "aspect_ratio", "values": ["9:16", "16:9", "1:1"], "default": "9:16", "type": "str"},
        },
        "fixed": {}, "note": "Uses the shot as a reference rather than an exact first frame",
    })
    return {"image": out_i, "video": out_v}


# ------------------------------------------------------------------ Replicate
DEAD_REPLICATE = {"openai/sora-2", "openai/sora-2-pro", "google/veo-3", "google/veo-3-fast"}


def replicate():
    r = load("replicate.json")
    out_i = []
    for m in r["image"]:
        s = base(m)
        if s["refs"] and not s["refs"].get("max"):
            s["refs"]["max"] = 8
        if m.get("resolution") is None and m.get("size"):
            pass
        if "imagen" in m["id"]:
            s["note"] = "Google retired Imagen on its own API; Replicate may still serve it"
        s["output"] = m.get("output") or "string"
        out_i.append(s)
    out_v = []
    for m in r["video"]:
        if m["id"] in DEAD_REPLICATE:
            continue
        if not m.get("first_frame"):
            continue
        s = base(m, video=True)
        if "veo-3.1" in m["id"]:
            s["note"] = "Google retires Veo 3.1 on its own API on 22 Oct 2026"
        out_v.append(s)
    return {"image": out_i, "video": out_v}


# ------------------------------------------------------------------ Google
def gemini():
    g = load("google_openai.json")["gemini_image"]
    out = []
    for m in g["models"]:
        c = {}
        av = [a for a in m.get("aspect_values") or []]
        if av:
            c["aspect"] = {"field": "aspectRatio", "values": av, "default": "9:16" if "9:16" in av else av[0], "type": "str"}
        rv = [v for v in (m.get("size_or_resolution_values") or []) if v != "512"]
        if len(rv) > 1:
            c["resolution"] = {"field": "imageSize", "values": rv, "default": "1K", "type": "str"}
        out.append({"id": m["id"], "label": m["label"], "mode": "both", "prompt": "text",
                    "refs": {"field": "inline", "list": True, "max": m.get("max_reference_images") or 3, "required": False},
                    "controls": c, "fixed": {}})
    return {"image": out, "video": []}


# ------------------------------------------------------------------ OpenAI
def openai():
    out = []
    keep = {"gpt-image-2.5-sunburst": None, "gpt-image-2.5-flare": None, "gpt-image-2": None,
            "gpt-image-1.5": "OpenAI retires this on 1 Dec 2026",
            "gpt-image-1-mini": "OpenAI retires this on 1 Dec 2026"}
    g = load("google_openai.json")["openai_image"]
    for m in g["models"]:
        if m["id"] not in keep:
            continue
        new = m["id"].startswith("gpt-image-2")
        aspects = ["auto", "1:1", "3:2", "2:3"] + (["16:9", "9:16", "4:3", "3:4"] if new else [])
        q = ["auto", "low", "medium", "high"] + (["xhigh", "max"] if "2.5" in m["id"] else [])
        s = {"id": m["id"], "label": m["label"], "mode": "both", "prompt": "prompt",
             "refs": {"field": "images", "list": True, "max": 16, "required": False},
             "controls": {
                 "aspect": {"field": "size", "values": aspects, "default": "auto", "type": "str"},
                 "resolution": {"field": "quality", "values": q, "default": "auto", "type": "str"},
                 "format": {"field": "output_format", "values": ["png", "jpeg", "webp"], "default": "jpeg", "type": "str"},
             }, "fixed": {"n": 1}}
        if keep[m["id"]]:
            s["note"] = keep[m["id"]]
        out.append(s)
    return {"image": out, "video": []}


# ------------------------------------------------------------------ fal.ai
def fal():
    f = load("fal.json")
    if not f:
        return None
    out_i = []
    for m in f["image"]:
        s = base(m)
        if s["refs"] and not s["refs"].get("max"):
            s["refs"]["max"] = 8
        if m.get("edit_id"):
            s["edit"] = {"api": m["edit_id"], "refs": s["refs"] or {"field": "image_urls", "list": True, "max": 8, "required": True},
                         "controls": s["controls"], "fixed": s["fixed"]}
            s["mode"] = "both"
        out_i.append(s)
    out_v = [base(m, video=True) for m in f["video"] if m.get("first_frame")]
    return {"image": out_i, "video": out_v}


# ------------------------------------------------------------------ clips, v2
#
# A clip model can run in up to three modes, and the card on the Clips board
# shows only the ones its model has:
#   frames - an exact first frame, and a last frame where the model takes one
#   refs   - reference images / videos / audio the prompt talks about
#   text   - nothing but the prompt
# A mode may need a different model id (a text-to-video or reference sibling),
# with its own controls - so each mode carries {api, controls, fixed}.

VFIXED = {   # fields a model requires that have no documented default
    "kling-2.6/image-to-video": {"sound": False},
    "kling-2.6/text-to-video": {"sound": False},
    "kling-3.0/video": {"sound": False, "multi_shots": False},
    "wan/2-6-flash-image-to-video": {"audio": False},
    "kling-3.0-omni/image-to-video": {"aspect_ratio": "auto"},
}
KIE_REF_SIB = {
    "kling-3.0-omni/image-to-video": "kling-3.0-omni/reference-to-video",
    "minimax-h3/image-to-video": "minimax-h3/reference-to-video",
    "wan/2-7-image-to-video": "wan/2-7-r2v",
    "wan/2-6-image-to-video": "wan/2-6-video-to-video",
    "wan/2-6-flash-image-to-video": "wan/2-6-flash-video-to-video",
    "happyhorse-1-1/image-to-video": "happyhorse-1-1/reference-to-video",
}
KIE_FLF_SIB = {"pixverse-v6/image-to-video": "pixverse-v6/transition"}
NOT_FLAT = ("elements", "kling_elements", "audio_ids", "image_references", "first_clip_url",
            "driving_audio_url", "audio_url", "video_list", "first_clip", "start_video",
            "target_audio_url")
DEAD_VIDEO = {"openai/sora-2", "openai/sora-2-pro", "google/veo-3", "google/veo-3-fast"}


def vctl(m):
    return controls_of(m, video=True)


def vfixed(m):
    return dict(VFIXED.get(m["id"], {}))


def flat(ref):
    """A plain list (or single) of links - the only reference shape the card
    edits. Named "elements" and id-based inputs are left out on purpose."""
    if not ref or not ref.get("field"):
        return None
    f = ref["field"]
    if "[" in f or "." in f or f in NOT_FLAT or ref.get("item_shape"):
        return None
    role = (ref.get("role") or "reference").lower()
    if not role.startswith("reference") or "not a reference" in (ref.get("note") or "").lower():
        return None                    # a soundtrack or "continue this clip", not a reference
    return {"field": f, "max": int(ref.get("max") or 1),
            "list": ref.get("list", True) is not False}


def syntax(ref, kind):
    """How the prompt names the n-th reference of a kind, when the docs say."""
    t = ((ref or {}).get("prompt_syntax") or "")
    word = {"images": "Image", "videos": "Video", "audio": "Audio"}[kind]
    if "@image" in t or "@Image" in t:
        return "@" + word + "{n}"
    if "[Image 1]" in t or "[Image1]" in t:
        return "[" + word + " {n}]"
    if "Image 1" in t or "'Image 1'" in t:
        return word + " {n}"
    if "Image1" in t:
        return word + "{n}"
    return ""


def frame(f):
    if not f or not f.get("field"):
        return None
    out = {"field": f["field"], "list": bool(f.get("list"))}
    if f.get("index") is not None:
        out["index"] = int(f["index"])
        out["list"] = True
    return out


def refs_mode(m):
    imgs, vids, aud = flat(m.get("ref_images")), flat(m.get("ref_videos")), flat(m.get("ref_audio"))
    if not (imgs or vids or aud):
        return None
    out = {"api": m["id"], "controls": vctl(m), "fixed": vfixed(m), "syntax": {}}
    for kind, r, raw in (("images", imgs, m.get("ref_images")), ("videos", vids, m.get("ref_videos")),
                         ("audio", aud, m.get("ref_audio"))):
        if r:
            out[kind] = r
            sx = syntax(raw, kind) or syntax(m.get("ref_images"), kind)
            if sx:
                out["syntax"][kind] = sx
    return out


def clip_spec(m, by_id, label=None, ref_sib=None, flf_sib=None, note=None):
    """One clip model with every mode it can run in."""
    modes = {}
    first = frame(m.get("first_frame"))
    if first:
        fr = {"api": m["id"], "controls": vctl(m), "fixed": vfixed(m), "first": first,
              "last": frame(m.get("last_frame"))}
        fs = by_id.get(flf_sib or m.get("flf_sibling") or "")
        if fs and not fr["last"] and frame(fs.get("first_frame")) and frame(fs.get("last_frame")):
            fr["flf"] = {"api": fs["id"], "controls": vctl(fs), "fixed": vfixed(fs),
                         "first": frame(fs.get("first_frame")), "last": frame(fs.get("last_frame"))}
        modes["frames"] = fr
    r = refs_mode(m)
    rs = by_id.get(ref_sib or m.get("ref_sibling") or "")
    if not r and rs:
        r = refs_mode(rs)
    if r:
        modes["refs"] = r
    if m.get("text_only_ok"):
        modes["text"] = {"api": m["id"], "controls": vctl(m), "fixed": vfixed(m)}
    else:
        ts = by_id.get(m.get("t2v_sibling") or "")
        if ts:
            modes["text"] = {"api": ts["id"], "controls": vctl(ts), "fixed": vfixed(ts)}
    main = modes.get("frames") or modes.get("refs") or modes.get("text")
    if not main:
        return None
    spec = {"id": m["id"], "api": m["id"], "label": label or m.get("label") or m["id"],
            "mode": "both", "prompt": "prompt", "controls": main["controls"],
            "fixed": main["fixed"], "modes": modes}
    if note or m.get("note_for_menu"):
        spec["note"] = note or m["note_for_menu"]
    # the older single-frame shape, kept for anything that still reads it
    if "frames" in modes:
        spec["first_frame"] = {"field": modes["frames"]["first"]["field"],
                               "list": modes["frames"]["first"]["list"]}
    return spec


def tidy_label(t):
    t = re.sub(r"\s*\((i2v|t2v|image-to-video)[^)]*\)", "", t)
    return re.sub(r"\s*(Image|Text)[ -]to[ -]Video\s*", " ", t).strip()


def kie_clips():
    rows = load("kie_video_io.json")
    by_id = {}
    for m in rows:
        by_id.setdefault(m["id"], m)
    out = []
    for m in rows:
        if not m.get("in_catalog") and m["id"] != "gemini-omni-video":
            continue
        if m["id"] == "veo-3-1":
            tier = m["required_other"]["input.model"]
            name = {"veo3_fast": "Veo 3.1 Fast", "veo3_lite": "Veo 3.1 Lite", "veo3": "Veo 3.1 Quality"}[tier]
            ctl_ = vctl(m)
            base_fixed = {"model": tier}
            modes = {
                "frames": {"api": "veo-3-1", "controls": ctl_, "first": frame(m["first_frame"]),
                           "last": frame(m["last_frame"]),
                           "fixed": dict(base_fixed, generation_type="FIRST_AND_LAST_FRAMES_2_VIDEO")},
                "text": {"api": "veo-3-1", "controls": ctl_,
                         "fixed": dict(base_fixed, generation_type="TEXT_2_VIDEO")},
            }
            if m.get("ref_images"):
                rc = json.loads(json.dumps(ctl_))
                rc["duration"] = dict(rc.get("duration") or {"field": "duration", "type": "int"},
                                      values=["8"], default="8")
                rc["aspect"] = dict(rc["aspect"], values=[v for v in rc["aspect"]["values"] if v != "Auto"],
                                    default="16:9")
                modes["refs"] = {"api": "veo-3-1", "controls": rc,
                                 "fixed": dict(base_fixed, generation_type="REFERENCE_2_VIDEO"),
                                 "images": {"field": "image_urls", "max": 3, "list": True}, "syntax": {}}
            out.append({"id": "veo-3-1:" + tier, "api": "veo-3-1", "label": name, "mode": "both",
                        "prompt": "prompt", "controls": ctl_, "fixed": modes["frames"]["fixed"],
                        "modes": modes, "note": "Google retires Veo 3.1 on its own API on 22 Oct 2026",
                        "first_frame": {"field": "image_urls", "list": True}})
            continue
        if m["id"] == "gemini-omni-video":
            spec = clip_spec(m, by_id, label="Gemini Omni Video (original)")
        else:
            spec = clip_spec(m, by_id, label=tidy_label(m.get("label") or m["id"]),
                             ref_sib=KIE_REF_SIB.get(m["id"]), flf_sib=KIE_FLF_SIB.get(m["id"]))
        if spec:
            out.append(spec)
    return out


def other_clips(provider, catalog_ids):
    rows = [m for m in load("fal_replicate_video_io.json") if m["provider"] == provider]
    by_id = {m["id"]: m for m in rows}
    out = []
    for m in rows:
        if m["id"] not in catalog_ids or m["id"] in DEAD_VIDEO:
            continue
        note = None
        if "veo-3.1" in m["id"] or "veo3.1" in m["id"]:
            note = "Google retires Veo 3.1 on its own API on 22 Oct 2026"
        spec = clip_spec(m, by_id, label=tidy_label(m.get("label") or m["id"]), note=note)
        if spec:
            out.append(spec)
    return out


def first(lst, ids):
    """Put the dependable everyday models at the top - the first one is the
    default for a new session on that service."""
    rank = {i: n for n, i in enumerate(ids)}
    return sorted(lst, key=lambda m: rank.get(m["id"], len(ids)))


fa = fal()
cat = {"kie": kie(), "replicate": replicate(), "gemini": gemini(), "openai": openai()}
cat["kie"]["video"] = kie_clips()
cat["replicate"]["video"] = other_clips("replicate", {m["id"] for m in load("replicate.json")["video"]})
if fa:
    fa["video"] = other_clips("fal", {m["id"] for m in load("fal.json")["video"]})
cat["kie"]["video"] = first(cat["kie"]["video"], [
    "kling-2.6/image-to-video", "kling-3.0/video", "google/gemini-omni-flash-1-1",
    "bytedance/seedance-2", "hailuo/2-3-image-to-video-standard", "wan/2-6-image-to-video",
    "gemini-omni-video"])
cat["replicate"]["image"] = first(cat["replicate"]["image"], ["google/nano-banana-2", "google/nano-banana-pro", "google/nano-banana"])
cat["replicate"]["video"] = first(cat["replicate"]["video"], ["kwaivgi/kling-v2.6", "bytedance/seedance-2.0", "minimax/hailuo-2.3"])
if fa:
    fa["image"] = first(fa["image"], ["fal-ai/nano-banana-2", "google/nano-banana-2.1", "fal-ai/nano-banana-pro"])
    fa["video"] = first(fa["video"], ["fal-ai/kling-video/v2.6/pro/image-to-video", "bytedance/seedance-2.0/image-to-video",
                                      "fal-ai/minimax/hailuo-2.3/standard/image-to-video"])
cat["gemini"]["image"] = first(cat["gemini"]["image"], ["gemini-nano-banana-2.1", "gemini-3-pro-image"])
if fa:
    cat["fal"] = fa
json.dump(cat, open(OUT, "w"), indent=1)
for p, v in cat.items():
    print(p, len(v["image"]), "image,", len(v["video"]), "video")
