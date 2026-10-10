# 🍒 Cherry

A small local dashboard for generating images from prompts you write yourself,
through the service and model you pick: kie.ai, fal.ai, Replicate, Google or OpenAI.
(Formerly Banana.)

Write prompts. Press generate. Every take is kept.

Pick what makes the pictures and clips for every session: kie.ai, fal.ai,
Replicate, Google Gemini or OpenAI. Mix them freely, even shot by shot.

---

## Start it

Double-click **Start Cherry** in the Cherry folder (or the Cherry icon on your
Desktop, once you have run **Make desktop shortcut**). A small window opens and
your browser opens on `http://127.0.0.1:8770/`. Close that window to stop Cherry.

It needs Python 3 and nothing else. The first time, it asks for an API key for
whichever service you use.

---

## How it works

### Sessions

A **session** is one set of prompts and everything they generate. Name it after
the job — "Throat clearing ad v3" — and it stays on the home screen with a
cover image, a prompt count and an image count until you delete it.

Its images land in `out/<session-name>/`, numbered by prompt.

### Prompts tab

Prompts sit side by side, two or three to a row. Each is a card with a number
badge, and that number is the thread running through the whole app: it is on the
card, on the thumbnails under the card, and on every image in the gallery.

A card holds:

- a **name** — new prompts are called *Image 1*, *Image 2*… Click it to rename.
- the **prompt**, in a box that grows so the whole thing is always on screen
- **references** — up to 8 images fed to the model
- **aspect ratio**, **resolution** and **format**, set per card

The small buttons top-right of a card: **◀ ▶** move it in the order, **⧉**
duplicates it, **✕** deletes it. The **▾** on the left folds that one card down.
Everything animates, so you can see what moved.

Three ways to add a prompt:

- the **+ tile** at the end of the grid — a blank card, ready to type into.
- **Import JSON** — paste one kie.ai payload or a whole pile of them. See below.
- **Duplicate** — copy a card, references and all, and tweak it.

**All N prompts** — the row of dropdowns at the top shows what every prompt is
currently set to. Pick a value and it is applied to all of them at once. If your
cards disagree it says *mixed* until you choose.

**Minimise all** folds every prompt into a compact card — name, three lines of
the prompt, its aspect/resolution/format, its status and its images — four to a
row, so you can see the whole set at once and still change a setting or hit
Generate. Click it again to expand.

### Generating

**Generate** on a card fires it immediately. The button then reads:

- **Generate again** — nothing has changed since the last take, so this is a
  re-roll.
- **Generate** — you edited the prompt or a setting, so this take will differ.

**Generate all** (top right) offers both: everything, or only the cards that are
new or edited since their last take — so a second pass does not re-bill the ones
you were happy with.

`Ctrl+Enter` inside a prompt generates that card.

Thumbnails appear under the card as takes finish. Click one to open it.

### Clips tab — turning them into video

Every clip in the session, in the order of the final edit. A clip is either a
**shot's clip** (it belongs to an image prompt and can start from its picture)
or a **standalone clip** (no image prompt at all - for a shot you'd rather
describe than draw). Add one with the **Add clip** tile, or the **+** on any
clip card to put one right after it.

**The card takes the shape of its model.** Pick the model at the top of the
card and it shows only what that model can use:

- **Frames** - an exact *first frame*, and a *last frame* where the model
  takes one (Seedance, Veo, Kling 3.0/2.5 Pro, Wan 3.0, Hailuo 02, MiniMax H3…).
  Click a frame to choose where it comes from: this shot's picture, **another
  shot's picture** (it follows that shot if you regenerate it), **where another
  clip ends** (Cherry grabs that clip's last frame, so this one carries on from
  it), or anything you upload.
- **References** - pictures, videos and sounds the model works from rather than
  copies exactly (Seedance 2.x up to 30 images, 10 videos, 10 sounds; Wan 3.0,
  Veo, Kling Omni, MiniMax H3…). Click a reference to type its tag
  (`@Image1`) into the prompt. Counts show the model's limit.
- **Text only** - just the prompt. Models without a text mode of their own use
  their text-to-video sibling automatically.

