"""Schema, queries and the JSON importer.

Cherry is a flat app: a session holds an ordered list of cards, a card holds a
prompt plus its render settings, and every time you press Generate a new row
lands in `generations`. Nothing is ever overwritten - editing a prompt does not
destroy the take that came from the old one, which is why the Images tab can
show the whole history.

The one derived value worth understanding is the fingerprint. It hashes the
prompt, the settings and the attached reference urls. The UI compares a card's
current fingerprint against its newest generation to decide whether the button
should read "Generate again" (nothing changed) or "Generate" (you edited it).
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time

# Layout:   Cherry/                 <- ROOT: everything served is under here
#             Start Cherry.bat
#             Output/<session>/      <- finished images, and videos/ for clips
#             system/                <- SYSTEM: the program and its private data
#               app/  data/  refs/  characters/
# Run from a flat checkout (no system/ folder) and it all sits beside app/.
SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPLIT = os.path.basename(SYSTEM).lower() == "system"
ROOT = os.path.dirname(SYSTEM) if _SPLIT else SYSTEM
DATA_DIR = os.path.join(SYSTEM, "data")
OUT_DIR = os.path.join(ROOT, "Output") if _SPLIT else os.path.join(ROOT, "out")
REF_DIR = os.path.join(SYSTEM, "refs")
# The cast lives in its own folder, and it is the durable copy. kie.ai forgets
# an upload after three days; these files are what let a character still work
# months later, by being re-uploaded on demand.
CHAR_DIR = os.path.join(SYSTEM, "characters")
DB_PATH = os.path.join(DATA_DIR, "cherry.db")
# The app was called Banana; an existing database under the old name is moved
# across on first start rather than silently replaced by an empty one.
LEGACY_DB = "banana.db"

ASPECTS = ["auto", "1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "4:5", "5:4", "21:9"]
RESOLUTIONS = ["1K", "2K", "4K"]
FORMATS = ["png", "jpg"]

# A clip speaks a different vocabulary from a still: 480p/720p/1080p, never the
# still's 1K/2K/4K. Sending one where the other belongs is silently wrong.
VIDEO_RESOLUTIONS = ["480p", "720p", "1080p"]
VIDEO_DURATIONS = ["4", "6", "8", "10"]

DEFAULTS = {
    "kie_api_key": "",
    # What a brand-new session starts with. The picker on the New session dialog
    # remembers the last choice by writing back here.
    "image_provider": "kie",
    "video_provider": "kie",
    "image_model": "nano-banana-2",
    "aspect_ratio": "9:16",
    "resolution": "1K",
    "output_format": "jpg",
    "video_model": "gemini-omni-video",
    "video_duration": "8",
    "video_resolution": "1080p",
    "video_aspect": "9:16",
}

_local = threading.local()
_write_lock = threading.Lock()


def _paths(root=None):
    """Point every path at `root`. Only the selftest calls this."""
    global ROOT, SYSTEM, DATA_DIR, OUT_DIR, REF_DIR, CHAR_DIR, DB_PATH
    if root:
        ROOT = SYSTEM = root
    DATA_DIR = os.path.join(ROOT, "data")
    OUT_DIR = os.path.join(ROOT, "out")
    REF_DIR = os.path.join(ROOT, "refs")
    CHAR_DIR = os.path.join(ROOT, "characters")
    DB_PATH = os.path.join(DATA_DIR, "cherry.db")


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        old = os.path.join(DATA_DIR, LEGACY_DB)
        if not os.path.exists(DB_PATH) and os.path.exists(old):
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(old + suffix):
                    os.replace(old + suffix, DB_PATH + suffix)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def reset_connection():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    title         TEXT,
    prompt        TEXT DEFAULT '',
    model         TEXT,
    aspect_ratio  TEXT,
    resolution    TEXT,
    output_format TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_session ON cards(session_id, seq);

CREATE TABLE IF NOT EXISTS refs (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    name        TEXT,
    kind        TEXT,
    source_path TEXT,
    upload_url  TEXT,
    uploaded_at TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_session ON refs(session_id);

CREATE TABLE IF NOT EXISTS card_refs (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    ref_id  INTEGER NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    pos     INTEGER DEFAULT 0,
    PRIMARY KEY (card_id, ref_id)
);

-- Extra references for the clip, on top of the image it animates. Kept apart
-- from card_refs because the image stage and the video stage want different
-- things attached.
CREATE TABLE IF NOT EXISTS card_video_refs (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    ref_id  INTEGER NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    pos     INTEGER DEFAULT 0,
    PRIMARY KEY (card_id, ref_id)
);

CREATE TABLE IF NOT EXISTS generations (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    n           INTEGER,
    model       TEXT,
    prompt      TEXT,
    params      TEXT,
    fingerprint TEXT,
    kie_task_id TEXT,
    kie_state   TEXT DEFAULT 'pending',
    result_url  TEXT,
    local_path  TEXT,
    fail_msg    TEXT,
    credits     REAL,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gen_card ON generations(card_id, n);
CREATE INDEX IF NOT EXISTS idx_gen_state ON generations(kie_state);

CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY,
    ts      TEXT,
    level   TEXT,
    message TEXT
);
"""

INFLIGHT = ("pending", "waiting", "queuing", "generating")

# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add
# them to a database that already exists, so they are ALTERed in on every start.
MIGRATIONS = [
    ("cards", "video_prompt", "TEXT DEFAULT ''"),
    ("cards", "video_model", "TEXT"),
    ("cards", "video_duration", "TEXT"),
    ("cards", "video_resolution", "TEXT"),
    ("cards", "video_aspect", "TEXT"),
    ("cards", "active_image_id", "INTEGER"),
    ("cards", "base_image_ref_id", "INTEGER"),
    ("generations", "kind", "TEXT DEFAULT 'image'"),
    ("generations", "source_gen_id", "INTEGER"),
    ("generations", "upload_url", "TEXT"),
    ("generations", "uploaded_at", "TEXT"),
    # Which service a session uses by default, and the optional per-card
    # override. A card with no provider of its own follows its session.
    ("sessions", "image_provider", "TEXT"),
    ("sessions", "image_model", "TEXT"),
    ("sessions", "video_provider", "TEXT"),
    ("sessions", "video_model", "TEXT"),
    ("cards", "provider", "TEXT"),
    ("cards", "video_provider", "TEXT"),
    # The poller must know which service to ask about a task, and some services
    # (fal.ai) hand back status links that have to be remembered.
    ("generations", "provider", "TEXT"),
    ("generations", "task_meta", "TEXT"),
    # Clips: a card is a "shot" (image prompt + its clip) or a standalone
    # "clip" with no image stage. A clip runs in a mode (frames / refs / text)
    # and takes its first and last frame from a source - see frame_source().
    ("cards", "kind", "TEXT DEFAULT 'shot'"),
    ("cards", "video_mode", "TEXT"),
    ("cards", "first_src", "TEXT"),
    ("cards", "last_src", "TEXT"),
    # A reference can be a picture, a video or a sound.
    ("refs", "media", "TEXT DEFAULT 'image'"),
]


def _migrate(c):
    for table, column, decl in MIGRATIONS:
        have = {r["name"] for r in c.execute("PRAGMA table_info(" + table + ")")}
        if column not in have:
            c.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + decl)


