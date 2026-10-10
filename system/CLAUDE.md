# Cherry — operating manual for Claude

(The app was called Banana until October 2026; some comments and the
selftest still say so.)

Read this before touching anything here.

---

## Read this first: the two rules that cost money

Everything else in this file is convenience. These two are not.

**1. Nothing may spend before pending edits have landed.** Field edits are
debounced 550ms. Generating inside that window sends the settings that were on
screen a moment ago and bills for the wrong thing. Every client path that
spends — `generate()`, `generateAll()`, `applyToAll()` — awaits `flushSaves()`
first. If you add another, it awaits it too.

**2. Every dispatch is claimed atomically before the network call.**
`db.begin_generation()` does the busy check and the pending insert while holding
`_write_lock`, so two threads racing a double-clicked button cannot both see
"not busy". The loser gets `None`, `kie.dispatch` raises `AlreadyRunning`, and
the route answers 409 — a quiet "already going", not a failure. The claim
happens *before* reference uploads, which take seconds and are exactly the
window a second click arrives in.

The selftest pins both: eight simultaneous clicks produce one task, four racing
`Generate all`s produce six tasks for six cards, and the payload is asserted
field by field against the card. Do not weaken either without reading those.

---

## What this is

A local dashboard for hand-written image prompts. You write a prompt, press
Generate, and it goes to the service you chose for that session (kie.ai, fal.ai,
Replicate, Gemini or OpenAI). Every take is kept.

It is deliberately **one screen deep**: a session holds an ordered list of
prompt cards, and each card holds a prompt plus its render settings. There is no
script parser, no character rig, no style system, no job queue. Those belong to
the sibling app in `Desktop/Vasko`, which generates prompts *for* you. This one
does not: **the user writes every prompt themselves, or pastes them in as JSON.**

That is the whole point. Do not add a prompt-generation stage to this app.

---

## Running it

```bash
cd Cherry/system && python app/server.py
```

Or double-click `Start Cherry.bat` in the Cherry folder. It opens
`http://127.0.0.1:8770/`.