Switch model and anything the new one can't use is kept, not deleted - the
card says "2 inputs kept but not used by this model" - and comes back when you
switch again. A clip that isn't ready says why ("This shot has no image yet",
"Too many reference images (12 of 9)") and its Generate button stays off.

Sound follows each model's own default. Length, resolution and aspect only list
what the model really offers - Kling 5 or 10 seconds, Hailuo 6 or 10, Veo 4/6/8.

**Older versions.** If a shot has several images, **▸ N older versions** under
its first frame lets you pick which one the clip starts from.

### Videos tab

Every finished clip, playable in place, in edit order. **Show every take** if you
want the older renders too, and **⭳ Download all** zips the lot.

### Filter and fold

**Filter by name** on either tab narrows to the shots you are working on — type a
name or a number. **Minimise all** folds them into compact cards showing the
settings, the status and the thumbnails, four or more to a row depending on how
wide your window is.

### Everything you generated

Every take, newest first, each tagged with the number of the prompt that made
it and its version. Tick **Latest take only** to see just the current one per
prompt.

Hover an image for a **⭳** button to save that one. **Download all** (top right)
gives you the whole session as a zip, named by prompt number and take — and it
follows the *Latest take only* tick, so you can grab just the current set.

Click any image for the full view: the exact prompt that produced it, a
download, and delete.

### Characters and references

**Add character** (top of the Prompts tab) is for the faces you use again and
again. Give a name, drop in one or several photos of them, done. **Characters
are kept across every session** — upload someone once and they are offered on
every prompt of every video you make afterwards. Deleting a session never
touches them.

The **+** square on a card opens the picker, which lists your **Cast** first and
then anything used in **this session**. From there you can also:

- **Drop a file on it, or browse your PC** — kept in `refs/` and pushed to
  kie.ai. Their links expire after three days; Cherry re-sends the file
  automatically, so you never have to think about it.
- **Paste a link** to an image already online.

Attached references show as thumbnails on the card, big enough to recognise at a
glance. Hover one and hit **✕** to take it off that prompt; deleting it
altogether is the **✕** inside the picker.

---

## Getting your prompts written for you

**JSON help**, in the top bar, writes the prompt for you. It has two halves —
**Images** and **Videos** — and builds the text from two choices:

- **Shots** — how many images to break the script into. Change it and the whole
  prompt updates.
- **Art direction** — *From the document* (the default: no style is imposed,
  because your script usually says what it should look like), or one you pick:
  cinematic 3D animation, clay stop-motion, 1930s hand-drawn cartoon, or
  photoreal. Choosing one writes a full style brief into the prompt and tells
  ChatGPT to repeat it in every shot so the set matches.

Hit **Copy the prompt**, paste it into ChatGPT, attach your script, and paste the
JSON it returns into **Import JSON**.

The video half works the same way: it asks for motion only — the image is the
first frame — and numbers each clip so it lands on the right shot.

## Two ways to import

**Import JSON** reads whichever you paste — it tells you which as you type.

**Just the prompts.** Paste them as plain text with a **blank line between
each one** (press Enter twice). Each block becomes a card on your current
settings, named *Image 1*, *Image 2*… A single newline stays *inside* a prompt,
so paragraphs are fine:

```
A warm kitchen at dawn, a woman at the counter, medium close-up.

Extreme close-up of her hands around a white mug,
steam rising in the morning light.

The window, rain running down it.
```

On the Images tab the same paste fills the clip prompts, shot 1, shot 2, and so
on, in order.

**Or the JSON**, in the kie.ai payload shape:

```json
{
  "title": "Kitchen at dawn",
  "model": "nano-banana-2",
  "input": {
    "prompt": "a kitchen at dawn, warm light",
    "aspect_ratio": "9:16",
    "resolution": "1K",
    "output_format": "jpg"
  }
}
```

There is deliberately no `image_input` here — you attach reference images in the
app, with the **+** on the card. If a payload does carry one, Cherry keeps any
real `https://` links and silently drops anything else, so a model writing
`"image_input": ["the character photo"]` cannot turn into a broken reference and
a wasted generation. It tells you how many it ignored.

Each payload with a prompt becomes one card. You can paste:

- a single object
- a JSON array of them
- **several objects one after another**, with or without commas between
- a half-copied paste that is missing its outer braces or its closing one