def init():
    for d in (DATA_DIR, OUT_DIR, REF_DIR, CHAR_DIR):
        os.makedirs(d, exist_ok=True)
    c = conn()
    c.executescript(SCHEMA)
    _migrate(c)
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Cards predating the auto-naming rule have no title, which would leave them
    # nameless in the gallery and in a downloaded filename. Name them once.
    c.execute("UPDATE cards SET title='Image ' || seq "
              "WHERE title IS NULL OR TRIM(title)=''")
    c.commit()
    relink_moved_files()


def _relocate(path):
    """Where a stored file is now, if the folder was moved or renamed.

    Paths are stored absolute, so unzipping on another PC, renaming the folder
    or the system/ reshuffle would strand every reference and take. The part of
    the path after a known folder name is kept and re-rooted here."""
    if not path or os.path.exists(path):
        return None
    p = path.replace("\\", "/")
    for marker, base in (("/Output/", OUT_DIR), ("/out/", OUT_DIR),
                         ("/characters/", CHAR_DIR), ("/refs/", REF_DIR)):
        i = p.rfind(marker)
        if i >= 0:
            cand = os.path.join(base, *p[i + len(marker):].split("/"))
            if os.path.exists(cand):
                return cand
    return None


def relink_moved_files():
    c = conn()
    moved = 0
    for table, col in (("refs", "source_path"), ("generations", "local_path")):
        for r in c.execute("SELECT id, " + col + " p FROM " + table +
                           " WHERE " + col + " IS NOT NULL").fetchall():
            new = _relocate(r["p"])
            if new:
                c.execute("UPDATE " + table + " SET " + col + "=? WHERE id=?", (new, r["id"]))
                moved += 1
    if moved:
        c.commit()
        log("info", "relinked " + str(moved) + " file(s) after the folder moved")


# ------------------------------------------------------------ settings

def get_setting(key, fallback=None):
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return DEFAULTS.get(key, fallback) if fallback is None else fallback
    return row["value"]


def set_setting(key, value):
    with _write_lock:
        c = conn()
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, "" if value is None else str(value)))
        c.commit()


def all_settings():
    rows = conn().execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def defaults():
    return {
        "model": get_setting("image_model") or DEFAULTS["image_model"],
        "aspect_ratio": get_setting("aspect_ratio") or DEFAULTS["aspect_ratio"],
        "resolution": get_setting("resolution") or DEFAULTS["resolution"],
        "output_format": get_setting("output_format") or DEFAULTS["output_format"],
    }


def video_defaults():
    return {
        "model": get_setting("video_model") or DEFAULTS["video_model"],
        "duration": get_setting("video_duration") or DEFAULTS["video_duration"],
        "resolution": get_setting("video_resolution") or DEFAULTS["video_resolution"],
        "aspect_ratio": get_setting("video_aspect") or DEFAULTS["video_aspect"],
    }


def log(level, message):
    with _write_lock:
        c = conn()
        c.execute("INSERT INTO log(ts,level,message) VALUES(?,?,?)",
                  (now(), level, str(message)[:2000]))
        c.commit()


def recent_log(limit=80):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ------------------------------------------------------------ sessions

def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "session"


def create_session(name, engines=None):
    """`engines` is {image_provider, image_model, video_provider, video_model};
    anything missing falls back to the last choice stored in settings."""
    name = (name or "").strip() or "Untitled session"
    eng = engines or {}
    pick = lambda k: (eng.get(k) or get_setting(k) or "").strip()   # noqa: E731
    image_provider, video_provider = pick("image_provider"), pick("video_provider")
    image_model, video_model = pick("image_model"), pick("video_model")
    with _write_lock:
        c = conn()
        base = slugify(name)
        slug, i = base, 2
        while c.execute("SELECT 1 FROM sessions WHERE slug=?", (slug,)).fetchone():
            slug = base + "-" + str(i)
            i += 1
        cur = c.execute(
            "INSERT INTO sessions(name,slug,created_at,updated_at,image_provider,"
            "image_model,video_provider,video_model) VALUES(?,?,?,?,?,?,?,?)",
            (name, slug, now(), now(), image_provider, image_model,
             video_provider, video_model))
        c.commit()
        sid = cur.lastrowid
    # Remember it: the next New session dialog opens on what you used last.
    for k, v in (("image_provider", image_provider), ("image_model", image_model),
                 ("video_provider", video_provider), ("video_model", video_model)):
        if v:
            set_setting(k, v)
    return sid


def touch_session(session_id):
    with _write_lock:
        c = conn()
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))
        c.commit()


def rename_session(session_id, name):
    name = (name or "").strip()
    if not name:
        return
    with _write_lock:
        c = conn()
        c.execute("UPDATE sessions SET name=?, updated_at=? WHERE id=?",
                  (name, now(), session_id))
        c.commit()


def delete_session(session_id):
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        c.commit()


def get_session(session_id):
    r = conn().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def list_sessions():
    rows = conn().execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC").fetchall()
    out = []
    for r in rows:
        s = dict(r)
        s["card_count"] = conn().execute(
            "SELECT COUNT(*) n FROM cards WHERE session_id=?",
            (s["id"],)).fetchone()["n"]
        s["image_count"] = conn().execute(
            "SELECT COUNT(*) n FROM generations g JOIN cards c ON c.id=g.card_id "
            "WHERE c.session_id=? AND g.kie_state='success' AND g.kind='image'",
            (s["id"],)).fetchone()["n"]
        s["video_count"] = conn().execute(
            "SELECT COUNT(*) n FROM generations g JOIN cards c ON c.id=g.card_id "
            "WHERE c.session_id=? AND g.kie_state='success' AND g.kind='video'",
            (s["id"],)).fetchone()["n"]
        s["busy"] = conn().execute(
            "SELECT COUNT(*) n FROM generations g JOIN cards c ON c.id=g.card_id "
            "WHERE c.session_id=? AND g.kie_state IN "
            "('pending','waiting','queuing','generating')",
            (s["id"],)).fetchone()["n"]
        # An image, explicitly: the cover goes into an <img>, and a clip's mp4
        # would render as a broken-image glyph.
        cover = conn().execute(
            "SELECT g.local_path FROM generations g JOIN cards c ON c.id=g.card_id "
            "WHERE c.session_id=? AND g.kie_state='success' AND g.kind='image' "
            "AND g.local_path IS NOT NULL ORDER BY g.id DESC LIMIT 1",
            (s["id"],)).fetchone()
        s["cover"] = rel_path(cover["local_path"]) if cover else None
        out.append(s)
    return out


def rel_path(path):
    """Paths leave the server relative to ROOT so the browser can ask for them
    back through /file without ever naming an absolute location."""
    if not path:
        return None
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return None


# ------------------------------------------------------------ engines

def session_engine(session, kind):
    """(provider, model) a session uses by default for stills or clips."""
    p = (session or {}).get(kind + "_provider") or get_setting(kind + "_provider") or "kie"
    m = (session or {}).get(kind + "_model") or ""
    if not m or not (session or {}).get(kind + "_provider"):
        # A session made before providers existed, or one with half a pair:
        # fall back to the stored default for that provider, never mix pairs.
        m = (get_setting(kind + "_model") if p == get_setting(kind + "_provider")
             else "") or ""
    return p, m


def effective_engine(card, kind="image", session=None):
    """The engine a card will really use.

    A card carries its own (provider, model) pair only when you overrode it; with
    no provider of its own it follows the session, and whatever model string it
    holds is ignored - a model id means nothing without the provider it belongs
    to, so the two only ever travel together."""
    own = card.get("provider" if kind == "image" else "video_provider")
    model_key = "model" if kind == "image" else "video_model"
    if own and card.get(model_key):
        return own, card[model_key]
    if session is None:
        session = get_session(card["session_id"]) if card.get("session_id") else None
    return session_engine(session, kind)


