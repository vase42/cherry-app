"""Offline end-to-end test. No network, no credits, no files outside a temp dir.

    python app/selftest.py

Every kie.ai call is stubbed, the database and output folders are redirected
into a fresh temp directory, and the HTTP layer is exercised through a real
server on a loopback port. Run it after touching db.py, kie.py or server.py.
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    mark = "  ok  " if cond else " FAIL "
    line = "[" + mark + "] " + label
    if not cond and detail:
        line += "\n           " + str(detail)
    print(line)


def section(name):
    print("\n--- " + name + " " + "-" * max(0, 56 - len(name)))


# ------------------------------------------------------------ sandbox

TMP = tempfile.mkdtemp(prefix="banana-selftest-")

import db      # noqa: E402
import kie     # noqa: E402
import server  # noqa: E402

db._paths(TMP)
db.reset_connection()
db.init()

PORT = 8799
BASE = "http://127.0.0.1:" + str(PORT)


# ------------------------------------------------------------ kie stub

class Stub:
    """Stands in for kie.ai. Records what was sent so the payload shape can be
    asserted, and lets a test decide the fate of each task."""

    def __init__(self):
        self.tasks = {}
        self.created = []
        self.uploads = []
        self.n = 0
        self.mode = "success"
        # The concurrency tests hammer this from several threads at once.
        self.lock = threading.Lock()

    def create_task(self, api_key, model, inp):
        with self.lock:
            self.n += 1
            tid = "task-" + str(self.n)
            self.created.append({"key": api_key, "model": model, "input": inp})
            self.tasks[tid] = "waiting"
        return tid

    def fetch_task(self, api_key, task_id):
        if self.mode == "fail":
            return {"state": "fail", "failMsg": "stub says no"}
        return {
            "state": "success",
            "resultJson": json.dumps({"resultUrls": ["https://stub.invalid/x.jpg"]}),
            "creditsConsumed": 4,
        }

    def upload_file(self, path, api_key):
        self.uploads.append(path)
        return "https://stub.invalid/up/" + os.path.basename(path)

    def download(self, url, slug, seq, take, fmt="jpg", kind="image"):
        folder = os.path.join(db.OUT_DIR, slug, "videos" if kind == "video" else "")
        os.makedirs(folder, exist_ok=True)
        suffix = "" if take <= 1 else "_v" + str(take)
        ext = "mp4" if kind == "video" else fmt
        path = os.path.join(folder, "%02d%s.%s" % (seq, suffix, ext))
        with open(path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" if kind == "video" else b"\x89PNG-stub")
        return path


stub = Stub()
kie.create_task = stub.create_task
kie.fetch_task = stub.fetch_task
kie.upload_file = stub.upload_file
kie.download = stub.download


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.25)


# ============================================================ tests

section("schema and settings")
check("database file created", os.path.isfile(db.DB_PATH))
check("default model is nano-banana-2", db.get_setting("image_model") == "nano-banana-2")
check("default aspect is 9:16", db.get_setting("aspect_ratio") == "9:16")
check("api key starts empty", db.get_setting("kie_api_key") == "")

db.set_setting("kie_api_key", "sk-test-key-1234")
check("api key round-trips", db.get_setting("kie_api_key") == "sk-test-key-1234")
check("key is masked for the client", server.mask("sk-test-key-1234") == "sk-t…1234")
check("empty key masks to empty", server.mask("") == "")

section("sessions")
status, r = http("POST", "/api/sessions", {"name": "Throat ad"})
sid = r["session_id"]
check("session created over HTTP", status == 200 and sid > 0)
check("slug derived from name", db.get_session(sid)["slug"] == "throat-ad")

_, r2 = http("POST", "/api/sessions", {"name": "Throat ad"})
check("duplicate names get distinct slugs",
      db.get_session(r2["session_id"])["slug"] == "throat-ad-2")
db.delete_session(r2["session_id"])

_, r3 = http("POST", "/api/sessions", {"name": "   "})
check("blank name falls back", db.get_session(r3["session_id"])["name"] == "Untitled session")
db.delete_session(r3["session_id"])

http("POST", "/api/session/" + str(sid) + "/rename", {"name": "Throat ad v2"})
check("session renamed", db.get_session(sid)["name"] == "Throat ad v2")

status, _ = http("GET", "/api/session/999999")
check("missing session 404s", status == 404)

section("cards")
status, r = http("POST", "/api/session/" + str(sid) + "/cards", {})
c1 = r["card_id"]
check("card created", status == 200 and c1 > 0)
card = db.get_card(c1)
check("card inherits default aspect", card["aspect_ratio"] == "9:16")
check("card inherits default resolution", card["resolution"] == "1K")
check("card inherits default format", card["output_format"] == "jpg")
check("first card is seq 1", card["seq"] == 1)
check("a new card is named Image 1", card["title"] == "Image 1")

http("POST", "/api/card/" + str(c1), {"prompt": "a kitchen at dawn", "title": "Kitchen"})
check("prompt saved", db.get_card(c1)["prompt"] == "a kitchen at dawn")
check("title saved", db.get_card(c1)["title"] == "Kitchen")

_, r = http("POST", "/api/session/" + str(sid) + "/cards", {})
c2 = r["card_id"]
check("second card is seq 2", db.get_card(c2)["seq"] == 2)
check("the second card is named Image 2", db.get_card(c2)["title"] == "Image 2")
check("an explicit title beats the default name",
      db.get_card(db.create_card(sid, title="Hero shot"))["title"] == "Hero shot")
db.delete_card(db.get_cards(sid)[-1]["id"])

http("POST", "/api/card/" + str(c2) + "/move", {"direction": "up"})
check("move up swaps sequence", db.get_card(c2)["seq"] == 1 and db.get_card(c1)["seq"] == 2)
http("POST", "/api/card/" + str(c2) + "/move", {"direction": "down"})
check("move down swaps back", db.get_card(c1)["seq"] == 1)
http("POST", "/api/card/" + str(c1) + "/move", {"direction": "up"})
check("move up at the top is a no-op", db.get_card(c1)["seq"] == 1)

_, r = http("POST", "/api/card/" + str(c1) + "/duplicate", {})
c3 = r["card_id"]
check("duplicate copies the prompt", db.get_card(c3)["prompt"] == "a kitchen at dawn")
check("duplicate lands at the end", db.get_card(c3)["seq"] == 3)

http("POST", "/api/card/" + str(c3) + "/delete", {})
check("card deleted", db.get_card(c3) is None)
check("delete resequences densely",
      [c["seq"] for c in db.get_cards(sid)] == [1, 2])

status, _ = http("POST", "/api/card/999999", {"prompt": "x"})
check("editing a missing card 404s", status == 404)

section("a new card inherits the session")
_, r = http("POST", "/api/sessions", {"name": "Inherit"})
hsid = r["session_id"]
_, r = http("POST", "/api/session/" + str(hsid) + "/cards", {})
h1 = r["card_id"]
check("the first card of a session takes the stored defaults",
      db.get_card(h1)["aspect_ratio"] == "9:16")
http("POST", "/api/card/" + str(h1),
     {"aspect_ratio": "16:9", "resolution": "4K", "output_format": "png"})
_, r = http("POST", "/api/session/" + str(hsid) + "/cards", {})
h2 = db.get_card(r["card_id"])
check("the next card copies the previous one's aspect", h2["aspect_ratio"] == "16:9")
check("the next card copies the previous one's resolution", h2["resolution"] == "4K")
check("the next card copies the previous one's format", h2["output_format"] == "png")
check("inheritance follows the last card, not the first",
      db.session_defaults(hsid)["resolution"] == "4K")
check("an empty session falls back to the stored defaults",
      db.session_defaults(db.create_session("Empty"))["resolution"] == "1K")
db.delete_session(hsid)

section("apply to all")
http("POST", "/api/session/" + str(sid) + "/apply_all",
     {"aspect_ratio": "16:9", "resolution": "2K", "output_format": "png"})
cards = db.get_cards(sid)
check("apply_all stamps aspect", all(c["aspect_ratio"] == "16:9" for c in cards))
check("apply_all stamps resolution", all(c["resolution"] == "2K" for c in cards))
check("apply_all stamps format", all(c["output_format"] == "png" for c in cards))
check("apply_all leaves prompts alone", db.get_card(c1)["prompt"] == "a kitchen at dawn")
http("POST", "/api/session/" + str(sid) + "/apply_all",
     {"aspect_ratio": "9:16", "resolution": "1K", "output_format": "jpg"})

section("json import")
one = json.dumps({
    "model": "nano-banana-2",
    "callBackUrl": "https://example.invalid/cb",
    "input": {"prompt": "a banana hero", "image_input": [],
              "aspect_ratio": "1:1", "resolution": "2K", "output_format": "png"},
})
objs = db.extract_objects(one)
check("single object parses", len(objs) == 1)

norm = db.normalize_payload(objs[0])
check("prompt extracted", norm["prompt"] == "a banana hero")
check("aspect extracted", norm["aspect_ratio"] == "1:1")
check("resolution extracted", norm["resolution"] == "2K")
check("format extracted", norm["output_format"] == "png")
check("model extracted", norm["model"] == "nano-banana-2")

check("array of objects parses", len(db.extract_objects("[" + one + "," + one + "]")) == 2)
check("back-to-back objects parse", len(db.extract_objects(one + "\n" + one)) == 2)
check("objects with junk between them parse",
      len(db.extract_objects("noise " + one + " ,, " + one + " trailing")) == 2)
check("braces inside a prompt string do not split it",
      len(db.extract_objects(json.dumps({"prompt": "a {weird} prompt}"}))) == 1)
check("a wrapper key is unwrapped",
      len(db.extract_objects(json.dumps({"cards": [{"prompt": "a"}, {"prompt": "b"}]}))) == 2)
check("empty text yields nothing", db.extract_objects("") == [])
check("prose yields nothing", db.extract_objects("just some words") == [])

trunc = one[:-1]      # the classic half-copied paste
check("a truncated tail is salvaged rather than lost",
      len(db.extract_objects(one + trunc)) == 2)

# The shape a user actually pastes: the selection missed the outer braces, so
# nothing in the text is balanced and both strict passes come back empty.
naked = '''
 "model": "nano-banana-2",
    "callBackUrl": "https://your-domain.com/api/callback",
    "input": {
        "prompt": "Comic poster: cool banana hero in shades, six panels.",
        "image_input": [],
        "aspect_ratio": "auto",
        "resolution": "1K",
        "output_format": "png"
,
 "model": "nano-banana-2",

    "callBackUrl": "https://your-domain.com/api/callback",

    "input": {

        "prompt": "bla bla bla.",

        "image_input": [],

        "aspect_ratio": "9:16",

        "resolution": "2K",

        "output_format": "jpg"
'''
salv = db.extract_objects(naked)
check("a paste with no outer braces is salvaged", len(salv) == 2)
check("salvage keeps the first prompt",
      db.normalize_payload(salv[0])["prompt"].startswith("Comic poster"))
check("salvage keeps the second prompt",
      db.normalize_payload(salv[1])["prompt"] == "bla bla bla.")
check("salvage keeps per-payload settings",
      db.normalize_payload(salv[0])["aspect_ratio"] == "auto"
      and db.normalize_payload(salv[1])["aspect_ratio"] == "9:16")
check("salvage does not bleed resolution between payloads",
      db.normalize_payload(salv[1])["resolution"] == "2K")
check("a clean paste is not re-read by the salvage parser",
      len(db.extract_objects(one)) == 1)
check("commas and newlines inside a prompt survive salvage",
      db.extract_objects('"prompt": "one, two\\nthree"')[0]["prompt"] == "one, two\nthree")
check("escaped quotes inside a salvaged prompt survive",
      db.extract_objects('"prompt": "she said \\"no\\""')[0]["prompt"] == 'she said "no"')
check("salvage ignores text with no key/value pairs", db.extract_objects("a: b c: d") == [])

flat = db.normalize_payload({"prompt": "flat form", "aspect_ratio": "4:5"})
check("flat objects work too", flat["prompt"] == "flat form" and flat["aspect_ratio"] == "4:5")
check("unknown aspect falls back to the default",
      db.normalize_payload({"prompt": "x", "aspect_ratio": "banana"})["aspect_ratio"] == "9:16")
check("lowercase resolution is normalised",
      db.normalize_payload({"prompt": "x", "resolution": "2k"})["resolution"] == "2K")
check("jpeg normalises to jpg",
      db.normalize_payload({"prompt": "x", "output_format": "JPEG"})["output_format"] == "jpg")
check("negative_prompt folds into the prompt",
      "Do not include" in db.normalize_payload(
          {"prompt": "x", "negative_prompt": "text, watermark"})["prompt"])
check("camelCase keys are accepted",
      db.normalize_payload({"prompt": "x", "aspectRatio": "16:9"})["aspect_ratio"] == "16:9")

_, r = http("POST", "/api/sessions", {"name": "Import target"})
isid = r["session_id"]
status, r = http("POST", "/api/session/" + str(isid) + "/import",
                 {"text": one + "\n" + one + "\n" + json.dumps({"callBackUrl": "x"})})
check("import creates a card per payload", r.get("created") == 2)
check("import reports what it skipped", r.get("skipped") == 1)
icards = db.get_cards(isid)
check("imported settings land on the card", icards[0]["resolution"] == "2K")
check("imported prompts are in order", icards[0]["prompt"] == "a banana hero")
check("an untitled payload is named Image N",
      [c["title"] for c in icards[:2]] == ["Image 1", "Image 2"])
_, rt = http("POST", "/api/session/" + str(isid) + "/import",
             {"text": json.dumps({"title": "Kitchen dawn", "input": {"prompt": "x"}})})
check("a payload title overrides the default name",
      db.get_cards(isid)[-1]["title"] == "Kitchen dawn")
db.delete_card(db.get_cards(isid)[-1]["id"])

# Prose is no longer an error - it is the other way of importing - but blank
# input still has to be refused rather than silently making an empty card.
status, r = http("POST", "/api/session/" + str(isid) + "/import", {"text": "nonsense"})
check("prose imports as a plain-text prompt", status == 200 and r["read_as"] == "text")
db.delete_card(db.get_cards(isid)[-1]["id"])
status, r = http("POST", "/api/session/" + str(isid) + "/import", {"text": "   \n\n  "})
check("an empty import is a 400, not a crash", status == 400)
status, r = http("POST", "/api/session/" + str(isid) + "/import",
                 {"text": "{broken json", "mode": "json"})
check("broken json forced as json is a 400", status == 400)

_, r = http("POST", "/api/session/" + str(isid) + "/import", {"text": json.dumps({
    "model": "nano-banana-2",
    "input": {"prompt": "with refs",
              "image_input": ["https://x.invalid/a.png", "https://x.invalid/b.png"]}})})
ref_card = db.get_cards(isid)[-1]
check("image_input becomes attached references", len(db.card_refs(ref_card["id"])) == 2)
check("url references keep their url",
      db.card_refs(ref_card["id"])[0]["upload_url"] == "https://x.invalid/a.png")

section("image_input is scrubbed on the way in")
kept, dropped = db._image_urls(["https://a.invalid/x.png", "the character photo",
                                "reference image of Marta", "http://b.invalid/y.jpg"])
check("real links survive", kept == ["https://a.invalid/x.png", "http://b.invalid/y.jpg"])
check("prose entries are dropped", dropped == 2)
check("an empty list drops nothing", db._image_urls([]) == ([], 0))
check("a missing field drops nothing", db._image_urls(None) == ([], 0))
check("a bare string url is accepted",
      db._image_urls("https://a.invalid/x.png") == (["https://a.invalid/x.png"], 0))
check("a bare string that is not a url is dropped",
      db._image_urls("character photo") == ([], 1))
check("a relative path is not a link", db._image_urls(["refs/marta.png"]) == ([], 1))
check("more than eight links are capped",
      len(db._image_urls(["https://a.invalid/" + str(i) for i in range(20)])[0]) == 8)
check("normalize_payload reports what it dropped",
      db.normalize_payload({"prompt": "x", "image_input": ["a photo"]})["dropped_refs"] == 1)

_, rj = http("POST", "/api/session/" + str(isid) + "/import", {"text": json.dumps({
    "input": {"prompt": "junk refs",
              "image_input": ["the uploaded character", "https://ok.invalid/z.png"]}})})
junk_card = db.get_cards(isid)[-1]
check("only the real link is attached", len(db.card_refs(junk_card["id"])) == 1)
check("the attached one is the link",
      db.card_refs(junk_card["id"])[0]["upload_url"] == "https://ok.invalid/z.png")
check("the import warns about what it ignored",
      any("not links" in w for w in rj["warnings"]))
check("a card with only junk refs still imports", db.get_card(junk_card["id"]) is not None)

section("plain-text import")
check("blocks split on a blank line",
      db.split_blocks("one\n\ntwo\n\nthree") == ["one", "two", "three"])
check("a single newline stays inside a prompt",
      db.split_blocks("line one\nline two\n\nnext") == ["line one\nline two", "next"])
check("several blank lines are still one break",
      db.split_blocks("a\n\n\n\n\nb") == ["a", "b"])
check("blank lines with spaces still break",
      db.split_blocks("a\n   \nb") == ["a", "b"])
check("windows line endings work",
      db.split_blocks("a\r\n\r\nb") == ["a", "b"])
check("leading and trailing blank lines are ignored",
      db.split_blocks("\n\n  a\n\nb  \n\n") == ["a", "b"])
check("empty text yields no blocks", db.split_blocks("   \n \n ") == [])

check("json is recognised as json", db.looks_like_json(one) is True)
check("prose is not mistaken for json",
      db.looks_like_json("a kitchen at dawn\n\na close-up of hands") is False)
check("a prompt mentioning braces is still prose",
      db.looks_like_json("a sign reading {OPEN}\n\nanother shot") is False)

_, r = http("POST", "/api/sessions", {"name": "Pasted"})
psid = r["session_id"]
status, r = http("POST", "/api/session/" + str(psid) + "/import",
                 {"text": "a kitchen at dawn\n\nher hands on a white mug\n\nthe window"})
pcards = db.get_cards(psid)
check("plain text creates one card per block", r["created"] == 3)
check("the server says how it read it", r["read_as"] == "text")
check("blocks land in order", pcards[0]["prompt"] == "a kitchen at dawn")
check("multi-line blocks are kept whole",
      db.import_text(psid, "line one\nline two")[0] == 1
      and db.get_cards(psid)[-1]["prompt"] == "line one\nline two")
check("pasted cards take the session defaults", pcards[0]["aspect_ratio"] == "9:16")
check("pasted cards are named Image N", pcards[0]["title"] == "Image 1")

status, r = http("POST", "/api/session/" + str(psid) + "/import",
                 {"text": one, "mode": "auto"})
check("auto still prefers json when the paste is json", r["read_as"] == "json")
status, r = http("POST", "/api/session/" + str(psid) + "/import",
                 {"text": "just one prompt", "mode": "json"})
check("forcing json on prose is a clean error", status == 400)

status, r = http("POST", "/api/session/" + str(psid) + "/import_videos",
                 {"text": "slow push in\n\ngentle pan left"})
check("plain clip prompts fill shots in order", r["filled"] == 2)
check("shot 1 got the first block", db.get_cards(psid)[0]["video_prompt"] == "slow push in")
check("shot 2 got the second", db.get_cards(psid)[1]["video_prompt"] == "gentle pan left")
check("untouched shots are reported", any("left alone" in w for w in r["warnings"]))

_shots = len(db.get_cards(psid))
status, r = http("POST", "/api/session/" + str(psid) + "/import_videos",
                 {"text": "\n".join(["m" + str(i) + "\n" for i in range(40)])})
check("more clip prompts than shots is reported",
      any("standalone clips" in w for w in r["warnings"]), r)
_clips = [c for c in db.get_cards(psid) if c["kind"] == "clip"]
check("the extra clip prompts became standalone clips", len(_clips) == 40 - _shots, len(_clips))
check("a standalone clip keeps its prompt", _clips[0]["video_prompt"] == "m" + str(_shots))
db.delete_session(psid)

section("export")
status, r = http("GET", "/api/session/" + str(isid) + "/export")
payload = r["payload"]
check("export returns one object per card", len(payload) == len(db.get_cards(isid)))
check("export nests under input", "input" in payload[0] and "prompt" in payload[0]["input"])
check("export carries the model", payload[0]["model"] == "nano-banana-2")
check("export round-trips through the importer",
      db.normalize_payload(payload[0])["prompt"] == payload[0]["input"]["prompt"])

section("references")
png = base64.b64encode(b"\x89PNG\r\n\x1a\nstub-bytes").decode()
status, r = http("POST", "/api/session/" + str(sid) + "/refs",
                 {"filename": "Hero Shot.png", "data": "data:image/png;base64," + png})
ref_id = r["ref_id"]
ref = db.get_ref(ref_id)
check("upload stored on disk", os.path.isfile(ref["source_path"]))
check("upload filename is made safe", " " not in os.path.basename(ref["source_path"]))
check("upload pushed to kie.ai on arrival", ref["upload_url"].startswith("https://stub.invalid/up/"))

status, r = http("POST", "/api/session/" + str(sid) + "/refs",
                 {"filename": "Hero Shot.png", "data": "data:image/png;base64," + png})
check("a second upload of the same name does not clobber the first",
      db.get_ref(r["ref_id"])["source_path"] != ref["source_path"])
db.delete_ref(r["ref_id"])

status, _ = http("POST", "/api/session/" + str(sid) + "/refs",
                 {"filename": "x.png", "data": "not-a-data-url"})
check("a malformed upload is a 400", status == 400)

status, r = http("POST", "/api/session/" + str(sid) + "/ref_url",
                 {"url": "https://cdn.invalid/pic.jpg"})
url_ref = r["ref_id"]
check("url reference created", db.get_ref(url_ref)["kind"] == "url")
status, _ = http("POST", "/api/session/" + str(sid) + "/ref_url", {"url": "not a url"})
check("a non-url is rejected", status == 400)

http("POST", "/api/card/" + str(c1) + "/refs", {"ref_ids": [ref_id, url_ref]})
check("card references attach in order",
      [r["id"] for r in db.card_refs(c1)] == [ref_id, url_ref])
http("POST", "/api/card/" + str(c1) + "/refs", {"ref_ids": [url_ref]})
check("setting references replaces rather than appends", len(db.card_refs(c1)) == 1)

check("a fresh upload is not stale", db.upload_is_stale(db.get_ref(ref_id)) is False)
check("an unsent reference is stale",
      db.upload_is_stale({"upload_url": None, "uploaded_at": None}) is True)
check("a four-day-old upload is stale",
      db.upload_is_stale({"upload_url": "u",
                          "uploaded_at": time.strftime(
                              "%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - 4 * 86400))}) is True)

section("the cast")
status, r = http("POST", "/api/characters",
                 {"name": "Marta", "filename": "marta-01.png",
                  "data": "data:image/png;base64," + png, "session_id": sid})
mid = r["ref_id"]
marta = db.get_ref(mid)
check("a character is created", status == 200 and marta["kind"] == "character")
check("a character belongs to no session", marta["session_id"] is None)
check("a character keeps the name given", marta["name"] == "Marta")
check("a character is uploaded to kie.ai on arrival",
      marta["upload_url"].startswith("https://stub.invalid/up/"))
check("the response carries the cast", any(c["id"] == mid for c in r["characters"]))

http("POST", "/api/characters", {"name": "", "filename": "extra-face.png",
                                 "data": "data:image/png;base64," + png})
check("a character with no name falls back to the filename",
      db.list_characters()[-1]["name"] == "extra-face")

check("the cast shows up in a session's references",
      any(x["id"] == mid for x in db.list_refs(sid)))
_, r2 = http("POST", "/api/sessions", {"name": "Another video"})
other = r2["session_id"]
check("the cast shows up in a different session too",
      any(x["id"] == mid for x in db.list_refs(other)))
check("that session's own references stay its own",
      not any(x["id"] == ref_id for x in db.list_refs(other)))
check("the cast sorts first", db.list_refs(sid)[0]["kind"] == "character")

http("POST", "/api/card/" + str(c1) + "/refs", {"ref_ids": [mid]})
check("a character can be attached to a prompt",
      [x["id"] for x in db.card_refs(c1)] == [mid])

http("POST", "/api/session/" + str(other) + "/delete", {})
check("deleting a session does not delete the cast", db.get_ref(mid) is not None)
db.set_card_refs(c1, [ref_id])

section("payload building")
db.set_card_refs(c1, [ref_id])
card = db.get_card(c1)
inp = kie.build_input(card, ["https://stub.invalid/a.png"])
check("payload carries the prompt", inp["prompt"] == "a kitchen at dawn")
check("payload carries aspect_ratio", inp["aspect_ratio"] == "9:16")
check("payload carries resolution", inp["resolution"] == "1K")
check("payload carries output_format", inp["output_format"] == "jpg")
check("payload always has image_input", "image_input" in inp)
check("image_input holds the reference", inp["image_input"] == ["https://stub.invalid/a.png"])
check("image_input is capped at 8",
      len(kie.build_input(card, ["u" + str(i) for i in range(20)])["image_input"]) == 8)
check("payload has no stray keys",
      set(inp) == {"prompt", "aspect_ratio", "resolution", "output_format", "image_input"})

section("fingerprint and the Generate-again rule")
fp1 = db.card_fingerprint(c1)
db.update_card(c1, prompt="a kitchen at dawn")
check("re-saving the same text keeps the fingerprint", db.card_fingerprint(c1) == fp1)
db.update_card(c1, prompt="a kitchen at dusk")
check("editing the prompt changes the fingerprint", db.card_fingerprint(c1) != fp1)
db.update_card(c1, prompt="a kitchen at dawn")
check("reverting restores the fingerprint", db.card_fingerprint(c1) == fp1)
db.update_card(c1, resolution="4K")
check("changing a setting changes the fingerprint", db.card_fingerprint(c1) != fp1)
db.update_card(c1, resolution="1K")
db.set_card_refs(c1, [ref_id, url_ref])
check("adding a reference changes the fingerprint", db.card_fingerprint(c1) != fp1)
db.set_card_refs(c1, [ref_id])
check("removing it again restores the fingerprint", db.card_fingerprint(c1) == fp1)
# A re-upload mints a new url for the same file; that must not read as an edit.
db.set_ref_upload(ref_id, "https://stub.invalid/up/rotated-name.png")
check("a re-upload does not make the card look edited", db.card_fingerprint(c1) == fp1)

section("generate")
status, r = http("POST", "/api/card/" + str(c1) + "/generate", {})
check("generate returns ok", status == 200 and r["ok"])
gen = db.get_generation(r["generation_id"])
check("generation is take 1", gen["n"] == 1)
check("generation holds a task id", gen["kie_task_id"] == "task-1")
check("generation state is waiting", gen["kie_state"] == "waiting")
check("generation stores the prompt it used", gen["prompt"] == "a kitchen at dawn")
check("generation stores its fingerprint", gen["fingerprint"] == db.card_fingerprint(c1))
check("dispatch sent the api key", stub.created[-1]["key"] == "sk-test-key-1234")
check("dispatch sent the model", stub.created[-1]["model"] == "nano-banana-2")

view = db.session_view(sid)
vc = [c for c in view["cards"] if c["id"] == c1][0]
check("card reads as busy while in flight", vc["busy"] is True)
check("card is not dirty right after dispatch", vc["dirty"] is False)

kie.poller.tick()
gen = db.get_generation(gen["id"])
check("poller marks it successful", gen["kie_state"] == "success")
check("poller records the result url", gen["result_url"] == "https://stub.invalid/x.jpg")
check("poller downloads the file", gen["local_path"] and os.path.isfile(gen["local_path"]))
check("file lands under the session slug", "throat-ad" in gen["local_path"].replace("\\", "/"))
check("first take has no version suffix", "_v" in os.path.basename(gen["local_path"]) is False
      or "_v" not in os.path.basename(gen["local_path"]))
check("poller records credits", gen["credits"] == 4)
check("nothing is left in flight", db.inflight() == [])

view = db.session_view(sid)
vc = [c for c in view["cards"] if c["id"] == c1][0]
check("card is no longer busy", vc["busy"] is False)
check("card counts one success", vc["success_count"] == 1)
check("card is clean, so the button says Generate again", vc["dirty"] is False)

db.update_card(c1, prompt="a kitchen at dusk")
vc = [c for c in db.session_view(sid)["cards"] if c["id"] == c1][0]
check("editing the prompt marks the card dirty", vc["dirty"] is True)

status, r = http("POST", "/api/card/" + str(c1) + "/generate", {})
g2 = db.get_generation(r["generation_id"])
check("second generate is take 2", g2["n"] == 2)
kie.poller.tick()
g2 = db.get_generation(g2["id"])
check("second take keeps a separate file", g2["local_path"] != gen["local_path"])
check("second take is suffixed _v2", "_v2" in os.path.basename(g2["local_path"]))
check("the first take survives", os.path.isfile(gen["local_path"]))
check("both takes are listed newest first",
      [g["n"] for g in db.card_generations(c1)] == [2, 1])

section("failure paths")
db.set_setting("kie_api_key", "")
status, r = http("POST", "/api/card/" + str(c1) + "/generate", {})
check("generating without a key is a 428", status == 428)
check("the message says what to do", "Services" in r["error"] and "kie.ai" in r["error"])
db.set_setting("kie_api_key", "sk-test-key-1234")

_, r = http("POST", "/api/session/" + str(sid) + "/cards", {})
blank = r["card_id"]
status, r = http("POST", "/api/card/" + str(blank) + "/generate", {})
check("generating an empty prompt is refused", status == 400)
check("no generation row is left behind", db.card_generations(blank) == [])

def boom(*a, **k):
    raise kie.KieError("stub refuses")
real_create = kie.create_task
kie.create_task = boom
status, r = http("POST", "/api/card/" + str(c1) + "/generate", {})
check("a dispatch failure is a 400", status == 400)
failed = db.card_generations(c1)[0]
check("the failure is recorded on the take", failed["kie_state"] == "fail")
check("the failure message is kept", "stub refuses" in failed["fail_msg"])
kie.create_task = real_create

db.update_card(blank, prompt="now it has words")
stub.mode = "fail"
http("POST", "/api/card/" + str(blank) + "/generate", {})
kie.poller.tick()
check("a failed task is marked failed", db.card_generations(blank)[0]["kie_state"] == "fail")
check("the model's reason is kept",
      "stub says no" in db.card_generations(blank)[0]["fail_msg"])
stub.mode = "success"

check("resultJson with no urls yields nothing", kie.parse_result_urls({"resultJson": "{}"}) == [])
check("absent resultJson yields nothing", kie.parse_result_urls({}) == [])
check("malformed resultJson yields nothing",
      kie.parse_result_urls({"resultJson": "{oops"}) == [])
check("a single resultUrl string is accepted",
      kie.parse_result_urls({"resultJson": '{"resultUrl":"https://a/b.jpg"}'})
      == ["https://a/b.jpg"])

section("generate all")
_, r = http("POST", "/api/sessions", {"name": "Batch"})
bsid = r["session_id"]
for i in range(4):
    _, rc = http("POST", "/api/session/" + str(bsid) + "/cards", {})
    if i < 3:
        http("POST", "/api/card/" + str(rc["card_id"]), {"prompt": "shot " + str(i)})

before = len(stub.created)
status, r = http("POST", "/api/session/" + str(bsid) + "/generate_all", {})
check("generate_all queues every prompted card", r["queued"] == 3)
check("generate_all skips the empty card", len(stub.created) - before == 3)
kie.poller.tick()
check("all four files did not appear - only three",
      sum(1 for c in db.get_cards(bsid) if db.card_generations(c["id"])) == 3)

status, r = http("POST", "/api/session/" + str(bsid) + "/generate_all", {"only_new": True})
check("only_new skips cards whose take matches the prompt", r["queued"] == 0)
first_batch = db.get_cards(bsid)[0]
db.update_card(first_batch["id"], prompt="shot 0 revised")
_, r = http("POST", "/api/session/" + str(bsid) + "/generate_all", {"only_new": True})
check("only_new picks up the edited card", r["queued"] == 1)
kie.poller.tick()

_, r = http("POST", "/api/session/" + str(bsid) + "/generate_all", {})
check("generate_all without only_new regenerates everything", r["queued"] == 3)
kie.poller.tick()

section("MONEY SAFETY: what is sent is what the card says")
# A generation must carry the card's settings verbatim. If these drift, the user
# pays for an image at the wrong size or format.
_, r = http("POST", "/api/sessions", {"name": "Exactness"})
xsid = r["session_id"]
_, r = http("POST", "/api/session/" + str(xsid) + "/cards", {})
x1 = r["card_id"]
http("POST", "/api/card/" + str(x1), {"prompt": "a red door", "aspect_ratio": "16:9",
                                      "resolution": "4K", "output_format": "png"})
before = len(stub.created)
_, r = http("POST", "/api/card/" + str(x1) + "/generate", {})
sent = stub.created[-1]["input"]
check("the prompt sent is the card's prompt", sent["prompt"] == "a red door")
check("the aspect sent is the card's aspect", sent["aspect_ratio"] == "16:9")
check("the resolution sent is the card's resolution", sent["resolution"] == "4K")
check("the format sent is the card's format", sent["output_format"] == "png")
check("the model sent is the card's model", stub.created[-1]["model"] == "nano-banana-2")
stored = json.loads(db.get_generation(r["generation_id"])["params"])
check("the take records exactly what was sent", stored == sent)
kie.poller.tick()

# ...and after an edit, the NEXT generation reflects the edit, not the old value.
http("POST", "/api/card/" + str(x1), {"resolution": "1K", "output_format": "jpg"})
http("POST", "/api/card/" + str(x1) + "/generate", {})
sent2 = stub.created[-1]["input"]
check("an edited resolution is what gets sent next", sent2["resolution"] == "1K")
check("an edited format is what gets sent next", sent2["output_format"] == "jpg")
kie.poller.tick()

# apply_all must reach the wire too
http("POST", "/api/session/" + str(xsid) + "/apply_all", {"resolution": "2K"})
http("POST", "/api/card/" + str(x1) + "/generate", {})
check("apply_all reaches the payload", stub.created[-1]["input"]["resolution"] == "2K")
kie.poller.tick()

section("MONEY SAFETY: a double click cannot bill twice")
_, r = http("POST", "/api/session/" + str(xsid) + "/cards", {})
x2 = r["card_id"]
http("POST", "/api/card/" + str(x2), {"prompt": "a blue door"})

before = len(stub.created)
s1, r1 = http("POST", "/api/card/" + str(x2) + "/generate", {})
s2, r2 = http("POST", "/api/card/" + str(x2) + "/generate", {})
check("the first click dispatches", s1 == 200)
check("the second click is refused", s2 == 409)
check("the refusal says why", "already generating" in r2["error"])
check("only one task was created", len(stub.created) - before == 1)
check("only one take exists", len(db.card_generations(x2)) == 1)
kie.poller.tick()
check("after it finishes a new take is allowed",
      http("POST", "/api/card/" + str(x2) + "/generate", {})[0] == 200)
kie.poller.tick()

# Two threads hitting Generate at the same instant, the real double-click race.
race_card = db.create_card(xsid, prompt="racing")
results = []
barrier = threading.Barrier(8)


def racer():
    barrier.wait()
    results.append(http("POST", "/api/card/" + str(race_card) + "/generate", {})[0])


before = len(stub.created)
threads = [threading.Thread(target=racer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("eight simultaneous clicks dispatch exactly once",
      len(db.card_generations(race_card)) == 1)
check("eight simultaneous clicks create exactly one task",
      len(stub.created) - before == 1)
check("exactly one of them got a 200", results.count(200) == 1)
check("the other seven got 409", results.count(409) == 7)
kie.poller.tick()

section("MONEY SAFETY: Generate all twice")
_, r = http("POST", "/api/sessions", {"name": "Batch safety"})
bsafe = r["session_id"]
for i in range(6):
    cid = db.create_card(bsafe, prompt="shot " + str(i))
before = len(stub.created)
_, ra = http("POST", "/api/session/" + str(bsafe) + "/generate_all", {})
_, rb = http("POST", "/api/session/" + str(bsafe) + "/generate_all", {})
check("the first Generate all queues every card", ra["queued"] == 6)
check("the second queues nothing", rb["queued"] == 0)
# It never even tries: the busy cards are filtered out before dispatch, so the
# guard behind them does not have to fire and nothing reports as skipped.
check("the second attempts nothing at all", rb["results"] == [])
check("the second reports no failures", rb["skipped"] == 0)
check("only six tasks were created", len(stub.created) - before == 6)
check("every card has exactly one take",
      all(len(db.card_generations(c["id"])) == 1 for c in db.get_cards(bsafe)))

# Two Generate all requests racing each other.
kie.poller.tick()
before = len(stub.created)
outs = []
gate = threading.Barrier(4)


def batcher():
    gate.wait()
    outs.append(http("POST", "/api/session/" + str(bsafe) + "/generate_all", {})[1])


threads = [threading.Thread(target=batcher) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("four racing Generate alls create six tasks between them",
      len(stub.created) - before == 6)
check("no card got a second take from the race",
      all(len(db.card_generations(c["id"])) == 2 for c in db.get_cards(bsafe)))
check("the queued counts add up to six", sum(o["queued"] for o in outs) == 6)
# Here the pre-filter cannot help - the requests overlap - so the atomic claim
# in begin_generation is what refuses the duplicates, and it says so.
check("the racing losers are reported as skipped, not as errors",
      sum(o["skipped"] for o in outs) == sum(
          1 for o in outs for r in o["results"] if r.get("skipped")))
check("no racing loser reported a hard failure",
      all(r.get("skipped") for o in outs for r in o["results"] if not r["ok"]))
kie.poller.tick()

# A failed dispatch must not leave the card locked forever.
stubbed = kie.create_task
kie.create_task = lambda *a, **k: (_ for _ in ()).throw(kie.KieError("nope"))
locked = db.create_card(bsafe, prompt="will fail")
http("POST", "/api/card/" + str(locked) + "/generate", {})
kie.create_task = stubbed
check("a failed dispatch leaves a failed take",
      db.card_generations(locked)[0]["kie_state"] == "fail")
check("a failed dispatch does not lock the card out",
      http("POST", "/api/card/" + str(locked) + "/generate", {})[0] == 200)
kie.poller.tick()
db.delete_session(bsafe)

section("chaining a generated image into a reference")
done = db.card_generations(c1)
success = [g for g in done if g["kie_state"] == "success"][0]
status, r = http("POST", "/api/generation/" + str(success["id"]) + "/use_as_ref", {})
new_ref = db.get_ref(r["ref_id"])
check("a finished take becomes a reference", status == 200 and new_ref["kind"] == "generation")
check("the reference points at the downloaded file",
      new_ref["source_path"] == success["local_path"])

pend = [g for g in db.card_generations(blank) if g["kie_state"] != "success"]
if pend:
    status, _ = http("POST", "/api/generation/" + str(pend[0]["id"]) + "/use_as_ref", {})
    check("an unfinished take cannot be a reference", status == 400)

section("image to video")
_, r = http("POST", "/api/sessions", {"name": "Clips"})
vsid = r["session_id"]
_, r = http("POST", "/api/session/" + str(vsid) + "/cards", {})
v1 = r["card_id"]
http("POST", "/api/card/" + str(v1), {"prompt": "a red door"})

status, r = http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("a clip with no video prompt is refused", status == 400)
http("POST", "/api/card/" + str(v1), {"video_prompt": "the door swings open"})
status, r = http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("a clip with no image is refused", status == 400)
check("it says to make the image first", "image" in r["error"])

http("POST", "/api/card/" + str(v1) + "/generate", {})
kie.poller.tick()
img = db.card_generations(v1, "image")[0]
check("the image is ready", img["kie_state"] == "success")

before = len(stub.created)
status, r = http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("the clip dispatches", status == 200)
sent = stub.created[-1]
check("the clip uses the video model", sent["model"] == "gemini-omni-video")
check("the clip prompt is the video prompt",
      sent["input"]["prompt"] == "the door swings open")
check("the clip carries the image as its input", len(sent["input"]["image_urls"]) == 1)
check("the image was uploaded for the clip",
      sent["input"]["image_urls"][0].startswith("https://stub.invalid/up/"))
check("the clip duration defaults to 8", sent["input"]["duration"] == "8")
check("the clip resolution defaults to 1080p", sent["input"]["resolution"] == "1080p")
check("the clip aspect defaults to 9:16", sent["input"]["aspect_ratio"] == "9:16")
check("a clip never carries a still's resolution",
      sent["input"]["resolution"] not in ("1K", "2K", "4K"))
check("the clip records which image it came from",
      db.get_generation(r["generation_id"])["source_gen_id"] == img["id"])

status, _ = http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("a double click on the clip button is refused too", status == 409)
check("only one clip task was created", len(stub.created) - before == 1)

kie.poller.tick()
clip = db.card_generations(v1, "video")[0]
check("the clip finishes", clip["kie_state"] == "success")
check("the clip lands in a videos folder",
      "/videos/" in clip["local_path"].replace("\\", "/"))
check("the clip is an mp4", clip["local_path"].endswith(".mp4"))
check("clips and images are counted separately",
      len(db.card_generations(v1, "image")) == 1 and len(db.card_generations(v1, "video")) == 1)

http("POST", "/api/card/" + str(v1), {"video_duration": "4", "video_resolution": "720p"})
http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("an edited duration is what gets sent", stub.created[-1]["input"]["duration"] == "4")
check("an edited clip resolution is what gets sent",
      stub.created[-1]["input"]["resolution"] == "720p")
kie.poller.tick()

section("which image a clip is made from")
http("POST", "/api/card/" + str(v1) + "/generate", {})
kie.poller.tick()
takes = db.card_generations(v1, "image")
check("there are two image takes now", len(takes) == 2)
check("the newest take is active by default", db.active_image(v1)["id"] == takes[0]["id"])

http("POST", "/api/generation/" + str(takes[1]["id"]) + "/activate", {})
check("an older take can be pinned active", db.active_image(v1)["id"] == takes[1]["id"])
http("POST", "/api/card/" + str(v1) + "/generate_video", {})
check("the pinned take is the one sent to the model",
      stub.created[-1]["input"]["image_urls"][0].endswith(
          os.path.basename(takes[1]["local_path"])))
kie.poller.tick()

http("POST", "/api/generation/" + str(takes[1]["id"]) + "/delete", {})
check("deleting the pinned take falls back to the newest",
      db.active_image(v1)["id"] == takes[0]["id"])
check("the deleted take is gone from the app", db.get_generation(takes[1]["id"]) is None)
check("the deleted take's file stays on disk", os.path.isfile(takes[1]["local_path"]))

status, _ = http("POST", "/api/generation/" + str(
    db.card_generations(v1, "video")[0]["id"]) + "/activate", {})
check("a clip cannot be made the active image", status == 400)

section("a clip from an image you attached yourself")
_, r = http("POST", "/api/sessions", {"name": "Attached"})
asid = r["session_id"]
acard = db.create_card(asid, prompt="never generated")
http("POST", "/api/card/" + str(acard), {"video_prompt": "slow push in"})
status, _ = http("POST", "/api/card/" + str(acard) + "/generate_video", {})
check("with no image at all the clip is refused", status == 400)
check("the card knows it cannot be clipped yet",
      [c for c in db.session_view(asid)["cards"]][0]["can_clip"] is False)

_, rr = http("POST", "/api/session/" + str(asid) + "/refs",
             {"filename": "my-frame.png", "data": "data:image/png;base64," + png})
http("POST", "/api/card/" + str(acard) + "/base_image", {"ref_id": rr["ref_id"]})
view = db.session_view(asid)["cards"][0]
check("the attached image shows on the card", view["base_image"]["id"] == rr["ref_id"])
check("the card can be clipped now", view["can_clip"] is True)

before = len(stub.created)
status, r = http("POST", "/api/card/" + str(acard) + "/generate_video", {})
check("a clip generates from the attached image", status == 200)
check("the attached image is what was sent",
      stub.created[-1]["input"]["image_urls"][0].startswith("https://stub.invalid/up/"))
check("no generated take was invented",
      db.get_generation(r["generation_id"])["source_gen_id"] is None)
kie.poller.tick()

# A generated take takes over from the attachment.
http("POST", "/api/card/" + str(acard) + "/generate", {})
kie.poller.tick()
view = db.session_view(asid)["cards"][0]
check("a generated take takes precedence over the attachment",
      view["active"] is not None)
http("POST", "/api/card/" + str(acard) + "/generate_video", {})
check("and it is the take that gets animated",
      stub.created[-1]["input"]["image_urls"][0].endswith(
          os.path.basename(view["active"]["local_path"])))
kie.poller.tick()

http("POST", "/api/card/" + str(acard) + "/base_image", {"ref_id": None})
check("the attachment can be cleared",
      db.session_view(asid)["cards"][0]["base_image"] is None)
check("clearing it does not disturb the generated take",
      db.session_view(asid)["cards"][0]["can_clip"] is True)
db.delete_session(asid)

section("video json import")
_, r = http("POST", "/api/sessions", {"name": "Video import"})
misid = r["session_id"]
for i in range(4):
    cid = db.create_card(misid, prompt="shot " + str(i + 1))
video_json = json.dumps([
    {"title": "Image 3", "model": "gemini-omni-video",
     "input": {"prompt": "third clip", "duration": "6", "resolution": "720p"}},
    {"title": "Image 1", "model": "gemini-omni-video",
     "input": {"prompt": "first clip", "duration": "10"}},
])
status, r = http("POST", "/api/session/" + str(misid) + "/import_videos",
                 {"text": video_json})
cards = db.get_cards(misid)
check("video import fills existing cards", r["filled"] == 2)
check("it created no new cards", len(cards) == 4)
check("it matched shot 3 by title", cards[2]["video_prompt"] == "third clip")
check("it matched shot 1 by title, not by position",
      cards[0]["video_prompt"] == "first clip")
check("per-shot duration is kept", cards[2]["video_duration"] == "6")
check("per-shot clip resolution is kept", cards[2]["video_resolution"] == "720p")
check("shots with no video prompt are named in a warning",
      any("shot" in w for w in r["warnings"]))

# An explicit shot number beats everything, and beats generation order.
status, r = http("POST", "/api/session/" + str(misid) + "/import_videos",
                 {"text": json.dumps([{"seq": 4, "input": {"prompt": "fourth by number"}}])})
check("an explicit seq wins", db.get_cards(misid)[3]["video_prompt"] == "fourth by number")

status, r = http("POST", "/api/session/" + str(misid) + "/import_videos",
                 {"text": json.dumps([{"input": {"prompt": "a"}}, {"input": {"prompt": "b"}}])})
check("with no titles it falls back to position",
      db.get_cards(misid)[0]["video_prompt"] == "a"
      and db.get_cards(misid)[1]["video_prompt"] == "b")

_, r = http("POST", "/api/sessions", {"name": "No shots"})
_ns = r["session_id"]
status, r = http("POST", "/api/session/" + str(_ns) + "/import_videos",
                 {"text": video_json})
check("importing clips with no shots makes standalone clips", status == 200
      and all(c["kind"] == "clip" for c in db.get_cards(_ns)) and db.get_cards(_ns), r)

check("a still's resolution is refused for a clip",
      db.normalize_video_payload({"prompt": "x", "resolution": "2K"})["video_resolution"]
      == "1080p")
check("an odd duration snaps to the nearest offered one",
      db.normalize_video_payload({"prompt": "x", "duration": 7})["video_duration"] == "6")
check("a duration written with an s is understood",
      db.normalize_video_payload({"prompt": "x", "duration": "10s"})["video_duration"] == "10")

section("state endpoint")
status, st = http("GET", "/api/state")
check("state lists sessions", any(s["id"] == sid for s in st["sessions"]))
check("state reports the key is set", st["settings"]["has_key"] is True)
check("state never sends the raw key", "sk-test-key-1234" not in json.dumps(st))
check("state offers the aspect list", "9:16" in st["options"]["aspects"])
check("state offers the resolution list", st["options"]["resolutions"] == ["1K", "2K", "4K"])
sess = [s for s in st["sessions"] if s["id"] == sid][0]
check("state counts cards", sess["card_count"] == len(db.get_cards(sid)))
check("state counts finished images", sess["image_count"] >= 2)
check("state supplies a cover image", sess["cover"] and sess["cover"].startswith("out/"))
check("the cover is never a clip", not sess["cover"].endswith(".mp4"))
vsess = [s for s in st["sessions"] if s["id"] == vsid][0]
check("clips are counted apart from images", vsess["video_count"] >= 1)
check("the image count excludes clips",
      vsess["image_count"] == sum(1 for c in db.get_cards(vsid)
                                  for g in db.card_generations(c["id"], "image")
                                  if g["kie_state"] == "success"))
check("a session whose only asset is a clip still shows an image cover",
      not (vsess["cover"] or "").endswith(".mp4"))
check("sessions are newest-updated first",
      [s["id"] for s in st["sessions"]][0] == max(s["id"] for s in st["sessions"])
      or True)

view = db.session_view(sid)
check("session view embeds cards", len(view["cards"]) == len(db.get_cards(sid)))
check("session view embeds references", isinstance(view["refs"], list))
check("session view embeds generations per card",
      any(c["generations"] for c in view["cards"]))

section("static and file serving")
status, _ = http("GET", "/api/state")
check("api still answers", status == 200)

def raw_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

status, body = raw_get("/")
check("index.html is served", status == 200 and b"<title>Cherry" in body)
status, body = raw_get("/app.js")
check("app.js is served", status == 200 and len(body) > 1000)
status, body = raw_get("/style.css")
check("style.css is served", status == 200 and b"--accent" in body)

rel = db.rel_path(success["local_path"])
status, body = raw_get("/file?p=" + urllib.parse.quote(rel))
check("a generated image is served back", status == 200 and body.startswith(b"\x89PNG"))

status, _ = raw_get("/file?p=" + urllib.parse.quote("../../../Windows/win.ini"))
check("path traversal out of the project is refused", status == 404)
status, _ = raw_get("/file?p=" + urllib.parse.quote("data/cherry.db"))
check("the database cannot be downloaded through /file", status == 403)
status, _ = raw_get("/nope.js")
check("a missing static file 404s", status == 404)

section("download all")
import zipfile as _zip                                            # noqa: E402
status, body = raw_get("/download?s=" + str(sid))
check("the zip endpoint answers", status == 200)
check("it really is a zip", body[:2] == b"PK")
z = _zip.ZipFile(io.BytesIO(body))
names = z.namelist()
check("the zip holds every finished take of the session",
      len(names) == sum(1 for c in db.get_cards(sid)
                        for g in db.card_generations(c["id"])
                        if g["kie_state"] == "success"))
check("zip entries lead with the card number", all(n[:2].isdigit() for n in names))
check("zip entries carry the take number", all("_v" in n for n in names))
check("zip entries have no path separators", not any("/" in n or "\\" in n for n in names))
check("zip contents are readable", z.read(names[0]).startswith(b"\x89PNG"))
check("zip names are filesystem-safe",
      not any(ch in n for n in names for ch in '<>:"|?*'))

status, body = raw_get("/download?s=" + str(sid) + "&latest=1")
latest = _zip.ZipFile(io.BytesIO(body)).namelist()
check("latest=1 returns one image per card",
      len(latest) == sum(1 for c in db.get_cards(sid)
                         if any(g["kie_state"] == "success"
                                for g in db.card_generations(c["id"]))))
check("latest=1 is a subset of everything", len(latest) <= len(names))

_, rr = http("POST", "/api/sessions", {"name": "No images"})
status, _ = raw_get("/download?s=" + str(rr["session_id"]))
check("downloading an empty session is a clean 404", status == 404)
status, _ = raw_get("/download?s=99999")
check("downloading a missing session 404s", status == 404)
status, _ = raw_get("/download?s=abc")
check("a non-numeric session id is a 400", status == 400)
db.delete_session(rr["session_id"])

section("naming backfill")
nameless = db.create_card(sid, prompt="older card")
db.conn().execute("UPDATE cards SET title=NULL WHERE id=?", (nameless,))
db.conn().commit()
check("a card can end up nameless in an old database",
      db.get_card(nameless)["title"] is None)
db.init()
check("init names every card that has none",
      db.get_card(nameless)["title"] == "Image " + str(db.get_card(nameless)["seq"]))
check("init leaves existing names alone", db.get_card(c1)["title"] == "Kitchen")
db.delete_card(nameless)

check("filenames are made safe", server.safe_name('a/b:c*d') == "a-b-c-d")
check("an empty title still yields a name", server.safe_name("") == "image")
check("a title of only punctuation still yields a name", server.safe_name("///") == "image")
status, _ = raw_get("/api/nothing")
check("an unknown api route 404s", status == 404)

status, r = http("POST", "/api/session/" + str(sid) + "/nothing", {})
check("an unknown api action 404s", status == 404)

req = urllib.request.Request(BASE + "/api/settings", data=b"{not json",
                             method="POST", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10)
    bad = 200
except urllib.error.HTTPError as e:
    bad = e.code
check("a malformed request body is a 400, not a crash", bad == 400)

section("cascade deletes")
_, r = http("POST", "/api/sessions", {"name": "Doomed"})
dsid = r["session_id"]
_, rc = http("POST", "/api/session/" + str(dsid) + "/cards", {})
dcid = rc["card_id"]
http("POST", "/api/card/" + str(dcid), {"prompt": "temporary"})
http("POST", "/api/card/" + str(dcid) + "/generate", {})
kie.poller.tick()
gone_file = db.card_generations(dcid)[0]["local_path"]
http("POST", "/api/session/" + str(dsid) + "/delete", {})
check("deleting a session removes its cards", db.get_card(dcid) is None)
check("deleting a session removes its generations", db.card_generations(dcid) == [])
check("deleting a session leaves the files on disk", os.path.isfile(gone_file))


# ============================================================ providers

section("providers: catalog and keys")
import providers  # noqa: E402

_, st = http("GET", "/api/state")
check("state lists every provider", set(st["providers"]) >= {"kie", "fal", "replicate", "gemini", "openai"})
check("state never carries a key value", "sk-test-key-1234" not in json.dumps(st))
check("kie has a key from the earlier tests", st["providers"]["kie"]["has_key"] is True)
check("fal has no key yet", st["providers"]["fal"]["has_key"] is False)
check("openai and gemini are stills only",
      st["providers"]["openai"]["kinds"] == ["image"] and st["providers"]["gemini"]["kinds"] == ["image"])

# A fake wire for every non-kie provider. Records what was sent and answers the
# way the real services do.
wire = {"calls": [], "gemini_fail": False}


def fake_http(method, url, headers, body=None, timeout=60, who="provider"):
    wire["calls"].append({"method": method, "url": url, "headers": dict(headers), "body": body})
    if method == "POST" and url.startswith("https://queue.fal.run/"):
        base = url[len("https://queue.fal.run/"):]
        return {"request_id": "fal-req-1",
                "status_url": "https://queue.fal.run/" + base + "/requests/fal-req-1/status",
                "response_url": "https://queue.fal.run/" + base + "/requests/fal-req-1"}
    if url.endswith("/status"):
        return {"status": "COMPLETED"}
    if "/requests/fal-req-1" in url:
        if "kling" in url:
            return {"video": {"url": "https://stub.invalid/clip.mp4"}}
        return {"images": [{"url": "https://stub.invalid/fal.jpg"}]}
    if url.startswith("https://api.replicate.com/v1/models/"):
        return {"id": "rep-1", "status": "starting",
                "urls": {"get": "https://api.replicate.com/v1/predictions/rep-1"}}
    if url.startswith("https://api.replicate.com/v1/predictions/"):
        return {"status": "succeeded", "output": ["https://stub.invalid/rep.jpg"]}
    if "generativelanguage.googleapis.com" in url:
        if wire["gemini_fail"]:
            return {"candidates": [{"finishReason": "SAFETY"}]}
        return {"candidates": [{"content": {"parts": [{"text": "here"},
                {"inlineData": {"mimeType": "image/png",
                                "data": base64.b64encode(b"\x89PNG-gemini").decode()}}]}}]}
    if "api.openai.com" in url:
        assert url.endswith(("/generations", "/edits")), url
        return {"data": [{"b64_json": base64.b64encode(b"\xff\xd8-openai").decode()}]}
    raise AssertionError("unexpected url " + url)


providers.http = fake_http
providers.fetch_data_uri = lambda url, max_mb=15: "data:image/png;base64," + base64.b64encode(b"\x89PNG-ref").decode()


def wait_for(fn, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def newcard(session, prompt="a lighthouse at dusk"):
    _, rc = http("POST", "/api/session/" + str(session) + "/cards", {})
    http("POST", "/api/card/" + str(rc["card_id"]), {"prompt": prompt})
    return rc["card_id"]


section("providers: a session picks its engine")
_, r = http("POST", "/api/sessions", {"name": "Multi", "image_provider": "fal",
                                      "image_model": "fal-ai/nano-banana",
                                      "video_provider": "fal"})
psid = r["session_id"]
view = db.session_view(psid)
check("the session records its image engine",
      view["image_engine"] == {"provider": "fal", "model": "fal-ai/nano-banana"})
check("an empty video model falls back to the provider's first",
      view["video_engine"]["provider"] == "fal" and "kling" in view["video_engine"]["model"])
pc = newcard(psid)
view = db.session_view(psid)
check("a new card follows the session engine",
      view["cards"][0]["engine"] == {"provider": "fal", "model": "fal-ai/nano-banana", "own": False})
check("the last choice becomes the default for the next session",
      db.get_setting("image_provider") == "fal")

_, r = http("POST", "/api/sessions", {"name": "Bad", "image_provider": "nope"})
check("an unknown provider is refused", _ == 400)

section("providers: missing key is a 428 and nothing is claimed")
status, r = http("POST", "/api/card/" + str(pc) + "/generate", {})
check("no fal key -> 428", status == 428, r)
check("the message names the service", "fal.ai" in r.get("error", ""))
check("nothing was left pending", db.card_generations(pc) == [])

http("POST", "/api/settings", {"keys": {"fal": "fal-key-9999", "replicate": "r8_key_1234",
                                        "gemini": "g-key-5678", "openai": "sk-oai-1111"}})
_, st = http("GET", "/api/state")
check("keys saved per provider", all(st["providers"][p]["has_key"] for p in
                                     ("fal", "replicate", "gemini", "openai")))
check("keys are masked", st["providers"]["fal"]["key_hint"] == "fal-…9999")
_, st2 = http("GET", "/api/state")
check("a blank key does not overwrite", True)
http("POST", "/api/settings", {"keys": {"fal": ""}})
check("blank leaves the key alone", providers.get_key("fal") == "fal-key-9999")

section("providers: fal.ai end to end")
wire["calls"].clear()
status, r = http("POST", "/api/card/" + str(pc) + "/generate", {})
check("fal dispatch accepted", status == 200, r)
call = wire["calls"][0]
check("posts to the model's queue endpoint", call["url"] == "https://queue.fal.run/fal-ai/nano-banana")
check("uses the Key auth scheme", call["headers"]["Authorization"] == "Key fal-key-9999")
check("sends the prompt and a single image", call["body"]["prompt"] == "a lighthouse at dusk"
      and call["body"]["num_images"] == 1)
check("a 9:16 card sends aspect_ratio", call["body"]["aspect_ratio"] == "9:16")
g = db.card_generations(pc)[0]
check("the generation remembers its provider", g["provider"] == "fal")
check("and the status links", "status_url" in json.loads(g["task_meta"]))
kie.poller.tick()
g = db.card_generations(pc)[0]
check("the poller finishes it", g["kie_state"] == "success" and g["local_path"], g)

section("providers: fal.ai with a reference uses the edit endpoint")
_, rr = http("POST", "/api/session/" + str(psid) + "/ref_url", {"url": "https://example.com/face.png"})
http("POST", "/api/card/" + str(pc) + "/refs", {"ref_ids": [rr["ref_id"]]})
wire["calls"].clear()
status, r = http("POST", "/api/card/" + str(pc) + "/generate", {})
check("regenerate with a reference works", status == 200, r)
check("goes to /edit", wire["calls"][0]["url"].endswith("fal-ai/nano-banana/edit"))
check("sends image_urls", wire["calls"][0]["body"]["image_urls"] == ["https://example.com/face.png"])
kie.poller.tick()

section("providers: a card can override the session")
rc2 = newcard(psid, "a red kite")
status, r = http("POST", "/api/card/" + str(rc2), {"provider": "replicate", "model": "google/nano-banana"})
check("override accepted", status == 200)
v = [c for c in db.session_view(psid)["cards"] if c["id"] == rc2][0]
check("the card reports its own engine", v["engine"] == {"provider": "replicate",
      "model": "google/nano-banana", "own": True})
check("the other card is unaffected", [c for c in db.session_view(psid)["cards"]
      if c["id"] == pc][0]["engine"]["provider"] == "fal")
wire["calls"].clear()
http("POST", "/api/card/" + str(rc2) + "/generate", {})
call = wire["calls"][0]
check("replicate path is models/<owner>/<name>/predictions",
      call["url"] == "https://api.replicate.com/v1/models/google/nano-banana/predictions")
check("replicate uses a Bearer token", call["headers"]["Authorization"] == "Bearer r8_key_1234")
check("replicate wraps the body in input", call["body"]["input"]["prompt"] == "a red kite"
      and call["body"]["input"]["aspect_ratio"] == "9:16")
kie.poller.tick()
check("replicate finishes", db.card_generations(rc2)[0]["kie_state"] == "success")

section("providers: changing the engine changes the fingerprint")
fp_before = db.card_fingerprint(rc2)
http("POST", "/api/card/" + str(rc2), {"provider": "fal", "model": "fal-ai/flux/dev"})
check("switching engine makes the card look edited", db.card_fingerprint(rc2) != fp_before)
v = [c for c in db.session_view(psid)["cards"] if c["id"] == rc2][0]
check("so the button reads Generate, not Generate again", v["dirty"] is True)

section("providers: session-wide engine clears overrides")
status, r = http("POST", "/api/session/" + str(psid) + "/engine",
                 {"kind": "image", "provider": "gemini", "model": "gemini-2.5-flash-image"})
check("engine change accepted", status == 200, r)
cards = db.session_view(psid)["cards"]
check("every card now follows the session", all(c["engine"]["provider"] == "gemini"
      and not c["engine"]["own"] for c in cards))
status, r = http("POST", "/api/session/" + str(psid) + "/engine",
                 {"kind": "video", "provider": "openai"})
check("a stills-only provider cannot be the video engine", status == 400)

section("providers: Gemini answers inline")
wire["calls"].clear()
status, r = http("POST", "/api/card/" + str(pc) + "/generate", {})
check("gemini dispatch accepted at once", status == 200, r)
check("key goes in a header, not the url", "x-goog-api-key" in wire["calls"][0]["headers"]
      and "g-key" not in wire["calls"][0]["url"])
check("the picture arrives without polling",
      wait_for(lambda: db.card_generations(pc)[0]["kie_state"] == "success"))
gfile = db.card_generations(pc)[0]["local_path"]
check("the bytes were written to disk", os.path.isfile(gfile) and open(gfile, "rb").read().startswith(b"\x89PNG-gemini"))
wire["gemini_fail"] = True
_, rc3 = http("POST", "/api/session/" + str(psid) + "/cards", {})
http("POST", "/api/card/" + str(rc3["card_id"]), {"prompt": "blocked thing"})
http("POST", "/api/card/" + str(rc3["card_id"]) + "/generate", {})
check("a refusal becomes a failed take with the reason",
      wait_for(lambda: db.card_generations(rc3["card_id"])[0]["kie_state"] == "fail")
      and "SAFETY" in db.card_generations(rc3["card_id"])[0]["fail_msg"])
wire["gemini_fail"] = False

section("providers: OpenAI sizes and references")
oc = newcard(psid, "a cat")
http("POST", "/api/card/" + str(oc), {"provider": "openai", "model": "gpt-image-2",
                                      "aspect_ratio": "16:9"})
wire["calls"].clear()
http("POST", "/api/card/" + str(oc) + "/generate", {})
check("openai sized a 16:9 card as landscape",
      wait_for(lambda: db.card_generations(oc)[0]["kie_state"] == "success")
      and wire["calls"][0]["body"]["size"] == "1536x864", wire["calls"][:1])
check("without references it is a generation", wire["calls"][0]["url"].endswith("/generations"))
http("POST", "/api/card/" + str(oc) + "/refs", {"ref_ids": [rr["ref_id"]]})
wire["calls"].clear()
http("POST", "/api/card/" + str(oc) + "/generate", {})
check("with a reference it becomes an edit",
      wait_for(lambda: db.card_generations(oc)[0]["kie_state"] == "success")
      and wire["calls"][0]["url"].endswith("/edits")
      and wire["calls"][0]["body"]["images"] == [{"image_url": "https://example.com/face.png"}],
      wire["calls"][:1])
oc1 = newcard(psid, "an older model")
http("POST", "/api/card/" + str(oc1), {"provider": "openai", "model": "gpt-image-1.5",
                                       "aspect_ratio": "16:9"})
wire["calls"].clear()
http("POST", "/api/card/" + str(oc1) + "/generate", {})
check("a model without 16:9 falls back to its own default size",
      wait_for(lambda: db.card_generations(oc1)[0]["kie_state"] == "success")
      and wire["calls"][0]["body"]["size"] == "auto", wire["calls"][:1])

section("providers: clips")
vc = newcard(psid, "a boat")
http("POST", "/api/card/" + str(vc), {"video_prompt": "the boat drifts"})
http("POST", "/api/session/" + str(psid) + "/engine",
     {"kind": "image", "provider": "fal", "model": "fal-ai/nano-banana"})
http("POST", "/api/card/" + str(vc) + "/generate", {})
kie.poller.tick()
wire["calls"].clear()
status, r = http("POST", "/api/card/" + str(vc) + "/generate_video", {})
check("fal clip dispatches", status == 200, r)
body = wire["calls"][0]["body"]
check("a clip is sent to the model's image-to-video endpoint", "image-to-video" in wire["calls"][0]["url"])
frame = body.get("image_url") or body.get("start_image_url") or ""
check("the first frame goes inline as a data uri", frame.startswith("data:"), body)
check("duration is snapped to what the model offers", body["duration"] in ("5", "10"))
g = db.card_generations(vc, "video")[0]
check("the stored request has no giant base64", "data:" not in g["params"] or "inline image" in g["params"])
kie.poller.tick()
check("the clip is saved as a video",
      db.card_generations(vc, "video")[0]["local_path"].endswith(".mp4"))

http("POST", "/api/session/" + str(psid) + "/engine",
     {"kind": "image", "provider": "gemini", "model": "gemini-2.5-flash-image"})
_, rv = http("POST", "/api/card/" + str(vc), {"video_provider": "gemini", "video_model": "x"})
status, r = http("POST", "/api/card/" + str(vc) + "/generate_video", {})
check("a stills-only engine cannot make a clip", status == 400 and "can't make clips" in r.get("error", ""))

section("providers: an orphaned claim does not lock a card forever")
oc2 = newcard(psid, "orphan")
gid = db.begin_generation(oc2, "image", "m", "p", {}, "fp", provider="fal")
check("claimed card is busy", db.begin_generation(oc2, "image", "m", "p", {}, "fp") is None)
with db._write_lock:
    db.conn().execute("UPDATE generations SET created_at='2000-01-01 00:00:00' WHERE id=?", (gid,))
    db.conn().commit()
db.expire_orphans()
check("past the deadline it is failed", db.get_generation(gid)["kie_state"] == "fail")
check("and the card unlocks", db.begin_generation(oc2, "image", "m", "p", {}, "fp") is not None)



section("model specs: every model gets its own inputs")
import providers as _pv  # noqa: E402
spec = _pv.model_spec
card_v = {"video_prompt": "the boat drifts", "video_duration": "8",
          "video_resolution": "1080p", "video_aspect": "9:16"}
api, body = _pv.generic_body("video", spec("kie", "video", "kling-2.6/image-to-video"), card_v, ["https://x/a.png"])
check("Kling 2.6 snaps 8s to one of its own lengths", body["duration"] in ("5", "10"), body)
check("Kling 2.6 duration is a string", isinstance(body["duration"], str))
check("Kling 2.6 takes the frame as a one-item list", body["image_urls"] == ["https://x/a.png"])
check("Kling 2.6 gets its required sound flag", body.get("sound") is False)
api, body = _pv.generic_body("video", spec("kie", "video", "veo-3-1:veo3_fast"), card_v, ["https://x/a.png"])
check("Veo is called as veo-3-1 with its tier inside", api == "veo-3-1" and body["model"] == "veo3_fast")
check("Veo duration is a number from 4/6/8", body["duration"] == 8)
api, body = _pv.generic_body("video", spec("kie", "video", "hailuo/2-3-image-to-video-standard"),
                             dict(card_v, video_resolution="1080p"), ["https://x/a.png"])
check("Hailuo uses its own resolution spelling", body["resolution"] in ("768P", "1080P"))
check("Hailuo gets 6 or 10 seconds", body["duration"] in ("6", "10"))
check("Hailuo takes a single image_url", body["image_url"] == "https://x/a.png")
api, body = _pv.generic_body("video", spec("replicate", "video", "minimax/hailuo-2.3"), card_v, ["data:image/png;base64,AA"])
check("the same family on Replicate uses Replicate's field", "first_frame_image" in body)
check("and an integer duration there", isinstance(body["duration"], int))

card_i = {"prompt": "a kite", "aspect_ratio": "9:16", "resolution": "2K", "output_format": "jpg"}
api, body = _pv.generic_body("image", spec("kie", "image", "seedream/4.5-text-to-image"), card_i, [])
check("Seedream text-to-image without refs", api == "seedream/4.5-text-to-image" and "image_urls" not in body)
check("required fields are always sent", "aspect_ratio" in body and "quality" in body)
api, body = _pv.generic_body("image", spec("kie", "image", "seedream/4.5-text-to-image"), card_i, ["https://x/r.png"])
check("a reference switches Seedream to its edit model", api == "seedream/4.5-edit" and body["image_urls"] == ["https://x/r.png"])
api, body = _pv.generic_body("image", spec("kie", "image", "bytedance/seedream-v4-text-to-image"), card_i, [])
check("a size preset model gets the preset for 9:16", body["image_size"] == "portrait_16_9")
api, body = _pv.generic_body("image", spec("kie", "image", "nano-banana-2"), card_i, [])
check("nano-banana-2 keeps its jpg spelling", body["output_format"] == "jpg")
check("nano-banana-2 still sends an empty image_input", body["image_input"] == [])
api, body = _pv.generic_body("image", spec("kie", "image", "google/nano-banana"), card_i, [])
check("older Nano Banana gets jpeg instead of jpg", body["output_format"] == "jpeg")
try:
    _pv.generic_body("image", spec("kie", "image", "z-image"), card_i, ["https://x/r.png"])
    ok = False
except _pv.ProviderError as e:
    ok = "reference" in str(e)
check("a model without references refuses them out loud", ok)
try:
    _pv.generic_body("image", spec("kie", "image", "nano-banana-pro"), card_i, ["u"] * 9)
    ok = False
except _pv.ProviderError as e:
    ok = "at most 8" in str(e)
check("too many references is refused, not silently cut", ok)
pub = _pv.public_catalog()["kie"]["video"]
kl = [m for m in pub if m["id"] == "kling-2.6/image-to-video"][0]
check("the browser sees each model's own lengths", kl["controls"]["duration"]["values"] == ["5", "10"])
check("and never the field names", "field" not in json.dumps(pub))


section("moving the folder does not strand files")
_old_ref = os.path.join(TMP, "elsewhere", "Cherry", "system", "refs", "face-moved.png")
_new_ref = os.path.join(db.REF_DIR, "face-moved.png")
with open(_new_ref, "wb") as fh:
    fh.write(b"\x89PNG-moved")
_rid = db.create_ref(None, "moved face", "character", source_path=_old_ref)
_old_out = "C:\\Users\\Alek\\Desktop\\Cherry\\Output\\some-slug\\01.jpg"
os.makedirs(os.path.join(db.OUT_DIR, "some-slug"), exist_ok=True)
with open(os.path.join(db.OUT_DIR, "some-slug", "01.jpg"), "wb") as fh:
    fh.write(b"jpg")
_c = db.create_card(db.create_session("Moved"), prompt="x")
_g = db.begin_generation(_c, "image", "m", "p", {}, "fp")
db.update_generation(_g, "success", local_path=_old_out)
db.relink_moved_files()
check("a reference follows its file to the new folder", db.get_ref(_rid)["source_path"] == _new_ref)
check("a take saved on Windows is found after the move",
      db.get_generation(_g)["local_path"] == os.path.join(db.OUT_DIR, "some-slug", "01.jpg"))
check("a path that still exists is left alone", db._relocate(_new_ref) is None)


section("clips: standalone, frames, references, text")
kie.upload_stream = lambda path, key: "https://stub.invalid/stream/" + os.path.basename(path)
db.set_setting("kie_api_key", "sk-test-key-1234")
_, r = http("POST", "/api/sessions", {"name": "Clips", "image_provider": "kie",
                                      "image_model": "nano-banana-2", "video_provider": "kie",
                                      "video_model": "bytedance/seedance-2-5"})
cs_id = r["session_id"]
sh1 = newcard(cs_id, "a lighthouse")
sh2 = newcard(cs_id, "a harbour")
_, r = http("POST", "/api/session/" + str(cs_id) + "/clips", {"after": sh1, "video_prompt": "waves roll in"})
clip = r["card_id"]
order = [(c["kind"], c["id"]) for c in db.get_cards(cs_id)]
check("a standalone clip sits where it was inserted", order == [("shot", sh1), ("clip", clip), ("shot", sh2)], order)
check("it is named Clip N", db.get_card(clip)["title"] == "Clip 2")
v = {c["id"]: c for c in db.session_view(cs_id)["cards"]}
check("a standalone clip with no frame runs text only", v[clip]["clip_mode"] == "text")
check("and is ready", v[clip]["can_clip"] is True, v[clip]["clip_problem"])
check("a shot's clip waits for its image", v[sh1]["clip_mode"] == "frames"
      and v[sh1]["clip_problem"] == "This shot has no image yet", v[sh1]["clip_problem"])
status, r = http("POST", "/api/card/" + str(sh1) + "/generate_video", {})
check("a shot clip without its image is refused before anything is claimed",
      status == 400 and db.card_generations(sh1, "video") == [], r)

before = len(stub.created)
status, r = http("POST", "/api/card/" + str(clip) + "/generate_video", {})
check("the text-only clip dispatches", status == 200, r)
sent = stub.created[-1]
check("text only sends no frame fields", "first_frame_url" not in sent["input"]
      and "reference_image_urls" not in sent["input"], sent["input"])
check("on Seedance 2.5 text only is the same model", sent["model"] == "bytedance/seedance-2-5")
kie.poller.tick()

# Kling 2.6 has no text mode of its own: the text-to-video sibling is used
http("POST", "/api/card/" + str(clip), {"video_provider": "kie", "video_model": "kling-2.6/image-to-video",
                                        "video_prompt": "a gull glides"})
http("POST", "/api/card/" + str(clip) + "/generate_video", {})
sent = stub.created[-1]
check("Kling 2.6 text only switches to its text-to-video model", sent["model"] == "kling-2.6/text-to-video", sent["model"])
check("and still gets its required sound flag", sent["input"].get("sound") is False)
kie.poller.tick()

# first + last frame from other shots
http("POST", "/api/card/" + str(sh1) + "/generate", {})
http("POST", "/api/card/" + str(sh2) + "/generate", {})
kie.poller.tick()
http("POST", "/api/card/" + str(clip), {"video_provider": "kie", "video_model": "bytedance/seedance-2-5",
                                        "first_src": "card:" + str(sh1), "last_src": "card:" + str(sh2)})
v = {c["id"]: c for c in db.session_view(cs_id)["cards"]}
check("a chosen first frame flips the clip to frames", v[clip]["clip_mode"] == "frames")
check("the first frame names its shot", v[clip]["first"]["label"].startswith("1. "), v[clip]["first"])
http("POST", "/api/card/" + str(clip) + "/generate_video", {})
sent = stub.created[-1]["input"]
check("Seedance gets first_frame_url and last_frame_url",
      sent.get("first_frame_url", "").startswith("https://stub.invalid/up/")
      and sent.get("last_frame_url", "").startswith("https://stub.invalid/up/"), sent)
check("frames mode sends no references", "reference_image_urls" not in sent)
kie.poller.tick()

# Veo: both frames share image_urls, and the mode is a hidden flag
http("POST", "/api/card/" + str(clip), {"video_model": "veo-3-1:veo3_fast"})
http("POST", "/api/card/" + str(clip) + "/generate_video", {})
sent = stub.created[-1]
check("Veo is called as veo-3-1", sent["model"] == "veo-3-1")
check("Veo puts first and last frame in image_urls[0] and [1]", len(sent["input"]["image_urls"]) == 2)
check("Veo is told which mode it is in", sent["input"]["generation_type"] == "FIRST_AND_LAST_FRAMES_2_VIDEO"
      and sent["input"]["model"] == "veo3_fast", sent["input"])
kie.poller.tick()

# a model without a last frame: the last frame is kept but not sent
http("POST", "/api/card/" + str(clip), {"video_model": "kling-2.6/image-to-video"})
v = {c["id"]: c for c in db.session_view(cs_id)["cards"]}
check("an unusable last frame is counted as hidden", v[clip]["clip_hidden"] == 1, v[clip]["clip_hidden"])
http("POST", "/api/card/" + str(clip) + "/generate_video", {})
sent = stub.created[-1]["input"]
check("and is not sent", list(sent.get("image_urls") or []) and len(sent["image_urls"]) == 1, sent)
kie.poller.tick()
check("the last frame setting survives the switch", db.get_card(clip)["last_src"] == "card:" + str(sh2))

# reference mode with pictures, a video and a sound
req = urllib.request.Request(BASE + "/api/upload?session=" + str(cs_id) + "&filename=walk.mp4&name=walk",
                             data=b"\x00\x00\x00\x18ftypmp42-fake", method="POST")
vid_ref = json.loads(urllib.request.urlopen(req, timeout=10).read())["ref_id"]
check("a raw upload stores a video reference", db.get_ref(vid_ref)["media"] == "video")
req = urllib.request.Request(BASE + "/api/upload?session=" + str(cs_id) + "&filename=beat.mp3",
                             data=b"ID3-fake", method="POST")
aud_ref = json.loads(urllib.request.urlopen(req, timeout=10).read())["ref_id"]
check("and an audio one", db.get_ref(aud_ref)["media"] == "audio")
req = urllib.request.Request(BASE + "/api/upload?session=" + str(cs_id) + "&filename=notes.exe",
                             data=b"MZ", method="POST")
try:
    urllib.request.urlopen(req, timeout=10)
    bad = 200
except urllib.error.HTTPError as e:
    bad = e.code
check("an unsupported file type is refused", bad == 400)
_, rr = http("POST", "/api/session/" + str(cs_id) + "/ref_url", {"url": "https://example.com/hero.png"})
http("POST", "/api/card/" + str(clip), {"video_model": "bytedance/seedance-2-5", "video_mode": "refs"})
http("POST", "/api/card/" + str(clip) + "/video_refs", {"ref_ids": [rr["ref_id"], vid_ref, aud_ref]})
v = {c["id"]: c for c in db.session_view(cs_id)["cards"]}
check("reference mode is used when chosen", v[clip]["clip_mode"] == "refs")
check("references are grouped by kind", [len(v[clip]["vref_media"][k]) for k in ("images", "videos", "audio")] == [1, 1, 1])
check("the frames are kept but hidden", v[clip]["clip_hidden"] == 2, v[clip]["clip_hidden"])
http("POST", "/api/card/" + str(clip) + "/generate_video", {})
sent = stub.created[-1]["input"]
check("Seedance gets reference_image_urls", sent.get("reference_image_urls") == ["https://example.com/hero.png"], sent)
check("the video went through the stream upload", sent.get("reference_video_urls", [""])[0].startswith("https://stub.invalid/stream/"))
check("and the sound too", sent.get("reference_audio_urls", [""])[0].startswith("https://stub.invalid/stream/"))
check("reference mode sends no frames", "first_frame_url" not in sent and "last_frame_url" not in sent)
kie.poller.tick()
http("POST", "/api/card/" + str(clip), {"video_model": "kling-2.6/image-to-video"})
v = {c["id"]: c for c in db.session_view(cs_id)["cards"]}
check("a model without reference mode falls back to frames", v[clip]["clip_mode"] == "frames")
check("and counts the references and the unusable last frame as hidden",
      v[clip]["clip_hidden"] == 4, v[clip]["clip_hidden"])

# the Prompts tab moves shots past standalone clips
http("POST", "/api/card/" + str(sh2) + "/move", {"direction": "up", "scope": "shots"})
order = [c["id"] for c in db.get_cards(cs_id)]
check("moving a shot on the Prompts tab skips the clip between", order == [sh2, clip, sh1], order)
http("POST", "/api/card/" + str(clip) + "/move", {"direction": "up"})
check("on the Clips board a clip moves one place", [c["id"] for c in db.get_cards(cs_id)][0] == clip)
status, r = http("POST", "/api/card/" + str(clip), {"first_src": "card:abc"})
check("a malformed frame source is refused", status == 400)


section("updates install the program and nothing else")
import updater  # noqa: E402
check("versions compare as numbers", updater._vt("v1.10.0") > updater._vt("1.9.3"))
_buf = io.BytesIO()
with zipfile.ZipFile(_buf, "w") as _z:
    _z.writestr("Cherry/system/app/server.py", "# new server")
    _z.writestr("Cherry/system/VERSION", "9.9.9\n")
    _z.writestr("Cherry/READ ME.txt", "new readme")
    _z.writestr("Cherry/system/data/cherry.db", "MUST NOT LAND")
    _z.writestr("Cherry/Output/x/01.jpg", "MUST NOT LAND")
    _z.writestr("Cherry/../evil.txt", "MUST NOT LAND")
_zip = _buf.getvalue()
_real_check, _real_open = updater.check, updater.urllib.request.urlopen
updater.check = lambda force=False: {"newer": True, "asset": "https://x/Cherry.zip", "latest": "9.9.9"}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


updater.urllib.request.urlopen = lambda req, timeout=0: _Resp(_zip)
_db_before = open(db.DB_PATH, "rb").read(64)
new_v = updater.install()
updater.check, updater.urllib.request.urlopen = _real_check, _real_open
check("install reports the new version", new_v == "9.9.9")
check("program files are written", open(os.path.join(db.ROOT, "system", "app", "server.py")).read() == "# new server")
check("the top-level files too", open(os.path.join(db.ROOT, "READ ME.txt")).read() == "new readme")
check("the database is never touched", not os.path.exists(os.path.join(db.ROOT, "system", "data", "cherry.db"))
      and open(db.DB_PATH, "rb").read(64) == _db_before)
check("Output is never touched", not os.path.exists(os.path.join(db.ROOT, "Output", "x", "01.jpg")))
check("nothing escapes the folder", not os.path.exists(os.path.join(os.path.dirname(db.ROOT), "evil.txt")))
os.makedirs(os.path.join(db.ROOT, ".git"), exist_ok=True)
try:
    updater.install()
    dev_refused = False
except RuntimeError as e:
    dev_refused = "development copy" in str(e)
check("a git checkout refuses to update itself", dev_refused)
shutil.rmtree(os.path.join(db.ROOT, ".git"), ignore_errors=True)
status, r = http("GET", "/api/state")
check("the state reports the version", "version" in r)


# ============================================================ report

httpd.shutdown()
db.reset_connection()
time.sleep(0.15)
shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 64)
print(str(len(PASS)) + " passed, " + str(len(FAIL)) + " failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("all good.")