That last case is the normal one when you copy out of a docs panel, and it
works. Anything Cherry genuinely cannot read is reported rather than silently
dropped.

Missing fields fall back to your defaults from Settings. `callBackUrl` is
ignored — Cherry polls for results instead. A `negative_prompt` is folded into
the prompt as an exclusion clause, because nano-banana has no negative field.

If a payload carries a `title` (or `name`), that becomes the card's name;
otherwise it is *Image N* like any other.

---

## Services and engines

**Services** (top right of the home screen, or the sliders icon inside a
session) is where API keys live, one per service. Keys are stored in
`data/cherry.db` and only ever sent to the service they belong to. The first
time you open Cherry, with nothing connected, the home screen leads with this.

| Service | Makes | Some of the models |
|---|---|---|
| kie.ai | images + clips | Nano Banana 2/2.1/Pro, Seedream 4–5, GPT Image, FLUX.2, Kontext, Grok, Ideogram, Qwen · Kling 2.1–3.0, Veo 3.1, Seedance, Hailuo, Wan, Runway, PixVerse |
| fal.ai | images + clips | Nano Banana, FLUX, Seedream, Imagen · Kling, Veo, Seedance, Hailuo, Wan, Luma |
| Replicate | images + clips | Nano Banana, Imagen 4, FLUX, Seedream, Qwen · Kling, Veo 3.1, Seedance, Hailuo, Wan, Luma, Runway, PixVerse |
| Google Gemini | images | Nano Banana 2.1, Nano Banana Pro, Flash Image |
| OpenAI | images | GPT Image 2.5, GPT Image 2, GPT Image 1.5 |

Every model has its own inputs, and Cherry knows them: Kling offers 5 or 10
seconds, Hailuo 6 or 10, Veo 4/6/8, Seedance any length from 4 to 15 — the
length dropdown on a shot only ever lists what its model can do, and the same goes
for aspect ratio, resolution and format. When a model has a separate "edit"
version (Seedream, GPT Image, Grok, Qwen…), attaching a reference picture
switches to it automatically.

An **engine** is a service plus one of its models. Every model also accepts a
custom id (*Another model id…*), for anything the list does not name.

- **The model menu** shows every model with its service badge, a search box, and
  short tags (lengths, top resolution, whether it takes reference pictures).
- **New session** asks for the engine up front: one for images, one for clips.
  If you pick a service with no key yet, a field appears right there to paste it.
- The **Images / Clips chip** at the top of each tab shows the session's engine.
  Click it to switch every shot at once. Takes you already made are kept.
- Each shot also has its own engine dropdown. *(session)* means it follows the
  session; pick something else and that shot alone uses it (highlighted yellow).
- Only the controls a model really uses are shown. Resolution, for instance, is
  a kie.ai setting and does not appear for FLUX.
- Reference images work with kie.ai, fal.ai (Nano Banana), Replicate (Nano
  Banana) and Gemini. A model that cannot take them fails the shot with a message
  rather than quietly dropping your character.

---

## Where everything lives

Everything is in the **Cherry folder**. Nothing is hidden in AppData, nothing is
in the cloud. Back it up by copying the folder.

```
Cherry/
  Start Cherry.bat
  Output/<session>/          finished images, numbered by shot
  Output/<session>/videos/   finished clips
  system/data/cherry.db      every session, prompt and take - and your API keys
  system/characters/         your cast - the faces you reuse across sessions
  system/refs/               one-off reference images
  system/app/                the program
```

Move or rename the folder whenever you like: on the next start Cherry finds
every picture in its new place by itself.

Deleting a session, a prompt or a take removes it **from the app only**. Nothing
is ever deleted at the service, and **the files on disk stay**.

---

## Updates

When a new version is out, the home screen says **Cherry 1.x is ready**. Click
**Update now**: Cherry downloads it, replaces its own program files and
restarts. Your sessions, keys, characters and the Output folder are never
touched. The version you have is next to the Cherry name, top left.

## Notes

- Nothing is ever overwritten. Rewrite a prompt and generate again, and the old
  take is still in the Images tab.
- kie.ai allows 20 new tasks per 10 seconds. Generate all paces itself, so a big
  batch will not get rejected.
- Runs only on `127.0.0.1` — it is not reachable from your network.