def set_session_engine(session_id, kind, provider, model, clear_cards=True):
    """Change a session's default engine. By default every card's own override is
    cleared too, so 'All N' means all N."""
    col = "provider" if kind == "image" else "video_provider"
    mcol = "model" if kind == "image" else "video_model"
    with _write_lock:
        c = conn()
        c.execute("UPDATE sessions SET " + kind + "_provider=?, " + kind +
                  "_model=?, updated_at=? WHERE id=?", (provider, model, now(), session_id))
        if clear_cards:
            c.execute("UPDATE cards SET " + col + "=NULL, " + mcol + "=NULL, "
                      "updated_at=? WHERE session_id=?", (now(), session_id))
        c.commit()
    for k, v in ((kind + "_provider", provider), (kind + "_model", model)):
        set_setting(k, v)


# ------------------------------------------------------------ cards

def next_seq(session_id):
    r = conn().execute("SELECT COALESCE(MAX(seq),0) m FROM cards WHERE session_id=?",
                       (session_id,)).fetchone()
    return r["m"] + 1


def session_defaults(session_id):
    """What a new card starts with: whatever the last card in this session uses,
    falling back to the stored defaults for the first one. Settings no longer
    has these fields - they belong to the Prompts tab - so a session carries its
    own look forward rather than reaching for a global preference."""
    d = defaults()
    row = conn().execute(
        "SELECT model, aspect_ratio, resolution, output_format FROM cards "
        "WHERE session_id=? AND COALESCE(kind,'shot')='shot' ORDER BY seq DESC, id DESC LIMIT 1",
        (session_id,)).fetchone()
    if not row:
        return d
    return {k: (row[k] or d[k]) for k in d}


def create_card(session_id, prompt="", title=None, model=None, aspect_ratio=None,
                resolution=None, output_format=None, kind="shot", after=None,
                video_prompt=None):
    """A shot (image prompt + its clip) or, with kind="clip", a standalone clip.

    One running order covers both: `seq` is the place in the final edit, so a
    standalone clip can sit between shot 3 and shot 4. `after` inserts right
    behind that card instead of at the end."""
    d = session_defaults(session_id)
    kind = "clip" if kind == "clip" else "shot"
    with _write_lock:
        c = conn()
        prev = c.execute("SELECT seq FROM cards WHERE id=? AND session_id=?",
                         (after, session_id)).fetchone() if after else None
        if prev:
            seq = prev["seq"] + 1
            c.execute("UPDATE cards SET seq=seq+1 WHERE session_id=? AND seq>=?",
                      (session_id, seq))
        else:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 s FROM cards WHERE session_id=?",
                            (session_id,)).fetchone()["s"]
        # Cards are always named. An imported payload's own title wins; anything
        # else gets "Image N" / "Clip N", so a card is never nameless.
        title = (title or "").strip() or (("Clip " if kind == "clip" else "Image ") + str(seq))
        cur = c.execute(
            "INSERT INTO cards(session_id,seq,title,prompt,model,aspect_ratio,"
            "resolution,output_format,created_at,updated_at,kind,video_prompt) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, seq, title, prompt or "",
             model or d["model"], aspect_ratio or d["aspect_ratio"],
             resolution or d["resolution"], output_format or d["output_format"],
             now(), now(), kind, video_prompt or ""))
        c.commit()
        card_id = cur.lastrowid
    touch_session(session_id)
    return card_id


def get_card(card_id):
    r = conn().execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    return dict(r) if r else None


CARD_FIELDS = ("title", "prompt", "model", "aspect_ratio", "resolution", "output_format",
               "video_prompt", "video_model", "video_duration", "video_resolution",
               "video_aspect", "provider", "video_provider", "video_mode", "first_src",
               "last_src")


def update_card(card_id, **fields):
    sets, vals = [], []
    for k in CARD_FIELDS:
        if k in fields:
            sets.append(k + "=?")
            vals.append(fields[k])
    if not sets:
        return
    vals += [now(), card_id]
    with _write_lock:
        c = conn()
        c.execute("UPDATE cards SET " + ", ".join(sets) + ", updated_at=? WHERE id=?", vals)
        c.commit()
    card = get_card(card_id)
    if card:
        touch_session(card["session_id"])


def delete_card(card_id):
    card = get_card(card_id)
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM cards WHERE id=?", (card_id,))
        c.commit()
    if card:
        resequence(card["session_id"])


def duplicate_card(card_id):
    card = get_card(card_id)
    if not card:
        return None
    title = (card["title"] + " copy") if card["title"] else None
    clip = card.get("kind") == "clip"
    new_id = create_card(card["session_id"], prompt=card["prompt"], title=title,
                         model=card["model"], aspect_ratio=card["aspect_ratio"],
                         resolution=card["resolution"],
                         output_format=card["output_format"],
                         kind=card.get("kind") or "shot", after=card_id if clip else None,
                         video_prompt=card.get("video_prompt"))
    set_card_refs(new_id, [r["id"] for r in card_refs(card_id)])
    set_card_video_refs(new_id, [r["id"] for r in card_video_refs(card_id)])
    fields = {k: card.get(k) for k in ("provider", "video_provider", "video_model",
                                        "video_duration", "video_resolution", "video_aspect",
                                        "video_mode", "last_src")}
    # A copied shot's "this shot's image" means the copy's own image.
    fields["first_src"] = card.get("first_src")
    update_card(new_id, **fields)
    return new_id


def move_card(card_id, direction, scope="all"):
    """Swap this card with its neighbour. Sequence numbers are the only order
    the UI knows about, so they are kept dense and 1-based. On the Prompts tab
    (scope="shots") the neighbour is the next shot, skipping standalone clips."""
    card = get_card(card_id)
    if not card:
        return
    op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
    only = " AND COALESCE(kind,'shot')='shot'" if scope == "shots" else ""
    nb = conn().execute(
        "SELECT * FROM cards WHERE session_id=? AND seq " + op +
        " ?" + only + " ORDER BY seq " + order + " LIMIT 1",
        (card["session_id"], card["seq"])).fetchone()
    if not nb:
        return
    with _write_lock:
        c = conn()
        c.execute("UPDATE cards SET seq=? WHERE id=?", (nb["seq"], card["id"]))
        c.execute("UPDATE cards SET seq=? WHERE id=?", (card["seq"], nb["id"]))
        c.commit()
    touch_session(card["session_id"])


def resequence(session_id):
    rows = conn().execute("SELECT id FROM cards WHERE session_id=? ORDER BY seq, id",
                          (session_id,)).fetchall()
    with _write_lock:
        c = conn()
        for i, r in enumerate(rows, 1):
            c.execute("UPDATE cards SET seq=? WHERE id=?", (i, r["id"]))
        c.commit()


def get_cards(session_id):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM cards WHERE session_id=? ORDER BY seq, id",
        (session_id,)).fetchall()]


def apply_to_all(session_id, **fields):
    sets, vals = [], []
    for k in ("model", "aspect_ratio", "resolution", "output_format"):
        if fields.get(k):
            sets.append(k + "=?")
            vals.append(fields[k])
    if not sets:
        return 0
    vals += [now(), session_id]
    with _write_lock:
        c = conn()
        cur = c.execute(
            "UPDATE cards SET " + ", ".join(sets) + ", updated_at=? WHERE session_id=?",
            vals)
        c.commit()
        return cur.rowcount