Stdlib only — no pip install, no build step, no node. `CHERRY_PORT` overrides the
port; `CHERRY_NO_BROWSER=1` stops it opening a tab (use this when you start it
yourself, so you do not steal the user's focus). The old `BANANA_*` names still
work.

If port 8770 is already listening, it is already running. Do not start a second
one.

---

## Layout

```
Cherry/                       what the user sees
  Start Cherry.bat            launcher (cd's into system\ and runs the server)
  Make desktop shortcut.bat   runs system\make_shortcut.ps1 - a .lnk holds
                              absolute paths, so it is made on the PC, not shipped
  READ ME.txt                 the short Windows guide
  Output/<session-slug>/      finished images; clips in videos/
  system/                     everything else
    CLAUDE.md  README.md  VERSION  cherry.ico  make_shortcut.ps1
    data/cherry.db            sessions, prompts, takes - and the API keys
    refs/  characters/        reference pictures (the local source of truth)
    app/
      server.py  db.py  kie.py  providers.py  updater.py  models.json  selftest.py
      static/                 index.html, style.css, app.js, prompts.js, img/
    tools/
      build_catalog.py        regenerates app/models.json from tools/research/
      make_release.py         builds Cherry.zip (what the update button installs)
      publish.py              test + version + commit + tag + push + GitHub release
```

`db.py` works out the layout: if the folder above `app/` is called `system`, the
private data lives in `system/` and finished work in the top-level `Output/`;
otherwise everything sits beside `app/` (flat checkout, and the selftest).

**Paths are stored absolute, and folders move** - unzipped on another PC,
renamed, reshuffled. `db.relink_moved_files()` runs on every start and re-roots
any reference or take whose file is missing, by the part of its path after
`Output/`, `out/`, `refs/` or `characters/` (Windows backslashes included). The
selftest pins it.

### Shipping an update (the whole workflow)

The repo is public at **github.com/vase42/cherry-app**. Two users, so the
loop is deliberately small:

1. Change things here, on the Mac. Run `python3 system/app/selftest.py`.
2. `python3 system/tools/publish.py 1.2.0 "what changed"` - it refuses to
   publish if the selftest fails, writes `system/VERSION`, commits, tags
   `v1.2.0`, builds `Cherry.zip`, pushes, and creates the GitHub release with
   the zip attached.
3. Every installed Cherry checks `releases/latest` from the home screen
   (`updater.check()`, cached an hour) and shows **Cherry 1.2.0 is ready -
   Update now**. Installing writes program files only (`updater.KEEP` skips
   `system/data/`, `refs/`, `characters/`, `Output/`) and exits with code 3;
   `Start Cherry.bat` loops on 3, and a marker file stops the restarted server
   opening a second browser tab.

A copy with a `.git` folder (this one) never updates itself - it would
overwrite work in progress with an older release. Version numbers: bump the
last digit for fixes, the middle one for features.

### Sending it to someone new

```bash
python system/tools/make_release.py        # writes Cherry.zip beside the folder
```

The zip holds the program only - never `data/` (it has the keys), `Output/`,
`refs/` or `characters/` - and the script refuses to finish if anything like
that slipped in. Everything is inside one `Cherry/` folder, so unzipping a newer
one over an old one updates the program and leaves the work alone. The
recipient needs Python 3 on PATH (`python` or `py`); nothing else.

---

## The model

Four tables carry everything.

**`sessions`** — a named set of prompts. `slug` names its output folder and is
made unique at creation, so two sessions called "Ad v2" never write into the
same directory.

**`cards`** — one shot or one standalone clip (`kind` = `shot` | `clip`). Holds `prompt`, `title`, `model`, `aspect_ratio`,
`resolution`, `output_format` and a dense 1-based `seq`. The seq is the badge on
the card and the number on the image in the gallery — it is the only thing tying
a prompt to its output visually, so keep it dense: `delete_card` resequences,
and `move_card` swaps. **One running order covers shots and standalone clips**:
`seq` is the place in the final edit, so the Prompts tab (shots only) can read
1, 3, 4 when a standalone clip is number 2 - downloads come out in edit order.
`create_card(after=...)` inserts and shifts; `move_card(scope="shots")` is the
Prompts tab's move, which skips standalone clips.

A new card takes its settings from `session_defaults()` — the *last* card in
that session, falling back to the stored defaults for the first one. Settings no
longer exposes those fields, so a session carries its own look forward instead
of reaching for a global preference.

**`refs` + `card_refs`** — reference images, many-to-many onto cards. Four
kinds: `upload` (a local file we pushed to kie.ai), `url` (the user pasted a
link; we never touch it), `generation` (a finished image of ours, re-used as a
reference), and `character`. Cap is 8 per card — the model's limit.

**A `character` is stored with `session_id` NULL, and that is what makes the
cast global.** `list_refs()` returns a session's own refs *plus* every character,
sorted first. Deleting a session cannot cascade a character away, because the
cascade is on `session_id` and a character has none. The point is that you
upload a face once and it is offered on every video you make afterwards — do not
"fix" this by giving characters a session.

**`generations`** — one row per press of Generate, `kind` being `image` or
`video`. Never updated in place except to advance its own state. This is why
history survives: take 1 is still there when you rewrite the prompt and generate
take 2. `source_gen_id` records which image a clip was made from; `upload_url`
caches the kie.ai copy of our own downloaded image so the clip stage does not
re-upload it every time.

### The two stages on one card

A card carries both: `prompt` + image settings, and `video_prompt` + `video_*`
settings. The Prompts tab edits the first, the Images tab the second. A clip is
always made **from that card's active image**, so the stages stay tied to a shot
number rather than to each other's timing.

`active_image_id` pins one take; NULL means "the newest successful one", which is
why regenerating promotes the new image automatically. `db.active_image()` falls
back to the newest whenever the pin is gone — deleting the pinned take cannot
strand a card. **Shot numbers, never timestamps**: a video prompt belongs to shot
4, so regenerating shot 4's image must not move it.

### The fingerprint

`db.fingerprint()` hashes prompt + model + aspect + resolution + format +
reference identity. It is stored on each generation, and the UI compares the
card's current fingerprint with its newest take:

- **equal** → the button reads **Generate again** (you would be re-rolling)
- **different** → the button reads **Generate** (you edited something)

References are fingerprinted by `source_path` where they have one, not by
`upload_url` — a re-upload mints a fresh url for the same picture and must not
make every card look edited. `db.ref_identity()` is where that lives.

---

## Engines: more than one service

**Every model's inputs are data.** `app/models.json` holds one spec per model
per service: which field the prompt goes in, where reference pictures go (field
name, list or single, max), where a clip's first frame goes, and each control —
`aspect`, `resolution`, `format`, `duration` — with its field name, allowed
values, default and type (`"5"` vs `5`). `providers.generic_body()` builds every
request from that; the UI draws its dropdowns from the same values via
`/api/state`. Never hard-code a model's shape in an adapter again.

`models.json` is generated by `tools/build_catalog.py` from the research JSON in
`tools/research/` (each service's own docs, read 2026-10-09). Change the
script's overrides and re-run it rather than hand-editing the JSON:

```bash
python3 tools/build_catalog.py app/models.json
```

Spec rules worth knowing:

- `id` is what a card stores; `api` is what is sent (Veo tiers are
  `veo-3-1:veo3_fast` with `api: "veo-3-1"` and the tier in `fixed.model`).
- `edit` holds a text-to-image model's sibling edit model. Attaching references
  switches to it (`seedream/4.5-text-to-image` -> `seedream/4.5-edit`).
- A size-preset control (`image_size: "portrait_16_9"`) is shown as ratios and
  translated through its `map`.
- `snap()` in `providers.py` and `ctlValue()` in `app.js` are the same rule —
  exact value, jpg/jpeg as one, nearest length, else the model's default — so
  the dropdown always shows what will be sent. Change one, change both.
- Every control a model has is always sent, because many models reject a
  request missing `aspect_ratio` / `quality` even where the docs show no default.
- Too many references, references on a model that takes none, or none on an
  edit-only model are refusals with a message — never a silent trim.
- Retired models are left out (Imagen on Google's own API, Sora, Veo 3.0,
  dall-e-3); retiring ones carry a `note` the menu shows.
- The first model in a list is the default for a new session on that service;
  `first()` in the build script sets that order. Keep durable models first.

An **adapter** per service does transport only: `submit()` and `poll()`, plus
`ref()` for how a reference picture travels. kie.ai, fal.ai and Replicate are
async; Gemini and OpenAI are `sync` - the picture is in the response, so
`kie._send` runs the call on a thread and the click returns at once.

**The engine is a pair, and the pair travels together.** `sessions` carry
`image_provider/image_model/video_provider/video_model`. A card has its own
`provider/model` (stills) or `video_provider/video_model` (clips) only when you
overrode it; with no provider of its own a card follows its session and any model
string it holds is ignored. `db.effective_engine()` is the one place that
resolves it. `db.set_session_engine()` clears every card's override, so "all"
means all. The engine is part of the fingerprint, so switching it flips a card to
**Generate** instead of **Generate again**.

Money rules still hold, unchanged: `kie.dispatch` claims the card in
`db.begin_generation` before anything else, and `kie._engine_for` raises
`MissingKey` *before* the claim, so a missing key leaves nothing pending (the
route answers 428, naming the provider). Rate limiting is kie.ai's alone.

Things that bite:

| | |
|---|---|
| **`KieError` is `providers.ProviderError`** | One error type for all providers, so routes and the poller catch a single thing. The name is kept for history. |
| **Only kie.ai uploads references** | Everyone else takes the picture inline as a data URI (`Adapter.ref`), so there is no expiring upload to manage. `providers.audit()` swaps those for a placeholder before the request is stored - never put megabytes of base64 in `generations.params`. |
| **Gemini/OpenAI have no task id** | A sync generation stays `pending` with a NULL `kie_task_id`, which the poller ignores. `db.expire_orphans()` fails any such claim older than 15 minutes, or a crash mid-call would lock the card forever. |
| **`generations.provider` + `task_meta`** | The poller must know which service owns a task, and fal.ai hands back status/response URLs that have to be remembered. |
| **Keys** | `settings.kie_api_key` for kie.ai (the original name, kept), `key_<provider>` for the rest. `/api/state` returns `has_key` and a mask per provider, never a value. |
| **Custom model ids** | `model_spec()` copies the provider's first entry with the id swapped in, so an unknown model is sent in the shape that provider's usual ones take. |
| **Not verified live** | fal.ai, Replicate, Gemini and OpenAI payloads follow each service's public docs and are covered by the selftest against stubs, but have not been run with a funded key. If one is rejected, the exact body sent is in the lightbox - fix that adapter's `build()`. |

---

## The JSON importer

`db.extract_objects()` is the most-defended function in the app, because the
user pastes payloads straight out of kie.ai's docs panel and they arrive
mangled. Three passes, each more forgiving:

1. **`json.loads`** — a clean paste. Also unwraps a `{"cards": [...]}` /
   `prompts` / `items` / `shots` / `data` wrapper.
2. **Brace scan** — a string-aware depth counter that pulls every balanced
   `{...}` out of surrounding noise. Handles several payloads pasted back to
   back, and braces inside a prompt string.
3. **Key/value salvage** — for the paste that missed the outer braces entirely,
   so *nothing* in the text is balanced. Collects every `"key": value` it can
   read, ignoring nesting, and starts a new payload each time a key repeats.
   That repeat is exactly where one pasted payload ends and the next begins.

A later pass only wins if it recovers **more prompts** than an earlier one, so a
clean paste is never re-read by the loose parser. Keep that ordering if you
touch it — the selftest pins it.

`normalize_payload()` then folds one object into card fields. It accepts the
nested `{model, input:{...}}` form and the flat form, tolerates camelCase,
normalises `2k`→`2K` and `jpeg`→`jpg`, falls back to the user's defaults for
anything unknown, and folds a `negative_prompt` into the prompt body as an
exclusion clause — nano-banana has no negative field.

An object with no prompt is not a card. It is skipped and counted.

### Plain text is the fourth way in

`split_blocks()` splits on **blank lines**, not on newlines — a single newline
stays inside a prompt, because paragraphs are normal in these. `looks_like_json`
decides which reader runs; the import dialog shows the verdict live as you type,
so there is no mode to get wrong. Forcing `mode: "json"` on prose is still an
error, which is what the API is for.

Prose is therefore no longer an import failure. Only genuinely empty input is.

### image_input is scrubbed, and left out of the example

`_image_urls()` keeps only entries starting `http://` or `https://` and counts
what it threw away; the importer surfaces that count as a warning. A model asked
for this JSON will write `"image_input": ["the character reference photo"]` — a
description, not a url, which kie.ai would treat as a broken reference and bill
you for the failure.

For the same reason `EXAMPLE_JSON` and the shape inside `CHATGPT_PROMPT` carry
**no `image_input` field at all**, and the prompt explicitly tells the model not
to add one. References are attached in the app, where they can be uploaded and
kept alive. Do not put the field back into either constant.

---

## The kie.ai contract

Image payload, and the only shape this app sends:

```json
{
  "model": "nano-banana-2",
  "input": {
    "prompt": "…",
    "image_input": [],
    "aspect_ratio": "9:16",
    "resolution": "1K",
    "output_format": "jpg"
  }
}
```

`callBackUrl` is deliberately **not** sent — a local app has no public callback
url, so results are polled instead.

- `POST /api/v1/jobs/createTask` → `{"code":200,"data":{"taskId":"…"}}`
- `GET  /api/v1/jobs/recordInfo?taskId=…` → `data.state` in
  `waiting|queuing|generating|success|fail`; `data.resultJson` is a JSON
  **string** containing `{"resultUrls":[…]}`.

Things that will bite you:

| | |
|---|---|
| **Rate limit** | 20 new tasks per 10s, rejected outright — no server queue. Every dispatch goes through the token bucket at 18/10s. |
| **Result urls expire** | The poller downloads each asset the moment it succeeds. Never hand a kie.ai result url to the UI as the long-term source. |
| **Uploads expire after 3 days** | `kie.ensure_upload()` re-sends anything older than 2.5 days before it is used. The local file in `refs/` is the source of truth. |
| **Uploads use a different host** | `kieai.redpandaai.co`, not `api.kie.ai`, and it is behind Cloudflare — the request must send a real browser User-Agent or it gets a 1010. |
| **9MB upload ceiling** | Rejected before it is sent, with a message the user can act on. |
| **`code: 402`** | Out of credits. Surfaced as its own error class so the message says "top up", not "dispatch failed". |

---

## Hard rules

| Rule | Detail |
|---|---|
| **No prompt generation** | The user writes the prompts. This app never calls a model to write text. |
| **No approve gate** | Generate fires immediately. There is no "approve the prompt first" step — that was a deliberate removal from the Vasko design. |
| **Services holds API keys and nothing else** | The dialog is one key per service. Image settings live on the Prompts tab, next to the prompts they apply to; clip settings on the Clips tab. A global "default aspect ratio" buried in a settings dialog is exactly the arrangement that was removed — do not put one back. |
| **A setting is shown, never hidden behind a dialog** | The session-wide row displays the current values and reads `mixed` when cards disagree. "Apply to all…" as a button that opens a form was removed for saying nothing about what it applies. |
| **No `window.confirm` / `alert` / `prompt`** | Every question is an in-app modal. Browser chrome at the top of the screen in a different typeface does not read as part of the app. Use `confirmModal()`. |
| **Every card is named** | New cards get "Image N"; an imported payload's own title wins. `db.init()` backfills any card that ends up nameless. A nameless card would be nameless in the gallery and in its downloaded filename. |
| **The whole prompt is always visible** | The textarea grows to its content and never scrolls. See the timing traps below. |
| **Never overwrite a take** | Every generate is a new row. Editing a prompt does not destroy the image it made. |
| **Never lose a prompt to a bad paste** | If the importer cannot read something the user can clearly see is a prompt, that is a bug in `extract_objects`, not user error. |
| **The key never leaves the machine** | Except in calls to the provider it belongs to. `/api/state` sends `has_key` and a mask, never the value. The selftest asserts this. |
| **Bind 127.0.0.1 only** | No auth, holds an API key. |
| **`/file` serves images from the project root only** | Traversal is refused; non-image extensions are refused, so the database cannot be pulled through it. |
| **Deleting never touches disk** | Deleting a session, card or take removes rows. The generated files stay in `Output/`. |
| **Stdlib only** | No dependencies, no build step. If a change needs a package, it needs a conversation first. |

---

## API reference

Base `http://127.0.0.1:8770`. All POST bodies are JSON.

**Read**

| | |
|---|---|
| `GET /api/state` | sessions with counts and cover, `providers` (catalogue + `has_key` + mask), option lists |
| `GET /api/session/<id>` | one session: cards, each with refs, generations, `dirty`, `busy` |
| `GET /api/session/<id>/export` | the session as kie.ai payloads — API only, no button |
| `GET /api/log` | recent log lines |
| `GET /file?p=<rel>` | a generated image or local reference |
| `GET /download?s=<id>&latest=1` | every finished image as one zip |

**Sessions**

| | |
|---|---|
| `POST /api/sessions` | `{name}` → `{session_id}` |
| `POST /api/session/<id>/rename` | `{name}` |
| `POST /api/session/<id>/delete` | |
| `POST /api/session/<id>/cards` | `{prompt?, title?}` → `{card_id}` |
| `POST /api/session/<id>/import` | `{text}` → `{created, skipped, warnings}` |
| `POST /api/session/<id>/apply_all` | `{model?, aspect_ratio?, resolution?, output_format?}` |
| `POST /api/session/<id>/refs` | `{filename, data, name?}` — data is a data: url |
| `POST /api/session/<id>/ref_url` | `{url}` |
| `POST /api/characters` | `{filename, data, name?, session_id?}` — a global cast member |
| `POST /api/session/<id>/generate_all` | `{only_new?}` → `{queued, results}` |

**Cards**

| | |
|---|---|
| `POST /api/card/<id>` | any of `title, prompt, model, aspect_ratio, resolution, output_format` |
| `POST /api/card/<id>/delete` | |
| `POST /api/card/<id>/duplicate` | |
| `POST /api/card/<id>/move` | `{direction: "up"\|"down", scope?: "shots"\|"all"}` |
| `POST /api/session/<id>/clips` | `{after?, video_prompt?}` - a standalone clip |
| `POST /api/upload?session=&filename=&name=&kind=` | raw body: an image, video or sound becomes a ref (`kind` upload / character / frame) |
| `GET /api/update` | is a newer release out? `{current, latest, newer, notes, dev}` |
| `POST /api/update/apply` | install it and restart (exit code 3) |
| `POST /api/card/<id>/refs` | `{ref_ids: []}` — replaces, does not append |
| `POST /api/card/<id>/generate` | fires now; 428 if no API key |

**Other**

| | |
|---|---|
| `POST /api/ref/<id>/delete` | |
| `POST /api/generation/<id>/delete` | |
| `POST /api/generation/<id>/use_as_ref` | a finished image becomes a reference — API only, the button was removed by request |
| `POST /api/settings` | `{keys?: {provider: key}, clear?: [provider], kie_api_key?}` — a blank key leaves that one alone |
| `POST /api/sessions` | `{name, image_provider?, image_model?, video_provider?, video_model?}` |
| `POST /api/session/<id>/engine` | `{kind: "image"\|"video", provider, model?}` — sets the session engine and clears every card's override |
| `POST /api/card/<id>` | also `provider/model` and `video_provider/video_model` — the per-card override; `null` follows the session — and `video_mode`, `first_src`, `last_src` |

Status codes that mean something: **428** = no API key for that card's provider (the UI opens
Services on this), **402-derived 502** = out of credits, **400** = the request
was wrong, **404** = no such thing.

---

## Front end notes

One file, `app/static/app.js`, no framework. Worth knowing:

- **`S`** is the only state. Every render reads from it.
- **Edits are debounced 550ms** to `POST /api/card/<id>`, and optimistic in `S`.
- **`renderCards()` will not redraw a card that has focus.** It patches that
  card's footer and take strip instead. Break this and the caret jumps to the
  end of the textarea while the user is typing — the single most annoying bug
  this app can have.
- **Polling is conditional.** `tick()` only refetches when something is actually
  in flight, so an idle session makes no requests at all.
- **The take strip under each prompt and the number badge in the gallery are the
  same number** — that is the pointer between the two tabs. Keep them in sync.

### Three timing traps, all already hit once

These are not hypothetical; each of them shipped as a bug and was fixed.

1. **`requestAnimationFrame` never fires while the window is hidden.** Anything
   that must happen on render — `growAll()` above all — runs synchronously.
   Defer it to a frame and prompts stay clipped until the user clicks something.
2. **`Animation.finished` never resolves while the window is hidden.** So
   `fadeOutCard()` races it against a timeout. Never let a decoration block the
   mutation it decorates.
3. **A hidden element measures as zero.** `autoGrow()` bails when `offsetParent`
   is null, and every path that *reveals* the prompts calls `growAll()` again:
   `show()`, `setTab('prompts')`, `renderCards()`, resize, visibilitychange.
   `show()` runs after the render, so the render's own attempt is the one that
   measures nothing.

### Look

The first design was dark glass with a flock of boids drifting over a colour
field. It was removed on purpose: it read as generic and fought the work. The
current look is Cherry: ink and paper, flat bordered panels, one cherry-red
accent (`--accent`), one sans-serif family throughout, dark by default with a
toggle (`data-theme` on `<html>`, remembered in localStorage). The painted cherry
(`static/img/cherry-hero.webp`) sits behind the home screen. Service logos in
`static/img/logos/` came from each service's own website. Keep new UI to the
tokens in `style.css`; no gradients, glows or emoji. Icons are inline SVG via
`ic()` in `app.js`.

### The model picker

`openModelMenu()` is the only way a model is chosen: a Spotlight-style window
with the search focused on open, every model of every service **that has a key**
laid out in family blocks (`familyOf()`), arrow keys moving spatially. New
session is three windows - name, image picker, clip picker - and Esc steps back
one (`onCancel`). The Images/Clips chips open the picker straight away.

### Animation

Structural changes go through `restructure()`, which measures every card,
lets the list re-render, then plays a FLIP transition from the old position to
the new one. Add, duplicate, reorder, delete and folding all use it.
`playFlip()` treats a card with no previous rect as new and fades it in instead.

Both `captureRects()` and `playFlip()` select `.card[data-card]`, not `.card` —
the add tile is a `.card` too and carries no id. The focus-preserving patch path
in `renderCards()` scopes the same way, or it would delete the add tile every
time somebody typed.

### Folding

`S.collapsed` is a Set of card ids. A collapsed card hides the textarea and the
reference strip and shows `.peek` — three clamped lines of the prompt — while
keeping the **settings row**, the status line, the Generate button and the take
strip live. Seeing that a folded card is still 1K/jpg is the point of glancing
at it, so the settings stay.

When *every* card is folded, `renderCards()` puts `.compact` on `#cards`, which
switches the grid to four fixed columns and shrinks the type, selects and take
thumbnails. Partially folded boards keep the normal `minmax(352px, 1fr)` grid.

The clamp is fussy: `-webkit-line-clamp` only applies when it ships in the same
rule that sets `display: -webkit-box`, and Chrome's newer standard `line-clamp`
computes the display to `flow-root` and rounds the box up to the nearest pixel
past the lines it kept. Hence the belt-and-braces `max-height` in
`.card.collapsed .peek`, driven off a `--peek-pad` custom property so the
compact size cannot drift out of sync with the calc. If you change the padding
or line-height, change `--peek-pad` rather than the numbers.

### JSON help

`static/prompts.js` builds the master prompts you paste into ChatGPT. It is a
separate file because it is prose, not logic, and it will be edited far more
often than `app.js`.

- `buildImagePrompt({shots, design, …})` and `buildVideoPrompt({shots, …})`
  assemble the text; the dialog re-runs them whenever the shot count or the art
  direction changes.
- `DESIGNS` is the art-direction list. **`none` is the default and imposes no
  style** — a script usually specifies its own look, and a second opinion
  stapled on top only fights it. Adding a design means adding one entry with a
  `block` of lines; nothing else changes.
- Every prompt ends on `Here is the document:` and nothing after it. There is no
  `[paste your script here]` placeholder — the user attaches the file.
- Both tell the model **not** to emit `image_input` / `image_urls`. References
  are attached in the app.
- Written as arrays of lines joined with `\n`, never template literals: the text
  is full of quotes and braces.

If the payload shape changes, `prompts.js`, `db.normalize_payload()` and
`db.normalize_video_payload()` move together.

---

## Testing

```bash
python system/app/selftest.py
```

476 checks, fully sandboxed — its own temp database, refs and output folders,
with every kie.ai call stubbed and every other provider's wire faked and a real HTTP server on a loopback port. No
network, no credits, nothing touched outside the temp dir.

**Run it after any change to `db.py`, `kie.py` or `server.py`.**

---

## Clips

Three tabs: **Prompts** (image prompts - shots only), **Clips** (every card,
in edit order - shots' clips and standalone clips) and **Videos** (finished
clips, read-only).

### A clip runs in a mode, and the card takes the model's shape

A clip model can run in up to three modes - `frames` (an exact first frame,
plus a last frame where the model takes one), `refs` (reference images /
videos / audio the prompt talks about) and `text` (nothing but the prompt).
Which ones a model has, and what each needs, is in its spec's `modes`
(`tools/build_catalog.py` builds them from `tools/research/*_video_io.json`).
A mode may use a different model id: Kling 2.6's text mode is
`kling-2.6/text-to-video`, Veo uses one id and a hidden `generation_type`,
fal's Veo first+last frame is a separate `flf` endpoint. Each mode carries its
own `{api, controls, fixed}`, so the card's duration/resolution dropdowns are
the ones that mode really has.

`cards.video_mode` is what the user picked (NULL = automatic: a configured
first frame means frames, otherwise references if any, otherwise text).
`providers.pick_mode()` falls back when the model lacks the picked mode;
`db.clip_state()` is the one place that resolves mode, frames, references,
readiness (`problem`) and how many inputs this model will not use (`hidden`) -
the board and `kie.dispatch_video` both read it, so they cannot disagree.
Switching model never deletes inputs: they are kept and counted as hidden.

### Frame sources

`cards.first_src` / `last_src`: `self` (this shot's active image, falling back
to an image attached to the shot), `card:<id>` (another shot's active image -
live, follows regeneration), `ref:<id>` (anything in the library, including a
**clip ending**: the browser seeks a finished clip to its end, draws it to a
canvas and uploads it as a `frame` ref), `none`. NULL means the default: `self`
for a shot's first frame, nothing otherwise. A shot whose image is not made yet
waits for it (problem: "This shot has no image yet") rather than quietly
turning text-only.

A model with references but no frame fields (Gemini Omni Video, the app's
original default) still gets the shot's picture: `first_as_ref` sends it as the
first reference image, which is how the app has always driven that model.

### References

`card_video_refs` holds a clip's references; `refs.media` says image / video /
audio (`db.media_of()` by extension). Videos and audio arrive through
`POST /api/upload` as the raw request body (no base64 - a 200MB clip), and go to
the service through each adapter's `media()`/`upload()`: kie.ai's multipart
stream upload, fal storage, Replicate's files API - links cached on the ref
with the provider's own expiry. Prompt tags (`@Image1`, `[Image 1]`, `Image 1`)
come from the spec's `syntax`; clicking a reference on the card types its tag
at the caret. Kling "elements", PixVerse named references and Gemini audio ids
are not supported - they need their own editors.

Imports: clip prompts beyond the number of shots, or JSON prompts with no
matching shot, become standalone clips instead of being dropped.

### Never rebuild a grid that contains media

`patchGrid()` keys every tile and stores a signature on it. A tile whose
signature is unchanged is not touched at all — not replaced, not moved.

This is not an optimisation. The poll runs every 2.5s while anything is
generating, and rebuilding `innerHTML` destroyed and recreated every `<video>`,
which reset playback and made the page jump: you could not watch one clip while
another rendered. Re-inserting a `<video>` restarts it, so even reordering is
done only when a tile is genuinely out of place.

A card that is mid-render gets its own tile, ghosting its source image behind a
spinner, so pressing Generate clip and switching to Videos shows something
happening rather than an empty page.

### Video JSON import fills, it does not create

`import_video_json` attaches prompts to cards that already exist, matching on
**`seq` first, then an exact title, then position**. Never on generation time.
A payload with no matching shot becomes a standalone clip; a shot with no
payload is named in a warning.

## Not built yet

- **Drag-to-reorder.** Cards move with the ◀ ▶ buttons.
- **Bulk delete / multi-select.**
- **Trimming or stitching clips** — Cherry generates them, it does not edit.