# ------------------------------------------------------------ references

MEDIA_EXT = {".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video",
             ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio", ".ogg": "audio"}


def media_of(path_or_url):
    """What a reference is, by its extension: image unless it says otherwise."""
    p = (path_or_url or "").split("?")[0]
    ext = (p if p.startswith(".") and p.count(".") == 1 else os.path.splitext(p)[1]).lower()
    return MEDIA_EXT.get(ext, "image")


def create_ref(session_id, name, kind, source_path=None, upload_url=None, media=None):
    media = media or media_of(source_path or upload_url)
    with _write_lock:
        c = conn()
        cur = c.execute(
            "INSERT INTO refs(session_id,name,kind,source_path,upload_url,"
            "uploaded_at,created_at,media) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, name, kind, source_path, upload_url,
             now() if upload_url else None, now(), media))
        c.commit()
        return cur.lastrowid


def get_ref(ref_id):
    r = conn().execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
    return dict(r) if r else None


def _ref_dict(row):
    d = dict(row)
    d["thumb"] = rel_path(d["source_path"]) if d["source_path"] else d["upload_url"]
    return d


def list_refs(session_id):
    """A session's own references, plus the whole cast.

    Characters are stored with no session_id, which makes them global: you
    upload a face once and it is there in every video you make afterwards, and
    deleting a session cannot cascade it away. They sort first."""
    rows = conn().execute(
        "SELECT * FROM refs WHERE session_id=? "
        "OR (session_id IS NULL AND kind='character') "
        "ORDER BY (kind='character') DESC, id", (session_id,)).fetchall()
    return [_ref_dict(r) for r in rows]


def list_characters():
    return [_ref_dict(r) for r in conn().execute(
        "SELECT * FROM refs WHERE kind='character' ORDER BY id").fetchall()]


def set_ref_upload(ref_id, url):
    with _write_lock:
        c = conn()
        c.execute("UPDATE refs SET upload_url=?, uploaded_at=? WHERE id=?",
                  (url, now(), ref_id))
        c.commit()


def delete_ref(ref_id):
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM refs WHERE id=?", (ref_id,))
        c.commit()


def upload_is_stale(ref, days=2.5):
    """kie.ai drops uploaded files after three days. Anything older than two and
    a half gets pushed again before it is used."""
    if not ref.get("upload_url") or not ref.get("uploaded_at"):
        return True
    try:
        t = time.mktime(time.strptime(ref["uploaded_at"], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return True
    return (time.time() - t) > days * 86400


def card_refs(card_id):
    rows = conn().execute(
        "SELECT r.*, cr.pos FROM card_refs cr JOIN refs r ON r.id=cr.ref_id "
        "WHERE cr.card_id=? ORDER BY cr.pos, r.id", (card_id,)).fetchall()
    return [_ref_dict(r) for r in rows]


def set_card_refs(card_id, ref_ids):
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM card_refs WHERE card_id=?", (card_id,))
        for pos, rid in enumerate(ref_ids or []):
            c.execute("INSERT OR IGNORE INTO card_refs(card_id,ref_id,pos) VALUES(?,?,?)",
                      (card_id, rid, pos))
        c.commit()


# ------------------------------------------------------------ fingerprint

def fingerprint(card, ref_urls):
    blob = json.dumps({
        "p": card["prompt"] or "",
        "m": "/".join(effective_engine(card, "image")),
        "a": card["aspect_ratio"] or "",
        "r": card["resolution"] or "",
        "f": card["output_format"] or "",
        "i": list(ref_urls or []),
    }, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ref_identity(refs):
    """Fingerprint references by what they *are*, not by the url they currently
    hold - a re-upload mints a fresh url for the same picture and must not make
    every card look edited."""
    out = []
    for r in refs:
        out.append(r.get("source_path") or r.get("upload_url") or str(r.get("id")))
    return out


def card_fingerprint(card_id):
    card = get_card(card_id)
    if not card:
        return None
    return fingerprint(card, ref_identity(card_refs(card_id)))


# ------------------------------------------------------------ generations

def begin_generation(card_id, kind, model, prompt, params, fp, source_gen_id=None,
                     provider="kie"):
    """Claim a card for one generation, or refuse.

    This is the single guard against paying twice. The busy check and the insert
    both happen while `_write_lock` is held, so two threads racing on a
    double-clicked button cannot both see "not busy" - the loser gets None and
    dispatches nothing. Every path that spends money goes through here.

    Returns the new generation id, or None if this card already has one of this
    kind in flight.
    """
    with _write_lock:
        c = conn()
        busy = c.execute(
            "SELECT 1 FROM generations WHERE card_id=? AND kind=? AND kie_state IN "
            "('pending','waiting','queuing','generating') LIMIT 1",
            (card_id, kind)).fetchone()
        if busy:
            return None
        n = c.execute(
            "SELECT COALESCE(MAX(n),0)+1 n FROM generations WHERE card_id=? AND kind=?",
            (card_id, kind)).fetchone()["n"]
        cur = c.execute(
            "INSERT INTO generations(card_id,kind,n,model,prompt,params,fingerprint,"
            "source_gen_id,kie_state,created_at,updated_at,provider) "
            "VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (card_id, kind, n, model, prompt, json.dumps(params), fp,
             source_gen_id, now(), now(), provider))
        c.commit()
        return cur.lastrowid


def set_generation_payload(gen_id, params):
    """Record the exact body sent to kie.ai. The card shows what it *asked* for;
    this is what was actually sent, which is the thing worth auditing."""
    with _write_lock:
        c = conn()
        c.execute("UPDATE generations SET params=?, prompt=?, updated_at=? WHERE id=?",
                  (json.dumps(params), params.get("prompt", ""), now(), gen_id))
        c.commit()


def attach_task(gen_id, task_id, meta=None):
    with _write_lock:
        c = conn()
        c.execute("UPDATE generations SET kie_task_id=?, kie_state='waiting', "
                  "task_meta=?, updated_at=? WHERE id=?",
                  (task_id, json.dumps(meta) if meta else None, now(), gen_id))
        c.commit()


def update_generation(gen_id, state, result_url=None, local_path=None,
                      fail_msg=None, credits=None):
    with _write_lock:
        c = conn()
        c.execute(
            "UPDATE generations SET kie_state=?, result_url=COALESCE(?,result_url), "
            "local_path=COALESCE(?,local_path), fail_msg=COALESCE(?,fail_msg), "
            "credits=COALESCE(?,credits), updated_at=? WHERE id=?",
            (state, result_url, local_path, fail_msg, credits, now(), gen_id))
        c.commit()


def get_generation(gen_id):
    r = conn().execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    return dict(r) if r else None


def delete_generation(gen_id):
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM generations WHERE id=?", (gen_id,))
        c.commit()


def card_generations(card_id, kind="image"):
    out = []
    for r in conn().execute(
            "SELECT * FROM generations WHERE card_id=? AND kind=? ORDER BY n DESC",
            (card_id, kind)).fetchall():
        d = dict(r)
        d["file"] = rel_path(d["local_path"])
        out.append(d)
    return out


def set_active_image(card_id, gen_id):
    with _write_lock:
        c = conn()
        c.execute("UPDATE cards SET active_image_id=?, updated_at=? WHERE id=?",
                  (gen_id, now(), card_id))
        c.commit()


def active_image(card_id, card=None, gens=None):
    """The image a clip will be made from.

    The pinned take when one is pinned and still around, otherwise the newest
    successful one - so regenerating a shot promotes the new image by default,
    and clicking an older version pins it instead."""
    card = card or get_card(card_id)
    if not card:
        return None
    ok = [g for g in (gens if gens is not None else card_generations(card_id, "image"))
          if g["kie_state"] == "success"]
    if card.get("active_image_id"):
        for g in ok:
            if g["id"] == card["active_image_id"]:
                return g
    return ok[0] if ok else None


def set_base_image(card_id, ref_id):
    """Pin an uploaded picture as the shot's own image.

    Not every shot has to be generated: sometimes you already have the frame and
    only want it animated. This is what the empty slot on the Images tab fills
    in, and a generated take still takes precedence when there is one."""
    with _write_lock:
        c = conn()
        c.execute("UPDATE cards SET base_image_ref_id=?, updated_at=? WHERE id=?",
                  (ref_id, now(), card_id))
        c.commit()


def base_image(card):
    """The uploaded stand-in for a shot that has not been generated."""
    rid = card.get("base_image_ref_id")
    if not rid:
        return None
    ref = get_ref(rid)
    return _ref_dict(ref) if ref else None


def set_generation_upload(gen_id, url):
    with _write_lock:
        c = conn()
        c.execute("UPDATE generations SET upload_url=?, uploaded_at=? WHERE id=?",
                  (url, now(), gen_id))
        c.commit()


def inflight():
    """Every generation the poller still has to chase, with the session slug and
    card sequence it needs to name the downloaded file."""
    q = ("SELECT g.*, c.seq, c.title, c.session_id, s.slug FROM generations g "
         "JOIN cards c ON c.id=g.card_id JOIN sessions s ON s.id=c.session_id "
         "WHERE g.kie_task_id IS NOT NULL AND g.kie_state IN "
         "('pending','waiting','queuing','generating')")
    return [dict(r) for r in conn().execute(q).fetchall()]


def generation_job(gen_id):
    """One generation with the slug and shot number it is filed under."""
    q = ("SELECT g.*, c.seq, c.title, c.session_id, s.slug FROM generations g "
         "JOIN cards c ON c.id=g.card_id JOIN sessions s ON s.id=c.session_id "
         "WHERE g.id=?")
    r = conn().execute(q, (gen_id,)).fetchone()
    return dict(r) if r else None


def expire_orphans(minutes=15):
    """A generation claimed but never handed a task - the app was closed between
    the claim and the send, or a thread died - would keep its card 'busy'
    forever. Past a generous deadline it is failed so the card unlocks."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - minutes * 60))
    with _write_lock:
        c = conn()
        c.execute("UPDATE generations SET kie_state='fail', fail_msg="
                  "'interrupted before it finished - press Generate again', "
                  "updated_at=? WHERE kie_state='pending' AND kie_task_id IS NULL "
                  "AND created_at < ?", (now(), cutoff))
        c.commit()


def session_busy(session_id):
    return conn().execute(
        "SELECT COUNT(*) n FROM generations g JOIN cards c ON c.id=g.card_id "
        "WHERE c.session_id=? AND g.kie_state IN "
        "('pending','waiting','queuing','generating')", (session_id,)).fetchone()["n"]


def card_video_refs(card_id):
    rows = conn().execute(
        "SELECT r.*, cr.pos FROM card_video_refs cr JOIN refs r ON r.id=cr.ref_id "
        "WHERE cr.card_id=? ORDER BY cr.pos, r.id", (card_id,)).fetchall()
    return [_ref_dict(r) for r in rows]


def set_card_video_refs(card_id, ref_ids):
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM card_video_refs WHERE card_id=?", (card_id,))
        for pos, rid in enumerate(ref_ids or []):
            c.execute("INSERT OR IGNORE INTO card_video_refs(card_id,ref_id,pos) "
                      "VALUES(?,?,?)", (card_id, rid, pos))
        c.commit()


# ------------------------------------------------------------ clip inputs

def default_src(card, which):
    if which == "first" and (card.get("kind") or "shot") == "shot":
        return "self"
    return "none"


def frame_source(card, which, gens=None):
    """Where a clip's first or last frame comes from, resolved.

    Source strings: "self" (this shot's active image, falling back to an image
    attached to the shot), "card:<id>" (another shot's active image - live, so
    regenerating that shot updates it), "ref:<id>" (anything in the library: an
    upload, a link, a character, a clip's last frame), "none". Returns None for
    no frame, or {src, label, thumb, gen|ref, missing}."""
    src = card.get(which + "_src") or default_src(card, which)
    if src == "none":
        return None
    out = {"src": src, "missing": False}
    if src == "self":
        gen = active_image(card["id"], card, gens)
        if gen:
            return dict(out, gen=gen, thumb=rel_path(gen["local_path"]),
                        label="This shot's image (take " + str(gen["n"]) + ")")
        base = base_image(card)
        if base:
            return dict(out, ref=base, thumb=base["thumb"], label="Image attached to this shot")
        return dict(out, missing=True, label="This shot has no image yet")
    if src.startswith("card:"):
        other = get_card(_int(src[5:]))
        if not other:
            return dict(out, missing=True, label="That shot was deleted")
        gen = active_image(other["id"], other)
        name = other["title"] or ("Shot " + str(other["seq"]))
        if not gen:
            return dict(out, missing=True, label=name + " has no image yet")
        return dict(out, gen=gen, thumb=rel_path(gen["local_path"]),
                    label=str(other["seq"]) + ". " + name + " (take " + str(gen["n"]) + ")")
    if src.startswith("ref:"):
        ref = get_ref(_int(src[4:]))
        if not ref:
            return dict(out, missing=True, label="That picture was deleted")
        r = _ref_dict(ref)
        return dict(out, ref=r, thumb=r["thumb"], label=r["name"])
    return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def frame_identity(f):
    if not f or f.get("missing"):
        return None
    if f.get("gen"):
        return "gen:" + str(f["gen"]["id"])
    r = f.get("ref") or {}
    return r.get("source_path") or r.get("upload_url") or "ref:" + str(r.get("id"))


def refs_by_media(vrefs):
    out = {"images": [], "videos": [], "audio": []}
    for r in vrefs:
        out[{"video": "videos", "audio": "audio"}.get(r.get("media") or "image", "images")].append(r)
    return out


def clip_state(card, session=None, gens=None, vrefs=None):
    """Everything the Clips board and the dispatcher agree on: which mode the
    clip really runs in, its resolved frames and references, whether it is
    ready, and how many attached inputs this model will not use."""
    import providers
    vp, vm = effective_engine(card, "video", session)
    spec = providers.model_spec(vp, "video", vm or providers.default_model(vp, "video"))
    modes = providers.video_modes(spec)
    first = frame_source(card, "first", gens)
    last = frame_source(card, "last", gens) if card.get("last_src") else None
    vrefs = card_video_refs(card["id"]) if vrefs is None else vrefs
    media = refs_by_media(vrefs)
    n_refs = sum(len(v) for v in media.values())
    explicit_first = bool(card.get("first_src")) and card["first_src"] not in ("none", "self")
    mode = providers.pick_mode(spec, card.get("video_mode"), first is not None, n_refs > 0)
    # In reference mode the clip's own picture - a shot's generated image, or a
    # chosen first frame - goes in as the first reference, so a shot's image
    # is attached the moment it exists. Clearing the first frame drops it.
    first_as_ref = mode == "refs" and first is not None
    run_mode = mode
    v = providers.mode_variant(spec, mode, bool(last and not last["missing"])) if mode else None
    problem, hidden = None, 0
    if not mode:
        problem = "This model cannot make clips"
    elif mode == "frames":
        if not first:
            problem = "Choose a first frame, or switch to text only"
        elif first["missing"]:
            problem = first["label"]
        if last and not (v or {}).get("last"):
            hidden += 1
        elif last and last["missing"] and not problem:
            problem = last["label"]
    elif mode == "refs":
        if first_as_ref and first["missing"]:
            problem = first["label"]
        elif not n_refs and not first_as_ref:
            # Nothing to refer to yet: it runs from the prompt alone.
            if "text" in modes:
                run_mode = "text"
            else:
                problem = "Add at least one reference"
        for kind, items in media.items():
            n = len(items) + (1 if first_as_ref and kind == "images" else 0)
            if not n:
                continue
            cap = (v or {}).get(kind)
            if not cap:
                hidden += len(items)
            elif n > cap["max"] and not problem:
                problem = ("Too many reference " + kind + " (" + str(n) + " of "
                           + str(cap["max"]) + ")")
    if mode != "refs":
        hidden += n_refs
    if mode == "text":
        hidden += int(explicit_first) + int(bool(last))
    elif mode == "refs":
        hidden += int(bool(last))
    if run_mode != mode:
        v = providers.mode_variant(spec, run_mode)
    return {"spec": spec, "mode": mode, "run_mode": run_mode, "modes": list(modes),
            "first": first, "last": last, "media": media, "problem": problem,
            "hidden": hidden, "variant": v, "first_as_ref": first_as_ref}


def video_fingerprint(card, image_gen, refs, base=None):
    blob = json.dumps({
        "p": card["video_prompt"] or "",
        "m": "/".join(effective_engine(card, "video")),
        "d": str(card["video_duration"] or ""),
        "r": card["video_resolution"] or "",
        "a": card["video_aspect"] or "",
        "src": image_gen["id"] if image_gen else None,
        "b": (base.get("source_path") or base.get("upload_url")) if base else None,
        "i": ref_identity(refs),
    }, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _public_frame(f):
    if not f:
        return None
    return {"src": f["src"], "label": f["label"], "thumb": f.get("thumb"),
            "missing": f["missing"], "gen_id": (f.get("gen") or {}).get("id"),
            "ref_id": (f.get("ref") or {}).get("id")}


def clip_fingerprint(card, cs):
    """Prompt + engine + settings + mode + exactly which frames and references,
    so any change flips the clip to Generate instead of Generate again."""
    blob = json.dumps({
        "p": card.get("video_prompt") or "",
        "m": "/".join(effective_engine(card, "video")),
        "d": str(card.get("video_duration") or ""),
        "r": card.get("video_resolution") or "",
        "a": card.get("video_aspect") or "",
        "mode": cs.get("run_mode", cs["mode"]),
        "f": frame_identity(cs["first"]) if cs["mode"] == "frames" else None,
        "l": frame_identity(cs["last"]) if cs["mode"] == "frames" else None,
        "i": {k: ref_identity(v) for k, v in cs["media"].items()} if cs.get("run_mode") == "refs" else None,
        "fr": frame_identity(cs["first"]) if cs.get("first_as_ref") else None,
    }, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------ composed view

def session_view(session_id):
    s = get_session(session_id)
    if not s:
        return None
    d = defaults()
    vd = video_defaults()
    cards = []
    for card in get_cards(session_id):
        refs = card_refs(card["id"])
        gens = card_generations(card["id"], "image")
        card["refs"] = refs
        card["generations"] = gens
        card["fingerprint"] = fingerprint(card, ref_identity(refs))
        latest = gens[0] if gens else None
        card["latest"] = latest
        # "Generate again" only when the newest take was made from exactly this
        # prompt and these settings; any edit flips the button back to "Generate".
        card["dirty"] = bool(latest) and latest["fingerprint"] != card["fingerprint"]
        card["busy"] = any(g["kie_state"] in INFLIGHT for g in gens)
        card["success_count"] = sum(1 for g in gens if g["kie_state"] == "success")

        # ---- video stage
        vrefs = card_video_refs(card["id"])
        vgens = card_generations(card["id"], "video")
        src = active_image(card["id"], card, gens)
        card["active"] = src
        card["base_image"] = base_image(card)
        card["video_refs"] = vrefs
        card["video_generations"] = vgens
        ip, im = effective_engine(card, "image", s)
        vp, vm = effective_engine(card, "video", s)
        card["engine"] = {"provider": ip, "model": im,
                          "own": bool(card.get("provider"))}
        card["video_engine"] = {"provider": vp, "model": vm,
                                "own": bool(card.get("video_provider"))}
        card["video_model"] = vm
        card["video_duration"] = str(card["video_duration"] or vd["duration"])
        card["video_resolution"] = card["video_resolution"] or vd["resolution"]
        card["video_aspect"] = card["video_aspect"] or vd["aspect_ratio"]
        card["kind"] = card.get("kind") or "shot"
        cs = clip_state(card, s, gens, vrefs)
        card["clip_mode"] = cs["mode"]
        card["clip_modes"] = cs["modes"]
        card["first"] = _public_frame(cs["first"])
        card["last"] = _public_frame(cs["last"])
        card["vref_media"] = cs["media"]
        card["clip_problem"] = cs["problem"]
        card["clip_hidden"] = cs["hidden"]
        card["first_as_ref"] = cs["first_as_ref"]
        card["clip_run_mode"] = cs["run_mode"]
        card["video_fingerprint"] = clip_fingerprint(card, cs)
        card["can_clip"] = cs["problem"] is None
        vlatest = vgens[0] if vgens else None
        card["video_latest"] = vlatest
        card["video_dirty"] = (bool(vlatest)
                               and vlatest["fingerprint"] != card["video_fingerprint"])
        card["video_busy"] = any(g["kie_state"] in INFLIGHT for g in vgens)
        card["video_count"] = sum(1 for g in vgens if g["kie_state"] == "success")
        cards.append(card)

    s["cards"] = cards
    s["image_engine"] = dict(zip(("provider", "model"), session_engine(s, "image")))
    s["video_engine"] = dict(zip(("provider", "model"), session_engine(s, "video")))
    s["refs"] = list_refs(session_id)
    s["busy"] = session_busy(session_id)
    s["defaults"] = d
    s["video_defaults"] = vd
    return s


# ------------------------------------------------------------ JSON import

def extract_objects(text):
    """Pull every payload out of a pasted blob, however mangled.

    Three passes, each more forgiving than the last, because kie.ai payloads get
    copied out of a docs code panel and arrive in every possible state of
    damage:

    1. json.loads, for a paste that is actually valid.
    2. A string-aware brace scan, for several payloads pasted back to back or
       buried in surrounding noise.
    3. Key/value salvage, for the common case where the selection missed the
       outer braces entirely - so nothing in the text is balanced and passes 1
       and 2 both come back empty-handed.

    The later passes only win if they recover more prompts than the earlier
    ones, so a clean paste is never re-interpreted by the loose parser.
    """
    text = (text or "").strip()
    if not text:
        return []
    strict = _strict_objects(text)
    if _count_prompts(strict):
        salvaged = _salvage_objects(text)
        return salvaged if _count_prompts(salvaged) > _count_prompts(strict) else strict
    return _salvage_objects(text) or strict


def _has_prompt(obj):
    for src in (obj, obj.get("input") if isinstance(obj.get("input"), dict) else {}):
        for k in ("prompt", "image_prompt", "text"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, list) and v:
                return True
    return False


def _count_prompts(objs):
    return sum(1 for o in objs if _has_prompt(o))


def _strict_objects(text):
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for key in ("cards", "prompts", "items", "shots", "data"):
                if isinstance(parsed.get(key), list):
                    return [o for o in parsed[key] if isinstance(o, dict)]
            return [parsed]
        if isinstance(parsed, list):
            return [o for o in parsed if isinstance(o, dict)]
        return []
    except (json.JSONDecodeError, ValueError):
        pass

    objects, depth, start = [], 0, None
    in_str, escape = False, False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        obj = None
                    if isinstance(obj, dict):
                        objects.append(obj)
                    start = None
    return objects


# --- pass 3: salvage ------------------------------------------------------
#
# When the paste has no balanced braces at all, structure is unrecoverable but
# the *pairs* are not. This walks the text collecting every `"key": value` it
# can read, ignoring nesting entirely, and starts a new payload each time a key
# repeats - which is exactly where one pasted payload ends and the next begins.
# The result is a flat object, and normalize_payload already reads that form.

_SKIP = object()
_WS = " \t\r\n"


def _read_string(text, i):
    """Read a JSON string starting at the opening quote. Returns (value, end)."""
    if i >= len(text) or text[i] != '"':
        return None, i
    out, i, n = [], i + 1, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 >= n:
                return None, i
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(nxt, nxt)
                       if nxt != "u" else "")
            if nxt == "u" and i + 5 < n:
                try:
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None, i                       # unterminated: not a usable string


def _balanced_end(text, i):
    """Index just past the container opening at i, or None if it never closes."""
    open_ch = text[i]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc, n = 0, False, False, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _read_value(text, i):
    n = len(text)
    if i >= n:
        return _SKIP, i
    ch = text[i]
    if ch == '"':
        val, end = _read_string(text, i)
        return (_SKIP, i) if val is None else (val, end)
    if ch in "{[":
        end = _balanced_end(text, i)
        if end is None:
            return _SKIP, i              # truncated: step inside and keep going
        try:
            return json.loads(text[i:end]), end
        except (json.JSONDecodeError, ValueError):
            return _SKIP, i
    j = i
    while j < n and text[j] not in ",}]\r\n":
        j += 1
    raw = text[i:j].strip()
    if not raw:
        return _SKIP, i + 1
    try:
        return json.loads(raw), j
    except (json.JSONDecodeError, ValueError):
        return raw, j


def _salvage_pairs(text):
    pairs, i, n = [], 0, len(text)
    while i < n:
        if text[i] != '"':
            i += 1
            continue
        key, j = _read_string(text, i)
        if key is None:
            i += 1
            continue
        k = j
        while k < n and text[k] in _WS:
            k += 1
        if k >= n or text[k] != ":":
            i = j                        # a bare string, not a key
            continue
        k += 1
        while k < n and text[k] in _WS:
            k += 1
        value, end = _read_value(text, k)
        if value is _SKIP:
            i = k                        # could not read it; scan inside instead
        else:
            pairs.append((key, value))
            i = max(end, i + 1)
    return pairs


def _salvage_objects(text):
    payloads, cur = [], {}
    for key, value in _salvage_pairs(text):
        if key in cur:                   # a repeated key means the next payload
            payloads.append(cur)
            cur = {}
        cur[key] = value
    if cur:
        payloads.append(cur)
    return [p for p in payloads if _has_prompt(p)]


def _norm_resolution(v):
    if not v:
        return None
    v = str(v).strip().upper().replace(" ", "")
    return v if v in RESOLUTIONS else None


def _norm_format(v):
    if not v:
        return None
    v = str(v).strip().lower().lstrip(".")
    if v == "jpeg":
        v = "jpg"
    return v if v in FORMATS else None


def _image_urls(value):
    """Keep only entries that are actually links.

    A model asked for this JSON will cheerfully write
    "image_input": ["the character reference photo"] - a description, not a url.
    Sent to kie.ai that is a broken reference and a wasted generation, so
    anything that is not http(s) is dropped on the way in. Returns
    (urls, dropped_count) so the importer can say what it threw away.
    """
    if isinstance(value, str):
        value = [value]
    kept, dropped = [], 0
    for u in value or []:
        text = str(u).strip() if isinstance(u, (str, int, float)) else ""
        if text.lower().startswith(("http://", "https://")):
            kept.append(text)
        elif u not in (None, "", []):
            dropped += 1
    return kept[:8], dropped


def _norm_aspect(v):
    if not v:
        return None
    v = str(v).strip().lower().replace(" ", "")
    if v in ("auto", "adaptive"):
        return "auto"
    v = v.replace("x", ":").replace("/", ":")
    return v if v in ASPECTS else None


def normalize_payload(obj, d=None):
    """Fold one kie.ai-shaped object into the fields a card holds.

    Accepts the nested {model, input:{...}} form and the flat
    {prompt, aspect_ratio, ...} form. Anything without a prompt is not a card -
    a lone callBackUrl fragment, say - and the caller drops it.
    """
    d = d or defaults()
    inp = obj.get("input") if isinstance(obj.get("input"), dict) else obj

    def pick(*keys):
        for src in (inp, obj):
            if not isinstance(src, dict):
                continue
            for k in keys:
                if src.get(k) not in (None, "", []):
                    return src[k]
        return None

    prompt = pick("prompt", "image_prompt", "text")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    prompt = (prompt or "").strip()

    refs, dropped = _image_urls(
        pick("image_input", "image_urls", "images", "reference_images"))

    title = pick("title", "name", "id", "label")
    negative = pick("negative_prompt", "negative")
    if negative:
        # nano-banana has no negative field; the exclusion clause belongs in the
        # prompt itself, and folding it in at import keeps one editable body of
        # text rather than a second box that silently rewrites it later.
        prompt = (prompt + "\n\nDo not include any of the following: "
                  + str(negative).strip().rstrip(".") + ".")

    return {
        "title": str(title)[:120] if title else None,
        "prompt": prompt,
        "model": pick("model") or d["model"],
        "aspect_ratio": (_norm_aspect(pick("aspect_ratio", "aspectRatio", "aspect"))
                         or d["aspect_ratio"]),
        "resolution": (_norm_resolution(pick("resolution", "image_size", "size"))
                       or d["resolution"]),
        "output_format": (_norm_format(pick("output_format", "outputFormat", "format"))
                          or d["output_format"]),
        "image_input": refs,
        "dropped_refs": dropped,
    }


def split_blocks(text):
    """Prompts separated by a blank line.

    The other way in: paste prose, press Enter twice between prompts, done. A
    single newline stays inside a prompt - paragraphs are normal in these - so
    it takes an empty line to start the next one.
    """
    blocks = re.split(r"\n[ \t]*\n+", (text or "").replace("\r\n", "\n").strip())
    return [b.strip() for b in blocks if b.strip()]


def looks_like_json(text):
    return bool(_count_prompts(extract_objects(text)))


def import_text(session_id, text):
    """Blank-line-separated prompts, each becoming a card on the session's
    current settings. Returns (created, skipped, warnings)."""
    blocks = split_blocks(text)
    if not blocks:
        return 0, 0, ["There was nothing to read in that paste."]
    d = session_defaults(session_id)
    for block in blocks:
        create_card(session_id, prompt=block, model=d["model"],
                    aspect_ratio=d["aspect_ratio"], resolution=d["resolution"],
                    output_format=d["output_format"])
    log("info", "text import created " + str(len(blocks)) + " card(s) in session "
        + str(session_id))
    return len(blocks), 0, []


def import_video_text(session_id, text):
    """Blank-line-separated clip prompts, filling the shots in order."""
    blocks = split_blocks(text)
    if not blocks:
        return 0, 0, ["There was nothing to read in that paste."]
    cards = get_cards(session_id)
    warnings = []
    filled = 0
    for card, block in zip(cards, blocks):
        update_card(card["id"], video_prompt=block)
        filled += 1
    if len(blocks) > len(cards):
        # More clip prompts than shots: the rest become standalone clips.
        extra = blocks[len(cards):]
        for block in extra:
            create_card(session_id, kind="clip", video_prompt=block)
            filled += 1
        warnings.append(str(len(extra)) + " prompt(s) had no shot, so they became "
                        "standalone clips.")
    if len(cards) > len(blocks):
        warnings.append("Shots " + str(len(blocks) + 1) + "-" + str(len(cards))
                        + " were left alone.")
    log("info", "video text import filled " + str(filled) + " shot(s)")
    return filled, 0, warnings


def import_json(session_id, text):
    """Turn a pasted blob into cards. Returns (created, skipped, warnings)."""
    objects = extract_objects(text)
    if not objects:
        return 0, 0, ["Nothing that looks like JSON was found in that paste."]

    d = session_defaults(session_id)
    created, skipped, junk, warnings = 0, 0, 0, []
    for obj in objects:
        card = normalize_payload(obj, d)
        junk += card["dropped_refs"]
        if not card["prompt"]:
            skipped += 1
            continue
        card_id = create_card(
            session_id, prompt=card["prompt"], title=card["title"],
            model=card["model"], aspect_ratio=card["aspect_ratio"],
            resolution=card["resolution"], output_format=card["output_format"])
        if card["image_input"]:
            ids = []
            for url in card["image_input"]:
                name = url.rsplit("/", 1)[-1][:80] or "reference"
                ids.append(create_ref(session_id, name, "url", upload_url=url))
            set_card_refs(card_id, ids)
        created += 1
    if skipped:
        warnings.append(str(skipped) + " object(s) had no prompt and were skipped.")
    if junk:
        warnings.append(
            "Ignored " + str(junk) + " image_input entr" +
            ("y" if junk == 1 else "ies") +
            " that were not links. Attach references with the + on the card.")
    log("info", "imported " + str(created) + " card(s) into session " + str(session_id))
    return created, skipped, warnings


def _norm_video_resolution(v):
    if not v:
        return None
    v = str(v).strip().lower().replace(" ", "")
    if v in ("1k", "2k", "4k"):          # a still's vocabulary; not a clip's
        return None
    return v if v in VIDEO_RESOLUTIONS else None


def _norm_duration(v):
    if v in (None, ""):
        return None
    text = str(v).strip().lower().rstrip("s").strip()
    try:
        n = int(round(float(text)))
    except (TypeError, ValueError):
        return None
    if str(n) in VIDEO_DURATIONS:
        return str(n)
    # Snap anything else to the nearest offered length rather than sending a
    # duration the model will reject.
    return min(VIDEO_DURATIONS, key=lambda d: abs(int(d) - n))


def normalize_video_payload(obj, vd=None):
    """One video payload folded into the video fields a card holds."""
    vd = vd or video_defaults()
    inp = obj.get("input") if isinstance(obj.get("input"), dict) else obj

    def pick(*keys):
        for src in (inp, obj):
            if not isinstance(src, dict):
                continue
            for k in keys:
                if src.get(k) not in (None, "", []):
                    return src[k]
        return None

    prompt = pick("prompt", "video_prompt", "text")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)

    return {
        "title": (str(pick("title", "name", "id", "label"))[:120]
                  if pick("title", "name", "id", "label") else None),
        "seq": pick("seq", "shot", "index", "number"),
        "video_prompt": (prompt or "").strip(),
        "video_model": pick("model", "video_model") or vd["model"],
        "video_duration": _norm_duration(pick("duration", "video_duration",
                                              "duration_sec", "seconds"))
                          or vd["duration"],
        "video_resolution": _norm_video_resolution(pick("resolution", "video_resolution"))
                            or vd["resolution"],
        "video_aspect": _norm_aspect(pick("aspect_ratio", "aspectRatio", "video_aspect"))
                        or vd["aspect_ratio"],
    }


def import_video_json(session_id, text):
    """Attach video prompts to the prompts that already exist.

    Shots are matched by *number*, never by when an image happened to be made:
    an explicit seq/shot field wins, then an exact title match, then position in
    the file. That is what keeps "the 4th video prompt" on shot 4 after shot 4's
    image has been regenerated.
    """
    objects = extract_objects(text)
    if not objects:
        return 0, 0, ["Nothing that looks like JSON was found in that paste."]

    cards = get_cards(session_id)

    by_seq = {c["seq"]: c for c in cards}
    by_title = {}
    for c in cards:
        by_title.setdefault((c["title"] or "").strip().lower(), c)

    vd = video_defaults()
    used, filled, unmatched, warnings = set(), 0, 0, []

    for position, obj in enumerate(objects, 1):
        v = normalize_video_payload(obj, vd)
        if not v["video_prompt"]:
            continue

        card = None
        try:
            n = int(v["seq"])
            card = by_seq.get(n)
        except (TypeError, ValueError):
            pass
        if card is None and v["title"]:
            card = by_title.get(v["title"].strip().lower())
        if card is None:
            card = by_seq.get(position)
        if card is None or card["id"] in used:
            # No shot to attach to: it becomes a standalone clip, not a loss.
            new = create_card(session_id, kind="clip", title=v["title"],
                              video_prompt=v["video_prompt"])
            update_card(new, video_duration=v["video_duration"],
                        video_resolution=v["video_resolution"], video_aspect=v["video_aspect"])
            used.add(new)
            filled += 1
            unmatched += 1
            continue

        used.add(card["id"])
        update_card(card["id"], video_prompt=v["video_prompt"],
                    video_model=v["video_model"], video_duration=v["video_duration"],
                    video_resolution=v["video_resolution"],
                    video_aspect=v["video_aspect"])
        filled += 1

    if unmatched:
        warnings.append(str(unmatched) + " video prompt(s) had no shot, so they became "
                        "standalone clips.")
    missing = [c["seq"] for c in cards if c["id"] not in used]
    if missing and filled:
        warnings.append("No video prompt for shot" + ("s " if len(missing) > 1 else " ")
                        + ", ".join(str(x) for x in missing[:12])
                        + ("…" if len(missing) > 12 else "") + ".")
    log("info", "video import filled " + str(filled) + " shot(s) in session "
        + str(session_id))
    return filled, unmatched, warnings


def export_session(session_id):
    out = []
    for card in get_cards(session_id):
        refs = [r["upload_url"] for r in card_refs(card["id"]) if r["upload_url"]]
        out.append({
            "model": card["model"],
            "input": {
                "prompt": card["prompt"],
                "image_input": refs,
                "aspect_ratio": card["aspect_ratio"],
                "resolution": card["resolution"],
                "output_format": card["output_format"],
            },
        })
    return out
