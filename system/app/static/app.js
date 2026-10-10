/* Cherry — the whole client.
 *
 * One global `S` holds what the server last told us; every render reads from it
 * and nothing else. Edits are optimistic in the DOM and debounced to the server,
 * which is why typing in a prompt never re-renders the card out from under the
 * caret: renderCards() skips any card that currently holds focus.
 *
 * The one rule that costs money if broken: nothing that spends credits may run
 * before flushSaves() has landed every pending edit. See generate().
 */

const S = {
  view: 'home',
  sessions: [],
  settings: {},
  providers: {},
  options: { aspects: [], resolutions: [], formats: [],
             video_resolutions: [], durations: [] },
  session: null,
  tab: 'prompts',
  filter: '',            // home: session filter
  cardFilter: '',        // session: filter cards by name
  timer: null,
  collapsed: new Set(),  // prompt cards folded down
  vcollapsed: new Set(), // video cards folded down
  openVersions: new Set(),
  allTakes: false,       // videos tab: every take, or just the current one
  uploading: [],         // names of files mid-upload, for the spinner tiles
  firing: new Set(),     // buttons currently awaiting a dispatch response
};

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fileUrl = (rel) => '/file?p=' + encodeURIComponent(rel);
const INFLIGHT = ['pending', 'waiting', 'queuing', 'generating'];
const EASE = 'cubic-bezier(.22,.61,.36,1)';

/* ═════════════════════════════════════════════ icons */

const ICON = {
  left:  '<path d="M10 3 5 8l5 5"/>',
  right: '<path d="M6 3l5 5-5 5"/>',
  down:  '<path d="M3 6l5 5 5-5"/>',
  copy:  '<rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.5"/><path d="M3 10V4.5A1.5 1.5 0 0 1 4.5 3H10"/>',
  x:     '<path d="M4 4l8 8M12 4l-8 8"/>',
  plus:  '<path d="M8 3v10M3 8h10"/>',
  film:  '<rect x="2.5" y="3.5" width="11" height="9" rx="1.5"/><path d="M5 3.5v9M11 3.5v9"/>',
  note:  '<path d="M6 12V4l7-1.5V10"/><circle cx="4.5" cy="12" r="1.6"/><circle cx="11.5" cy="10" r="1.6"/>',
  more:  '<circle cx="3.5" cy="8" r=".8"/><circle cx="8" cy="8" r=".8"/><circle cx="12.5" cy="8" r=".8"/>',
};
const ic = (n) => '<svg class="ic" viewBox="0 0 16 16" aria-hidden="true">' + ICON[n] + '</svg>';

/* ═════════════════════════════════════════════ engines
 *
 * An engine is a (provider, model) pair: which service makes the picture, and
 * which of its models. A session has one for stills and one for clips; a card
 * follows its session unless you override that one card. The catalogue - every
 * model, and exactly which values each of its controls accepts - comes from the
 * server, so adding a model is a server-side change only.
 */

const PROV = () => S.providers || {};
const provLabel = (p) => (PROV()[p] || {}).label || p || 'kie.ai';

/* Each service's own icon, from its website (static/img/logos). A lettered
   square stands in for anything without one. */
const LOGOS = new Set(['kie', 'fal', 'replicate', 'gemini', 'openai']);
function badge(pid, size = 18) {
  if (LOGOS.has(pid)) {
    return `<img class="pbadge" src="/img/logos/${pid}.png" alt="" aria-hidden="true"
              width="${size}" height="${size}" style="--s:${size}px">`;
  }
  const t = (pid || '?')[0].toUpperCase();
  return `<span class="pbadge" style="--s:${size}px;--bg:#555;--fg:#fff" aria-hidden="true">${esc(t)}</span>`;
}

/* The catalogue entry for a model - or, for an id typed in by hand, the
   provider's first entry with that id, which is also what the server does. */
function specOf(provider, kind, model) {
  const list = (PROV()[provider] || {})[kind] || [];
  const hit = list.find((m) => m.id === model);
  if (hit) return hit;
  return { ...(list[0] || { controls: {} }), id: model, label: model || '?', custom: true };
}
const engineText = (e, kind) => specOf(e.provider, kind, e.model).label + ' · ' + provLabel(e.provider);
const providersFor = (kind) => Object.entries(PROV()).filter(([, p]) => p.kinds.includes(kind));
/* The ones you can actually use: a service only appears in the picker once it
   has a key. Services lists them all, so that is where a new one is added. */
const usableFor = (kind) => providersFor(kind).filter(([, p]) => p.has_key);

/* Card field <-> control, per stage. */
const CTL = {
  image: [['aspect', 'aspect_ratio'], ['resolution', 'resolution'], ['format', 'output_format']],
  video: [['duration', 'video_duration'], ['resolution', 'video_resolution'], ['aspect', 'video_aspect']],
};
const controlsOf = (e, kind) => Object.keys(specOf(e.provider, kind, e.model).controls || {});

/* What a stored value becomes for this model - the same rule the server uses
   when it builds the request, so the dropdown always shows what will be sent:
   an exact match, jpg/jpeg treated as one, the nearest length for a duration,
   otherwise the model's default. */
function ctlValue(key, ctl, v) {
  const vals = ctl.values.map(String);
  v = v == null ? '' : String(v);
  if (vals.includes(v)) return v;
  if (key === 'format') {
    const alias = { jpg: 'jpeg', jpeg: 'jpg' }[v];
    if (alias && vals.includes(alias)) return alias;
  }
  if (key === 'duration' && v && !isNaN(Number(v))) {
    return vals.reduce((best, d) =>
      Math.abs(Number(d) - Number(v)) < Math.abs(Number(best) - Number(v)) ? d : best, vals[0]);
  }
  return String(ctl.default ?? vals[0]);
}

function controlSelects(card, kind) {
  const e = kind === 'video' ? card.video_engine : card.engine;
  const spec = specOf(e.provider, kind, e.model);
  // A clip shows the controls of the mode it runs in: Veo's reference mode is
  // 8 seconds only, a text-to-video sibling may offer other ratios.
  const md = kind === 'video' && spec.modes && spec.modes[card.clip_mode];
  const ctls = (md && md.controls) || spec.controls || {};
  return CTL[kind].map(([key, field]) => {
    const ctl = ctls[key];
    if (!ctl || !ctl.values || !ctl.values.length) return '';
    return selectHtml(field, card.id, ctl.values.map(String), ctlValue(key, ctl, card[field]),
                      key === 'duration' ? 's' : '');
  }).join('');
}

/* Which family a model belongs to, for grouping in the picker: "Seedream 5.0
   Pro" -> Seedream, "FLUX.1 [dev]" -> FLUX, "GPT-4o Image" -> GPT Image. */
function familyOf(label) {
  const l = label.replace(/^(Google|OpenAI|ByteDance|Alibaba)\s+/i, '');
  if (/^nano banana/i.test(l)) return 'Nano Banana';
  if (/^flux/i.test(l)) return 'FLUX';
  if (/^gpt/i.test(l)) return 'GPT Image';
  if (/^gemini omni/i.test(l)) return 'Gemini Omni';
  if (/^grok/i.test(l)) return 'Grok';
  if (/^(minimax|hailuo)/i.test(l)) return 'Hailuo / MiniMax';
  if (/^luma/i.test(l)) return 'Luma';
  return l.split(/[\s(]/)[0];
}

/* Inside its block a model drops the family name it would only repeat:
   "Seedream 5.0 Pro" reads "5.0 Pro" under Seedream. */
function shortLabel(label, fam) {
  const l = label.replace(/^(Google|OpenAI)\s+/i, '');
  if (!l.toLowerCase().startsWith(fam.toLowerCase())) return label;
  const rest = l.slice(fam.length);
  if (/^[.\d]/.test(rest)) return label;               // "FLUX.1 [dev]" stays whole
  const short = rest.replace(/^[\s\-]+/, '').replace(/^(image|video)\b\s*/i, '');
  // a bare version number reads better with its name: "Nano Banana 2", not "2"
  return short && !/^[\d.]+$/.test(short) ? short : label;
}

/* Short facts for the model menu: how long, how sharp, whether it takes
   reference pictures. */
function modelTags(m, kind) {
  const c = m.controls || {}, tags = [];
  if (c.duration) {
    const v = c.duration.values.map(Number).sort((x, y) => x - y);
    tags.push(v.length === 1 ? v[0] + 's' : v.length <= 3 ? v.join('/') + 's' : v[0] + '–' + v[v.length - 1] + 's');
  }
  if (c.resolution) tags.push(String(c.resolution.values[c.resolution.values.length - 1]));
  if (kind === 'image' && m.refs) tags.push(m.mode === 'edit' ? 'needs image' : 'refs');
  if (kind === 'video') {
    const md = m.modes || {};
    if (md.frames) tags.push(md.frames.last ? 'first+last' : 'first frame');
    if (md.refs) tags.push('refs' + (md.refs.videos ? ' +video' : '') + (md.refs.audio ? ' +audio' : ''));
    if (md.text) tags.push('text');
  }
  return tags;
}

/* Where a brand-new session should start: the last choice, unless that service
   has no key and another does. */
function startingEngine(kind) {
  const set = S.settings;
  let provider = set[kind + '_provider'];
  let model = set[kind + '_model'];
  const ok = usableFor(kind).length ? usableFor(kind) : providersFor(kind);
  if (!ok.some(([id]) => id === provider)) provider = ok[0] && ok[0][0];
  if (!(PROV()[provider] || {}).has_key) {
    const connected = ok.find(([, p]) => p.has_key);
    if (connected) { provider = connected[0]; model = null; }
  }
  const list = (PROV()[provider] || {})[kind] || [];
  if (!model || !list.some((m) => m.id === model)) model = list[0] && list[0].id;
  return { provider, model };
}

/* ── the model picker: a Spotlight-style window for every place a model is chosen
 *
 * It opens with the search box focused, so you can type straight away; every
 * model of every service is laid out at once, one column per service. Arrow keys
 * move, Enter picks, Esc closes. `anchor` is only used to give focus back.
 */

let MENU = null;
function closeMenu(cancelled = false) {
  if (!MENU) return;
  const onCancel = cancelled && MENU.onCancel;
  MENU.el.remove();
  document.removeEventListener('keydown', MENU.key, true);
  const back = MENU.anchor;
  MENU = null;
  if (back && back.isConnected) back.focus();
  if (onCancel) onCancel();
}

function openModelMenu(anchor, { kind, current, session, onPick, onCancel, title, step, skip }) {
  closeMenu();
  const noun = kind === 'video' ? 'clip' : 'image';
  const usable = usableFor(kind);
  const total = usable.reduce((n, [, p]) => n + p[kind].length, 0);
  const el = document.createElement('div');
  el.className = 'mpick-scrim';
  el.innerHTML = `
    <div class="mpick" role="dialog" aria-label="Choose a ${noun} model">
      ${title ? `<div class="mpick-title">${step ? `<span class="step">${esc(step)}</span>` : ''}<b>${esc(title)}</b></div>` : ''}
      <div class="mpick-search">
        <svg class="ic" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>
        <input type="text" placeholder="Search ${total} ${noun} models — name, service, 4K, 10s…"
               autocomplete="off" spellcheck="false">
      </div>
      ${session ? `<button class="mfollow" data-v="">${badge(session.provider)}
          <span><b>Follow the session</b><small>${esc(engineText(session, kind))}</small></span></button>` : ''}
      <div class="mpick-cols"></div>
      ${providersFor(kind).length > usable.length ? `<p class="mhidden">${providersFor(kind).length - usable.length}
        more service${providersFor(kind).length - usable.length === 1 ? '' : 's'} hidden until you add a key —
        <a data-act="settings">Services</a></p>` : ''}
      <div class="mpick-foot"><span><kbd>↑</kbd><kbd>↓</kbd><kbd>←</kbd><kbd>→</kbd> move</span>
        <span><kbd>enter</kbd> choose</span><span><kbd>esc</kbd> ${onCancel ? 'back' : 'close'}</span>
        <span class="mcount"></span>
        <button type="button" class="linkbtn" data-custom>Model not listed?</button>
        ${skip ? `<button type="button" class="btn sm" data-skip>${esc(skip)}</button>` : ''}</div>
    </div>`;
  document.body.append(el);
  const input = $('.mpick-search input', el), cols = $('.mpick-cols', el);
  const curKey = current ? current.provider + '|' + current.model : '';
  if (session && !current) $('.mfollow', el).classList.add('on');
  if (title) $('.mpick-search input', el).placeholder = 'Search ' + total + ' ' + noun + ' models…';

  // Search matches the name, the id, the service and the tags, word by word:
  // "kling 10s" finds every Kling that does ten seconds.
  const hay = (pid, p, m) => (m.label + ' ' + m.id + ' ' + p.label + ' ' + modelTags(m, kind).join(' ')
                              + ' ' + (m.note || '')).toLowerCase();

  const paint = () => {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    if (!usable.length) {
      cols.innerHTML = `<div class="mempty">No service with ${noun} models has a key yet.
        <br><button class="btn primary" data-act="settings" style="margin-top:14px">Add a key in Services</button></div>`;
      $('.mcount', el).textContent = '';
      return;
    }
    cols.innerHTML = usable.map(([pid, p]) => {
      const items = p[kind].filter((m) => words.every((w) => hay(pid, p, m).includes(w)));
      shown += items.length;
      if (!items.length) return '';
      // One small block per model family, in the order the family first appears.
      const fams = new Map();
      items.forEach((m) => {
        const f = familyOf(m.label);
        if (!fams.has(f)) fams.set(f, []);
        fams.get(f).push(m);
      });
      return `<section class="mcol">
        <header>${badge(pid, 20)}<b>${esc(p.label)}</b><span class="n">${items.length}</span></header>
        <div class="mfams">${[...fams].map(([f, list]) => `
          <div class="mfam"><h5>${esc(f)}</h5>${list.map((m) => {
            const v = pid + '|' + m.id;
            return `<button class="mitem${v === curKey ? ' on' : ''}" data-v="${esc(v)}" title="${esc(m.label)} · ${esc(m.id)}${m.note ? ' — ' + esc(m.note) : ''}">
              ${v === curKey ? '<svg class="ic mcheck" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>' : ''}<span class="mname">${esc(shortLabel(m.label, f))}${m.note ? '<small>' + esc(m.note) + '</small>' : ''}</span>
              <span class="mtags">${modelTags(m, kind).map((t) => `<i>${esc(t)}</i>`).join('')}</span></button>`;
          }).join('')}</div>`).join('')}</div></section>`;
    }).join('') || `<div class="mempty">Nothing matches “${esc(input.value.trim())}”.</div>`;
    $('.mcount', el).textContent = shown + ' of ' + total;
    const items = $$('.mitem', cols);
    const on = (!words.length && $('.mitem.on', cols)) || items[0];
    items.forEach((b) => b.classList.toggle('act', b === on));
    if (on) on.scrollIntoView({ block: 'nearest' });
  };
  paint();

  const pick = (b) => {
    const v = b.dataset.v, cut = v.indexOf('|');
    closeMenu();
    onPick(v ? { provider: v.slice(0, cut), model: v.slice(cut + 1) } : null);
  };
  el.addEventListener('click', (e) => {
    const b = e.target.closest('.mitem, .mfollow');
    if (b) return pick(b);
    if (e.target.closest('[data-act="settings"]')) return closeMenu();
    if (e.target.closest('[data-skip]')) { closeMenu(); return onPick(undefined); }
    if (e.target.closest('[data-custom]')) {
      const back = { kind, current, session, onPick, onCancel, title, step, skip };
      closeMenu();
      return customModel(kind, onPick, () => openModelMenu(anchor, back));
    }
    if (e.target === el) closeMenu(true);
  });
  cols.addEventListener('mousemove', (e) => {
    const b = e.target.closest('.mitem');
    if (b && !b.classList.contains('act')) $$('.mitem', cols).forEach((x) => x.classList.toggle('act', x === b));
  });
  input.addEventListener('input', paint);

  // Arrow keys move to the nearest model in that direction, wherever it sits
  // in the grid of blocks.
  const move = (dir) => {
    const items = $$('.mitem', cols);
    const cur = items.find((b) => b.classList.contains('act'));
    if (!cur) return items[0];
    const c = cur.getBoundingClientRect(), cx = c.left + c.width / 2, cy = c.top + c.height / 2;
    let best = cur, bestScore = Infinity;
    for (const b of items) {
      if (b === cur) continue;
      const r = b.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const dx = x - cx, dy = y - cy;
      const ahead = { up: -dy, down: dy, left: -dx, right: dx }[dir];
      if (ahead <= 2) continue;
      const side = dir === 'up' || dir === 'down' ? Math.abs(dx) : Math.abs(dy);
      const score = ahead + side * 3;
      if (score < bestScore) { bestScore = score; best = b; }
    }
    return best;
  };
  const key = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); return closeMenu(true); }
    const dirs = { ArrowDown: 'down', ArrowUp: 'up', ArrowLeft: 'left', ArrowRight: 'right' };
    // left/right stay in the text box while there is text to move through
    if (dirs[e.key] && !((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && input.value)) {
      e.preventDefault();
      const n = move(dirs[e.key]);
      if (n) { $$('.mitem', cols).forEach((b) => b.classList.toggle('act', b === n)); n.scrollIntoView({ block: 'nearest' }); }
    } else if (e.key === 'Enter') {
      const a = $('.mitem.act', cols);
      if (a) { e.preventDefault(); e.stopPropagation(); pick(a); }
    } else if (e.target !== input && e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
      input.focus();                       // typing anywhere goes to the search
    }
  };
  document.addEventListener('keydown', key, true);
  MENU = { el, key, anchor, onCancel };
  input.focus();
}

/* The button that opens it: badge, model, and which service it comes from. */
function engineButton(e, kind, attrs, { follows = false } = {}) {
  const m = specOf(e.provider, kind, e.model);
  return `<button type="button" class="engbtn${follows ? '' : ' own'}" ${attrs}
            title="${esc(m.id)} on ${esc(provLabel(e.provider))}">
    ${badge(e.provider)}<span class="m">${esc(m.label)}</span>
    <span class="p">${follows ? 'session · ' : ''}${esc(provLabel(e.provider))}</span>
    <svg class="ic chev" viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg></button>`;
}

function engineSelectHtml(card, kind) {
  const own = kind === 'video' ? card.video_engine : card.engine;
  return engineButton(own, kind, `data-act="pick-engine" data-kind="${kind}" data-card="${card.id}"`,
                      { follows: !own.own });
}


/* ═════════════════════════════════════════════ transport */

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const err = new Error(data.error || (r.status + ' ' + r.statusText));
    err.status = r.status;
    throw err;
  }
  return data;
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, 4600);
}

/* ═════════════════════════════════════════════ pending edits
 *
 * Field edits are debounced so typing does not hammer the server. That leaves a
 * window where the screen and the database disagree — and generating inside
 * that window would send the OLD settings and bill for the wrong image. So
 * every path that spends money awaits flushSaves() first.
 */

const pending = new Map();

function queueSave(cardId, field, value) {
  const card = S.session.cards.find((c) => c.id === cardId);
  if (card) card[field] = value;
  const key = cardId + ':' + field;
  const prev = pending.get(key);
  if (prev) clearTimeout(prev.timer);

  const run = async () => {
    pending.delete(key);
    await api('/api/card/' + cardId, { [field]: value });
  };
  const timer = setTimeout(() => {
    run().then(() => loadSession(S.session.id, { quiet: true }))
         .catch((e) => toast(e.message, 'bad'));
  }, 550);
  pending.set(key, { timer, run });
}

async function flushSaves() {
  const waiting = [...pending.values()];
  if (!waiting.length) return;
  waiting.forEach((p) => clearTimeout(p.timer));
  pending.clear();
  for (const p of waiting) {
    try { await p.run(); } catch (e) { toast(e.message, 'bad'); }
  }
}

/* ═════════════════════════════════════════════ FLIP */

function captureRects(sel = '#cards') {
  const m = new Map();
  $$(sel + ' .card[data-card]').forEach((el) =>
    m.set(el.dataset.card, el.getBoundingClientRect()));
  return m;
}

function playFlip(prev, sel = '#cards') {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  $$(sel + ' .card[data-card]').forEach((el) => {
    const before = prev.get(el.dataset.card);
    const after = el.getBoundingClientRect();
    if (!before) {
      el.animate([{ opacity: 0, transform: 'scale(.94) translateY(12px)' },
                  { opacity: 1, transform: 'none' }],
                 { duration: 300, easing: EASE });
      return;
    }
    const dx = before.left - after.left, dy = before.top - after.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
    el.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' },
                { transform: 'none' }], { duration: 360, easing: EASE });
  });
}

async function restructure(work, sel = '#cards') {
  const prev = captureRects(sel);
  await work();
  await loadSession(S.session.id);
  playFlip(prev, sel);
}

async function fadeOutCard(cardId) {
  const el = $('.card[data-card="' + cardId + '"]');
  if (!el || document.hidden || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  el.classList.add('leaving');
  const anim = el.animate([{ opacity: 1, transform: 'none' },
                           { opacity: 0, transform: 'scale(.9)' }],
                          { duration: 190, easing: EASE });
  // The browser pauses animations on a hidden window, so `finished` can hang
  // indefinitely. A decoration must never block the delete it decorates.
  await Promise.race([anim.finished.catch(() => {}),
                      new Promise((done) => setTimeout(done, 280))]);
}

/* ═════════════════════════════════════════════ state */

async function loadHome() {
  const st = await api('/api/state');
  S.sessions = st.sessions;
  S.settings = st.settings;
  S.providers = st.providers;
  S.options = st.options;
  S.version = st.version;
  renderHome();
  checkUpdate();
}

async function loadSession(id, { quiet = false } = {}) {
  const view = await api('/api/session/' + id);
  S.session = view;
  if (!quiet) renderSession(); else refreshSession();
}

function show(view) {
  S.view = view;
  document.documentElement.dataset.view = view;
  if (view === 'session') growAll();
  clearInterval(S.timer);
  S.timer = setInterval(tick, 2500);
  window.scrollTo({ top: 0 });
}

async function tick() {
  // A poll that lands between a keystroke and its debounced save would repaint
  // the card from stale server data. Never refresh with edits in flight.
  if (pending.size) return;
  // Nothing to see in a hidden window, and the server keeps collecting results
  // regardless, so there is nothing to catch up on when you come back either.
  if (document.hidden) return;
  try {
    if (S.view === 'session' && S.session) {
      if (S.session.cards.some((c) => c.busy || c.video_busy)) {
        await loadSession(S.session.id, { quiet: true });
      }
    } else if (S.view === 'home') {
      if (S.sessions.some((s) => s.busy)) await loadHome();
    }
  } catch { /* a dropped poll is not worth a toast */ }
}

/* ═════════════════════════════════════════════ home */

/* ═════════════════════════════════════════════ updates
 *
 * "Check for updates" (home screen, top right) asks GitHub through the server
 * and answers in one window: a new version with what changed and an Update now
 * button, or "you're up to date". Updating happens in the same window - it
 * downloads, replaces the program files (never sessions, keys or Output),
 * Cherry restarts itself, and the page reloads on the new version.
 * The home screen also shows a banner on its own when a new version is out.
 */
async function checkUpdate(force = false) {
  try {
    const u = await api('/api/update' + (force ? '?force=1' : ''));
    S.update = u;
    const box = $('#update');
    $('#ver').textContent = u.current ? 'v' + u.current : '';
    $('[data-act="check-update"]')?.classList.toggle('has-new', !!(u.newer && !u.dev));
    if (!u.newer || u.dev) { box.hidden = true; return u; }
    box.hidden = false;
    box.innerHTML = `<div><b>Cherry ${esc(u.latest)} is out</b>
        <span>You have ${esc(u.current)}. See what's new and update in one click.</span></div>
      <button class="btn primary" data-act="check-update">See update</button>`;
    return u;
  } catch { return null; }
}

/* Release notes as written on GitHub: "- " lines become a list, the rest
   paragraphs. Escaped first, so nothing in them can run. */
function notesHtml(text) {
  const lines = String(text || '').split('\n').map((l) => l.trim());
  let html = '', list = false;
  for (const l of lines) {
    const item = /^[-*•]\s+/.test(l);
    if (item && !list) { html += '<ul>'; list = true; }
    if (!item && list) { html += '</ul>'; list = false; }
    if (!l) continue;
    const t = esc(l.replace(/^[-*•]\s+/, '').replace(/^#+\s*/, ''));
    html += item ? '<li>' + t + '</li>' : (/^#/.test(l) ? '<h5>' + t + '</h5>' : '<p>' + t + '</p>');
  }
  return html + (list ? '</ul>' : '');
}

function openUpdates() {
  const m = modal(`<div class="upd" id="upd"></div>`);
  const box = $('#upd', m);
  const show = (html) => { box.innerHTML = html; };

  const checking = () => show(`
    <div class="upd-icon"><i class="spin big"></i></div>
    <h3>Checking for updates…</h3>
    <p class="sub">Asking GitHub for the newest Cherry.</p>`);

  const render = (u) => {
    if (!u || u.error) {
      return show(`
        <div class="upd-icon warn">!</div>
        <h3>Couldn't check right now</h3>
        <p class="sub">${esc((u && u.error) || 'No answer from GitHub.')} You have Cherry ${esc((u && u.current) || S.version || '')}.</p>
        <div class="actions"><button class="btn" data-close>Close</button>
          <button class="btn primary" data-upd="again">Try again</button></div>`);
    }
    if (!u.newer) {
      return show(`
        <div class="upd-icon ok"><svg class="ic" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg></div>
        <h3>You're up to date</h3>
        <p class="sub">Cherry ${esc(u.current)} is the newest version.</p>
        <div class="actions"><button class="btn primary" data-close>Done</button></div>`);
    }
    show(`
      <div class="upd-head">
        <img src="/img/cherry-64.png" alt="" width="44" height="44">
        <div><span class="upd-kicker">New version</span><h3>Cherry ${esc(u.latest)} is out</h3>
          <p class="sub">You have ${esc(u.current)}.</p></div>
      </div>
      <div class="upd-notes">${notesHtml(u.notes) || '<p>No notes for this version.</p>'}</div>
      ${u.dev ? `<p class="note">This copy is the development folder (it has git), so it doesn't update itself —
        pull the changes with git instead.</p>` : `<p class="note">Your sessions, keys, characters and the Output folder stay exactly as they are.</p>`}
      <div class="actions">
        ${u.page ? `<a class="btn quiet" href="${esc(u.page)}" target="_blank" rel="noreferrer">On GitHub</a><span class="sp"></span>` : ''}
        <button class="btn" data-close>Later</button>
        ${u.dev ? '' : '<button class="btn primary" data-upd="go">Update now</button>'}
      </div>`);
  };

  const steps = (n, msg) => show(`
    <div class="upd-icon"><i class="spin big"></i></div>
    <h3>Updating to Cherry ${esc(S.update.latest)}</h3>
    <ol class="upd-steps">
      ${['Downloading', 'Installing', 'Restarting Cherry'].map((t, i) =>
        `<li class="${i < n ? 'done' : i === n ? 'now' : ''}">${t}</li>`).join('')}
    </ol>
    <p class="sub">${esc(msg)}</p>`);

  const go = async () => {
    steps(0, 'Fetching the new version from GitHub…');
    let r;
    try {
      r = await api('/api/update/apply', {});
    } catch (e) {
      return show(`
        <div class="upd-icon warn">!</div>
        <h3>The update didn't go through</h3>
        <p class="sub">${esc(e.message)} Nothing was changed.</p>
        <div class="actions"><button class="btn" data-close>Close</button>
          <button class="btn primary" data-upd="go">Try again</button></div>`);
    }
    steps(2, 'Installed. Cherry is starting again — this page reloads by itself.');
    const until = Date.now() + 60000;
    while (Date.now() < until) {
      await new Promise((ok) => setTimeout(ok, 1200));
      try {
        const st = await api('/api/state');
        if (st.version === r.version) {
          show(`
            <div class="upd-icon ok"><svg class="ic" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg></div>
            <h3>Updated to Cherry ${esc(r.version)}</h3>
            <p class="sub">Reloading…</p>`);
          setTimeout(() => location.reload(), 900);
          return;
        }
      } catch { /* still restarting */ }
    }
    show(`
      <div class="upd-icon warn">!</div>
      <h3>Installed — but Cherry didn't come back</h3>
      <p class="sub">Close Cherry's black window and start it again; you'll be on ${esc(r.version)}.</p>
      <div class="actions"><button class="btn primary" data-close>OK</button></div>`);
  };

  box.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-upd]');
    if (!b) return;
    if (b.dataset.upd === 'go') return go();
    checking();
    render(await checkUpdate(true));
  });
  checking();
  checkUpdate(true).then(render);
}

function renderHome() {
  const connected = Object.values(PROV()).filter((p) => p.has_key).map((p) => p.label);
  $('#keystate').innerHTML = connected.length
    ? 'Connected: ' + esc(connected.join(', ')) + ' · <a data-act="settings">Manage</a>' : '';

  // First run: nothing is connected, so lead with that. The panel is only
  // rewritten when that state flips, so typing a key is never clobbered by the
  // re-render a filter keystroke causes.
  const box = $('#onboard');
  const sig = connected.length ? 'on' : 'off';
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.hidden = !!connected.length;
    box.innerHTML = connected.length ? '' : `
      <h2>Connect a service to begin</h2>
      <p>Cherry sends your prompts to an image or video service you choose. Paste a key for
         at least one &mdash; add more whenever you like, and pick a different one for each session.</p>
      ${servicesHtml()}
      <div style="display:flex;justify-content:flex-end;margin-top:14px">
        <button class="btn primary" data-act="save-keys">Save keys</button>
      </div>`;
  }

  $('#session-filter').hidden = S.sessions.length < 6;
  const q = S.filter.trim().toLowerCase();
  const list = q ? S.sessions.filter((s) => s.name.toLowerCase().includes(q)) : S.sessions;
  const enginesOf = (s) => {
    const ip = s.image_provider || S.settings.image_provider;
    const vp = s.video_provider || S.settings.video_provider;
    return provLabel(ip) + (vp && vp !== ip ? ' → ' + provLabel(vp) : '');
  };
  const n = (v, w) => v + ' ' + w + (v === 1 ? '' : 's');

  $('#session-grid').innerHTML =
    '<button class="scard new" data-act="new-session"><span class="plus">+</span>New session</button>' +
    list.map((s) => `
      <div class="scard" data-session="${s.id}">
        <button class="kebab" data-act="session-menu" data-session="${s.id}" title="More"
                aria-label="Session options">${ic('more')}</button>
        <div class="cover">${s.cover
          ? `<img src="${fileUrl(s.cover)}" alt="" loading="lazy">`
          : '<span class="none">No images yet</span>'}</div>
        <div class="body">
          <h3>${esc(s.name)}</h3>
          <div class="meta">
            <span>${n(s.card_count, 'prompt')}</span><span class="dot">&middot;</span>
            <span>${n(s.image_count, 'image')}</span>
            ${s.video_count ? `<span class="dot">&middot;</span><span>${n(s.video_count, 'clip')}</span>` : ''}
            ${s.busy ? '<span class="dot">&middot;</span><span class="working">working…</span>' : ''}
          </div>
          <div class="engines">${esc(enginesOf(s))}</div>
        </div>
      </div>`).join('');
}

/* ═════════════════════════════════════════════ session shell */

function renderSession() {
  $('#session-name').textContent = S.session.name;
  document.title = S.session.name + ' · Cherry';
  setTab(S.tab);
  refreshSession();
}

function setTab(tab) {
  S.tab = tab;
  $$('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));
  $('#tab-prompts').classList.toggle('on', tab === 'prompts');
  $('#tab-images').classList.toggle('on', tab === 'images');
  $('#tab-videos').classList.toggle('on', tab === 'videos');
  $('#json-help-btn').textContent = 'JSON help · ' +
    (tab === 'prompts' ? 'Images' : 'Videos');
  growAll();
  if (S.session) {
    if (tab === 'prompts') renderCards();
    else if (tab === 'images') renderVideoCards();
    else renderClips();
  }
}

const isShot = (c) => (c.kind || 'shot') === 'shot';
const visibleShots = () => visibleCards().filter(isShot);

function visibleCards() {
  const q = S.cardFilter.trim().toLowerCase();
  if (!q) return S.session.cards;
  return S.session.cards.filter((c) =>
    (c.title || '').toLowerCase().includes(q) ||
    ('image ' + c.seq).includes(q) ||
    String(c.seq) === q);
}

function refreshSession() {
  const s = S.session;
  if (!s) return;
  const images = s.cards.reduce((n, c) => n + c.success_count, 0);
  const clips = s.cards.reduce((n, c) => n + c.video_count, 0);
  $('#count-prompts').textContent = s.cards.filter(isShot).length;
  $('#count-images').textContent = s.cards.length;      // the Clips board: every card
  $('#count-videos').textContent = clips;

  const busyCards = s.cards.filter((c) => c.busy || c.video_busy).length;
  const busy = $('#busy');
  busy.hidden = !busyCards;
  if (busyCards) $('span', busy).textContent = busyCards + ' generating…';

  $('[data-act="download-all"]').classList.toggle('off', !images);
  $('[data-act="download-videos"]').classList.toggle('off', !clips);

  const chip = (kind, label) => {
    const e = kind === 'video' ? s.video_engine : s.image_engine;
    const own = s.cards.filter((c) => (kind === 'video' ? c.video_engine : c.engine).own).length;
    return `<button class="chip" data-act="engine" data-kind="${kind}"
        title="Change what makes ${kind === 'video' ? 'clips' : 'images'} in this session">
      <span class="k">${label}</span><span class="v">${esc(specOf(e.provider, kind, e.model).label)}</span>
      <span class="p">${esc(provLabel(e.provider))}</span>
      ${own ? `<span class="more">+${own} custom</span>` : ''}</button>`;
  };
  $$('[data-chip]').forEach((box) => {
    const html = chip(box.dataset.chip, box.dataset.chip === 'video' ? 'Clips' : 'Images');
    if (box.dataset.sig !== html) { box.dataset.sig = html; box.innerHTML = html; }
  });

  renderAllSet();
  // Only the visible tab is rendered. The others are rebuilt when you switch
  // to them, which costs nothing and saves doing it every 2.5s for a grid
  // holding two dozen full-size stills.
  if (S.tab === 'prompts') renderCards();
  else if (S.tab === 'images') renderVideoCards();
  else renderClips();

  const list = visibleCards();
  const anyOpen = !list.length || list.some((c) => !S.collapsed.has(c.id));
  $('#fold-all').textContent = anyOpen ? 'Minimise all' : 'Expand all';
  const anyOpenV = !list.length || list.some((c) => !S.vcollapsed.has(c.id));
  $('#fold-all-v').textContent = anyOpenV ? 'Minimise all' : 'Expand all';
}

/* The session-wide settings, shown as the values themselves rather than behind
   an "Apply to all" dialog — so what every prompt is set to is readable at a
   glance, and changing one stamps the lot. "mixed" appears only when the cards
   genuinely disagree. */
function settingsRow(cards, fields) {
  if (!cards.length || !fields.length) return '';
  const shared = (key) => {
    const v = cards[0][key];
    return cards.every((c) => c[key] === v) ? v : null;
  };
  return '<span class="alllbl">All ' + cards.length + '</span>' +
    fields.map(([key, values, suffix]) => {
      const cur = shared(key);
      return `<select class="sel" data-all="${key}" title="Apply to every prompt">` +
        (cur === null ? '<option value="" selected>mixed</option>' : '') +
        values.map((v) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>` +
          esc(v + (suffix || '')) + '</option>').join('') +
        '</select>';
    }).join('');
}

function renderAllSet() {
  const cards = S.session.cards;
  // The row speaks the session engine's vocabulary: its values, nothing else.
  const row = (kind) => {
    const e = kind === 'video' ? S.session.video_engine : S.session.image_engine;
    const ctls = specOf(e.provider, kind, e.model).controls || {};
    return CTL[kind].filter(([key]) => ctls[key] && ctls[key].values.length)
      .map(([key, field]) => [field, ctls[key].values.map(String), key === 'duration' ? 's' : '']);
  };
  $('#allset').innerHTML = settingsRow(cards, row('image'));
  $('#allsetv').innerHTML = settingsRow(cards, row('video'));
}

/* ═════════════════════════════════════════════ shared card bits */

function autoGrow(el) {
  if (!el || !el.isConnected || !el.offsetParent) return;
  el.style.height = 'auto';
  const h = el.scrollHeight;
  // box-sizing is border-box but scrollHeight excludes the border, so add it
  // back or the last two pixels of the final line stay clipped.
  if (h > 0) el.style.height = (h + el.offsetHeight - el.clientHeight) + 'px';
}

/* A hidden element measures as zero, so a textarea can only be sized once its
   panel is on screen. Runs synchronously: requestAnimationFrame never fires
   while the window is hidden, which would leave prompts clipped. */
function growAll() {
  $$('.cards textarea').forEach((t) => {
    if (t !== document.activeElement || t.style.height === '') autoGrow(t);
  });
}
addEventListener('resize', growAll);
document.addEventListener('visibilitychange', () => { if (!document.hidden) growAll(); });

function stateLabel(latest, dirty) {
  if (!latest) return { cls: '', text: '<span>Not generated yet</span>' };
  if (INFLIGHT.includes(latest.kie_state)) {
    return { cls: 'work', text: '<i class="spin"></i><span>' +
      (latest.kie_state === 'pending' ? 'sending…' : latest.kie_state + '…') + '</span>' };
  }
  if (latest.kie_state === 'fail') {
    return { cls: 'bad', text: '<span title="' + esc(latest.fail_msg || '') + '">✕ ' +
      esc(latest.fail_msg || 'failed') + '</span>' };
  }
  if (dirty) return { cls: '', text: '<span>Edited since take ' + latest.n + '</span>' };
  return { cls: 'ok', text: '<span>✓ take ' + latest.n + ' ready</span>' };
}

function selectHtml(name, cardId, values, current, suffix) {
  return `<select class="sel" data-field="${name}" data-card="${cardId}">` +
    values.map((v) => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>` +
      esc(v + (suffix || '')) + '</option>').join('') + '</select>';
}

function refSrc(r) {
  return r.kind === 'url' ? esc(r.upload_url || '') : fileUrl(r.thumb || '');
}

/* Clicking a reference opens a preview; the + opens the picker. */
function refsHtml(card, kind) {
  const list = kind === 'video' ? card.video_refs : card.refs;
  const act = kind === 'video' ? 'pick-vrefs' : 'pick-refs';
  const off = kind === 'video' ? 'unvref' : 'unref';
  const thumbs = list.map((r) => `
    <div class="rthumb" data-act="preview-ref" data-ref="${r.id}" title="${esc(r.name)} — click to preview">
      <img src="${refSrc(r)}" alt="" loading="lazy"
           onerror="this.parentNode.classList.add('noimg');this.remove()">
      <button class="x" data-act="${off}" data-card="${card.id}" data-ref="${r.id}"
              title="Remove from this prompt">✕</button>
    </div>`).join('');
  const room = list.length < 8;
  return `<div class="refs">${thumbs}${room
    ? `<button class="refadd" data-act="${act}" data-card="${card.id}"
               title="Add a reference image">+</button>` : ''}</div>`;
}

function takesHtml(gens, kind) {
  if (!gens.length) return '';
  const tiles = gens.slice(0, 8).map((g) => {
    if (g.kie_state === 'success' && g.file) {
      const inner = kind === 'video'
        ? '<video src="' + fileUrl(g.file) + '" muted preload="metadata"></video>'
        : '<img src="' + fileUrl(g.file) + '" alt="" loading="lazy">';
      return `<div class="take" data-gen="${g.id}" title="Take ${g.n}">${inner}
                <span class="tag">v${g.n}</span></div>`;
    }
    if (g.kie_state === 'fail') {
      return `<div class="take fail" data-gen="${g.id}" title="${esc(g.fail_msg || 'failed')}">✕
                <span class="tag">v${g.n}</span></div>`;
    }
    return `<div class="take pend" title="working"><i class="spin"></i>
              <span class="tag">v${g.n}</span></div>`;
  }).join('');
  const more = gens.length > 8 ? `<span class="status">+${gens.length - 8} older</span>` : '';
  return `<div class="takes">${tiles}${more}</div>`;
}

function headTools(c, isFirst, isLast, folded, foldAct, scope = 'all', extra = '') {
  return `
    <button class="fold" data-act="${foldAct}" data-card="${c.id}"
            title="${folded ? 'Expand' : 'Minimise'}">${ic(folded ? 'right' : 'down')}</button>
    <span class="seq">${c.seq}</span>
    <input class="title-in" data-field="title" data-card="${c.id}"
           value="${esc(c.title || '')}" placeholder="${isShot(c) ? 'Image' : 'Clip'} ${c.seq}">
    <div class="tools">
      <button data-act="up" data-card="${c.id}" data-scope="${scope}" title="Move left"${isFirst ? ' disabled' : ''}>${ic('left')}</button>
      <button data-act="down" data-card="${c.id}" data-scope="${scope}" title="Move right"${isLast ? ' disabled' : ''}>${ic('right')}</button>
      ${extra}
      <button data-act="dup" data-card="${c.id}" title="Duplicate">${ic('copy')}</button>
      <button class="rm" data-act="del-card" data-card="${c.id}" title="Delete">${ic('x')}</button>
    </div>`;
}

/* A folded card still has to show what it is carrying, so the references
   survive as small chips rather than disappearing entirely. */
function foldedRefs(list) {
  if (!list.length) return '';
  return '<div class="minirefs" title="' + esc(list.map((r) => r.name).join(', ')) + '">' +
    list.slice(0, 4).map((r) => `<img src="${refSrc(r)}" alt=""
        data-act="preview-ref" data-ref="${r.id}" onerror="this.remove()">`).join('') +
    (list.length > 4 ? '<span class="more">+' + (list.length - 4) + '</span>' : '') +
    '</div>';
}

const ADD_TILE = `
  <button class="card addcard" data-act="add-card" title="Add a prompt">
    <span class="plus">+</span><span class="lbl">Add prompt</span>
  </button>`;

/* ═════════════════════════════════════════════ prompts tab */

function cardHtml(c, isFirst, isLast) {
  const st = stateLabel(c.latest, c.dirty);
  const label = !c.latest || c.dirty ? 'Generate' : 'Generate again';
  const folded = S.collapsed.has(c.id);
  const peek = (c.prompt || '').trim();
  return `
  <article class="card${folded ? ' collapsed' : ''}${c.busy ? ' is-busy' : ''}${c.latest?.kie_state === 'fail' ? ' is-fail' : ''}${c.success_count ? ' has-image' : ''}"
           data-card="${c.id}">
    <div class="card-head">${headTools(c, isFirst, isLast, folded, 'fold', 'shots')}</div>
    <div class="peek" data-act="fold" data-card="${c.id}">${peek ? esc(peek) : '<i>No prompt yet</i>'}</div>
    ${folded ? foldedRefs(c.refs) : ''}
    <textarea class="prompt-in" data-field="prompt" data-card="${c.id}" rows="4"
              placeholder="Describe the image…">${esc(c.prompt)}</textarea>
    ${folded ? '' : refsHtml(c, 'image')}
    <div class="card-set">
      ${engineSelectHtml(c, 'image')}
      ${controlSelects(c, 'image')}
    </div>
    <div class="card-foot">
      <span class="status ${st.cls}">${st.text}</span>
      <span class="sp"></span>
      <button class="btn primary" data-act="gen" data-card="${c.id}"
              ${c.busy || S.firing.has(c.id) ? 'disabled' : ''}>${
        c.busy ? 'Working…' : label}</button>
    </div>
    ${takesHtml(c.generations, 'image')}
  </article>`;
}

/* Repaint only what actually changed.
 *
 * The poll runs every 2.5s while anything is generating. Rebuilding the grid's
 * innerHTML each time tore down and recreated every card - and with it every
 * <img> - which made the page flash and forced the browser to decode two dozen
 * full-size stills over and over for no reason.
 *
 * So each card keeps the exact HTML it was built from on an expando property.
 * If the newly rendered HTML for that card is identical, the element is left
 * completely alone: not replaced, not moved, not re-decoded.
 */
function paintGrid(wrap, cards, tiles, foldedSet, addTile) {
  wrap.classList.toggle('compact', cards.length > 0
    && cards.every((c) => foldedSet.has(c.id)));

  // Never redraw a card the user is TYPING in - that would move the caret.
  //
  // Only a text field counts. A focused button has no caret to protect, and
  // treating it as one froze the card it sits in: clicking the x on a
  // reference removed it on the server and then skipped the repaint.
  const active = document.activeElement;
  const typing = active && (active.tagName === 'TEXTAREA'
    || (active.tagName === 'INPUT' && active.type !== 'checkbox'
        && active.type !== 'radio' && active.type !== 'file'));
  const held = typing ? active.closest('.card') : null;
  const holdId = held && wrap.contains(held) ? held.dataset.card : null;

  const build = (html) => {
    const box = document.createElement('div');
    box.innerHTML = html;
    return box.firstElementChild;
  };

  const have = new Map();
  let tile = null;
  [...wrap.children].forEach((el) => {
    if (el.classList.contains('addcard')) tile = el;
    else if (el.dataset.card) have.set(el.dataset.card, el);
    else el.remove();                          // an empty-state message
  });

  const wanted = new Set(tiles.map((t) => String(t.id)));
  have.forEach((el, id) => { if (!wanted.has(id)) el.remove(); });

  let prev = null, touched = false;
  for (const t of tiles) {
    const key = String(t.id);
    let el = have.get(key);

    if (!el) {
      el = build(t.html);
      el._html = t.html;
      touched = true;
    } else if (el._html !== t.html) {
      if (key === holdId) {
        // Patch only the parts that cannot hold focus, so the caret stays put.
        const fresh = build(t.html);
        el.className = fresh.className;
        const foot = $('.card-foot', el), freshFoot = $('.card-foot', fresh);
        if (foot && freshFoot) foot.innerHTML = freshFoot.innerHTML;
        const oldT = $('.takes', el), newT = $('.takes', fresh);
        if (newT && oldT) oldT.replaceWith(newT);
        else if (newT) el.append(newT);
        else if (oldT) oldT.remove();
        // Deliberately not recording _html: the card is mid-edit, so the next
        // pass must compare against the real thing again.
      } else {
        const fresh = build(t.html);
        fresh._html = t.html;
        el.replaceWith(fresh);
        el = fresh;
        touched = true;
      }
    }

    const shouldFollow = prev ? prev.nextElementSibling : wrap.firstElementChild;
    if (shouldFollow !== el) { wrap.insertBefore(el, shouldFollow); touched = true; }
    prev = el;
  }

  if (addTile) {
    if (!tile) { tile = build(addTile); touched = true; }
    if (tile.previousElementSibling !== prev || !tile.parentNode) {
      wrap.insertBefore(tile, prev ? prev.nextElementSibling : null);
    }
  } else if (tile) {
    tile.remove();
  }

  if (touched) growAll();
}

function renderCards() {
  const cards = visibleShots();
  const wrap = $('#cards');
  if (!S.session.cards.some(isShot)) {
    wrap.innerHTML = ADD_TILE +
      '<div class="empty side">No prompts yet — add one, or import a JSON payload.</div>';
    return;
  }
  if (!cards.length) {
    wrap.classList.remove('compact');
    wrap.innerHTML = '<div class="empty side">Nothing matches that filter.</div>';
    return;
  }
  const all = S.session.cards.filter(isShot);
  const tiles = cards.map((c) => ({
    id: c.id,
    html: cardHtml(c, c.seq === all[0].seq, c.seq === all[all.length - 1].seq),
  }));
  paintGrid(wrap, cards, tiles, S.collapsed, S.cardFilter ? '' : ADD_TILE);
}

/* ═════════════════════════════════════════════ images → video tab */

function versionsHtml(c) {
  const ok = c.generations.filter((g) => g.kie_state === 'success' && g.file);
  const activeId = c.active ? c.active.id : null;
  const others = ok.filter((g) => g.id !== activeId);
  if (!ok.length) return '';
  const open = S.openVersions.has(c.id);
  if (!others.length) {
    return `<div class="vermeta">take ${c.active ? c.active.n : '—'} · only version</div>`;
  }
  return `
    <button class="verbtn" data-act="versions" data-card="${c.id}">
      ${open ? '▾' : '▸'} ${others.length} older version${others.length === 1 ? '' : 's'}
    </button>
    ${open ? `<div class="verstack">${others.map((g) => `
      <div class="verrow" data-act="activate" data-gen="${g.id}" title="Use this one for the clip">
        <img src="${fileUrl(g.file)}" alt="" loading="lazy">
        <span class="vn">take ${g.n}</span>
        <span class="use">Use this</span>
        <button class="rm" data-act="del-take" data-gen="${g.id}"
                title="Remove this version from the app">✕</button>
      </div>`).join('')}</div>` : ''}`;
}

/* Keyed, signature-based patching.
 *
 * The poll runs every 2.5s while anything is generating. Rebuilding innerHTML
 * on each one destroyed and recreated every <video> element, which reset
 * playback and made the page jump - you could not watch one clip while another
 * was rendering. So each tile carries a key and a signature: unchanged tiles
 * are left completely alone, and only what actually differs is touched.
 */
function patchGrid(grid, tiles, emptyHtml) {
  if (!tiles.length) {
    if (grid.dataset.empty !== emptyHtml) {
      grid.innerHTML = '<div class="empty">' + emptyHtml + '</div>';
      grid.dataset.empty = emptyHtml;
    }
    return;
  }
  delete grid.dataset.empty;

  const have = new Map();
  [...grid.children].forEach((el) => {
    if (el.dataset.key) have.set(el.dataset.key, el);
    else el.remove();                       // the empty-state placeholder
  });

  const wanted = new Set(tiles.map((t) => t.key));
  have.forEach((el, key) => { if (!wanted.has(key)) el.remove(); });

  let prev = null;
  for (const t of tiles) {
    let el = have.get(t.key);
    if (!el || el.dataset.sig !== t.sig) {
      const holder = document.createElement('div');
      holder.innerHTML = t.html;
      const fresh = holder.firstElementChild;
      fresh.dataset.key = t.key;
      fresh.dataset.sig = t.sig;
      if (el) el.replaceWith(fresh); else grid.append(fresh);
      el = fresh;
    }
    // Only move it if it is genuinely out of order: re-inserting a <video>
    // restarts it.
    const shouldFollow = prev ? prev.nextElementSibling : grid.firstElementChild;
    if (shouldFollow !== el) grid.insertBefore(el, shouldFollow);
    prev = el;
  }
}

function renderClips() {
  const grid = $('#clips');
  const tiles = [];
  let waiting = 0;

  for (const c of visibleCards()) {
    // A card being rendered gets its own tile, so pressing Generate clip and
    // coming here shows something happening rather than an empty page.
    if (c.video_busy) {
      waiting += 1;
      const g = c.video_latest;
      const state = g && g.kie_state === 'pending' ? 'sending…'
                  : g ? g.kie_state + '…' : 'starting…';
      tiles.push({
        key: 'pending-' + c.id,
        sig: 'pending|' + state,
        html: '<div class="gcard clip rendering">'
          + '<div class="shot">'
          + (c.active ? '<img class="ghost" src="' + fileUrl(c.active.file) + '" alt="">' : '')
          + '<div class="veil"><i class="spin big"></i><span>' + esc(state) + '</span>'
          + '<small>clips take a few minutes</small></div>'
          + '</div>'
          + '<div class="cap"><span class="num">' + c.seq + '</span>'
          + '<span class="nm">' + esc(c.title || 'Image ' + c.seq) + '</span>'
          + '<span style="margin-left:auto;color:var(--warn)">rendering</span></div>'
          + '</div>',
      });
    }

    const done = c.video_generations.filter((g) => g.kie_state === 'success' && g.file);
    for (const gen of (S.allTakes ? done : done.slice(0, 1))) {
      tiles.push({
        key: 'gen-' + gen.id,
        sig: gen.id + '|' + gen.file + '|' + c.seq + '|' + (c.title || ''),
        html: '<div class="gcard clip">'
          + '<div class="shot"><video src="' + fileUrl(gen.file)
          + '" controls preload="metadata" playsinline></video></div>'
          + '<a class="dl" href="' + fileUrl(gen.file) + '" data-stop download="'
          + esc(downloadName(c, gen, 'video')) + '" title="Download">Download</a>'
          + '<div class="cap" data-gen="' + gen.id + '">'
          + '<span class="num">' + c.seq + '</span>'
          + '<span class="nm">' + esc(c.title || 'Image ' + c.seq) + '</span>'
          + '<span style="margin-left:auto;color:var(--dim)">v' + gen.n + '</span></div>'
          + '</div>',
      });
    }
  }

  const ready = tiles.length - waiting;
  const noun = S.allTakes ? 'take' : 'clip';
  $('#clips-pending').textContent = waiting
    ? waiting + ' rendering' + (ready ? ' · ' + ready + ' ready' : '')
    : (ready ? ready + ' ' + noun + (ready === 1 ? '' : 's') : '');

  patchGrid(grid, tiles,
    'No clips yet — write the motion on the Images tab and press Generate clip.');
}

const MODE_LABEL = { frames: 'Frames', refs: 'References', text: 'Text only' };
const MEDIA_LABEL = { images: 'Images', videos: 'Videos', audio: 'Audio' };
const MEDIA_ONE = { images: 'image', videos: 'video', audio: 'audio' };

function clipSpec(c) { return specOf(c.video_engine.provider, 'video', c.video_engine.model); }
function clipModeCaps(c) { return ((clipSpec(c).modes || {})[c.clip_mode]) || {}; }

/* The prompt tag for the n-th reference of a kind ("@Image2"), when the model's
   docs say how references are named. */
function refToken(c, kind, n) {
  const sx = (clipModeCaps(c).syntax || {})[kind];
  return sx ? sx.replace('{n}', n) : '';
}

function frameSlot(c, which) {
  const f = c[which];
  const name = which === 'first' ? 'First frame' : 'Last frame';
  if (!f) {
    return `<button class="fslot empty" data-act="pick-frame" data-which="${which}" data-card="${c.id}">
      <span class="plus">+</span><span class="lbl">${name}</span>
      <small>${which === 'first' ? 'a shot, an upload, a clip ending…' : 'optional — where the clip lands'}</small></button>`;
  }
  const img = f.thumb && !f.missing
    ? `<img src="${f.ref_id && !f.gen_id && /^https?:/.test(f.thumb) ? esc(f.thumb) : fileUrl(f.thumb)}" alt="" loading="lazy">`
    : `<div class="fmissing">${esc(f.label)}</div>`;
  return `<div class="fslot${f.missing ? ' missing' : ''}">
      <div class="fimg" data-act="pick-frame" data-which="${which}" data-card="${c.id}" title="Change the ${name.toLowerCase()}">${img}</div>
      <div class="fcap"><span class="k">${name}</span><span class="v" title="${esc(f.label)}">${esc(f.label)}</span>
        <button class="x" data-act="clear-frame" data-which="${which}" data-card="${c.id}" title="No ${name.toLowerCase()}">${ic('x')}</button></div>
    </div>`;
}

function mediaThumb(r) {
  const media = r.media || 'image';
  if (media === 'video') {
    return `<video src="${refSrc(r)}" muted preload="metadata"></video><span class="mk">${ic('film')}</span>`;
  }
  if (media === 'audio') return `<span class="aud">${ic('note')}<em>${esc(r.name)}</em></span>`;
  return `<img src="${refSrc(r)}" alt="" loading="lazy" onerror="this.parentNode.classList.add('noimg');this.remove()">`;
}

function refsBlock(c) {
  const caps = clipModeCaps(c);
  const rows = [];
  for (const kind of ['images', 'videos', 'audio']) {
    if (!caps[kind]) continue;
    const list = c.vref_media[kind] || [];
    const pre = kind === 'images' && c.first_as_ref && c.first ? 1 : 0;
    const used = list.length + pre;
    const chips = [];
    if (pre) {
      chips.push(`<div class="rthumb fixed" title="${esc(c.first.label)} — sent as the first reference">
        ${c.first.thumb && !c.first.missing ? `<img src="${fileUrl(c.first.thumb)}" alt="">` : ''}
        ${refToken(c, kind, 1) ? `<span class="tok">${esc(refToken(c, kind, 1))}</span>` : ''}</div>`);
    }
    list.forEach((r, i) => {
      const tok = refToken(c, kind, i + 1 + pre);
      chips.push(`<div class="rthumb m-${MEDIA_ONE[kind]}" data-act="${tok ? 'insert-token' : 'preview-ref'}" data-token="${esc(tok)}"
          data-ref="${r.id}" data-card="${c.id}" title="${esc(r.name)}${tok ? ' — click to put ' + esc(tok) + ' in the prompt' : ''}">
        ${mediaThumb(r)}${tok ? `<span class="tok">${esc(tok)}</span>` : ''}
        <button class="x" data-act="unvref" data-card="${c.id}" data-ref="${r.id}" title="Remove">✕</button></div>`);
    });
    rows.push(`<div class="mrow"><div class="mhead">${MEDIA_LABEL[kind]}<span class="cnt${used > caps[kind] ? ' over' : ''}">${used} / ${caps[kind]}</span></div>
      <div class="refs">${chips.join('')}${used < caps[kind] ? `<button class="refadd" data-act="pick-media" data-media="${kind}" data-card="${c.id}" title="Add ${MEDIA_LABEL[kind].toLowerCase()}">+</button>` : ''}</div></div>`);
  }
  return `<div class="mrows">${rows.join('')}</div>`;
}

function videoCardHtml(c, isFirst, isLast) {
  const st = stateLabel(c.video_latest, c.video_dirty);
  const label = !c.video_latest || c.video_dirty ? 'Generate clip' : 'Generate again';
  const folded = S.vcollapsed.has(c.id);
  const peek = (c.video_prompt || '').trim();
  const mode = c.clip_mode;
  const caps = clipModeCaps(c);

  const modeSeg = (c.clip_modes || []).length > 1 ? `<div class="seg sm modeseg">${['frames', 'refs', 'text']
    .filter((m) => c.clip_modes.includes(m))
    .map((m) => `<button data-act="clip-mode" data-mode="${m}" data-card="${c.id}"${m === mode ? ' class="on"' : ''}>${MODE_LABEL[m]}</button>`)
    .join('')}</div>` : (mode ? `<div class="modeone">${MODE_LABEL[mode]} only</div>` : '');

  let body = '';
  if (mode === 'frames') {
    body = `<div class="frames${caps.last ? ' two' : ''}">${frameSlot(c, 'first')}${caps.last ? frameSlot(c, 'last') : ''}</div>`
      + (c.first && c.first.src === 'self' && isShot(c) ? versionsHtml(c) : '');
  } else if (mode === 'refs') {
    body = refsBlock(c);
  } else if (mode === 'text') {
    body = '<div class="textnote">Text only — the prompt is all that is sent.</div>';
  }

  // Folded: the first picture (or what kind of clip it is) and a few badges.
  const thumb = c.first && c.first.thumb && !c.first.missing && mode === 'frames'
    ? `<div class="shotimg"><img src="${fileUrl(c.first.thumb)}" alt="" loading="lazy"></div>`
    : `<div class="shotimg empty-shot">${MODE_LABEL[mode] || '—'}</div>`;
  const badges = '<span class="fbadge ' + (peek ? 'has' : 'none') + '">'
    + (peek ? 'prompt' : 'no prompt') + '</span>'
    + (c.video_busy ? '<span class="fbadge work"><i class="spin"></i></span>'
       : c.video_count ? '<span class="fbadge ok">' + c.video_count + ' done</span>' : '');

  const placeholder = mode === 'refs'
    ? 'Describe the clip — click a reference to put its tag here…'
    : mode === 'text' ? 'Describe the whole clip: subject, action, camera…'
    : 'Describe only the motion and the camera move…';

  return `
  <article class="card vcard ${isShot(c) ? 'is-shot' : 'is-clip'}${folded ? ' collapsed' : ''}${c.video_busy ? ' is-busy' : ''}${c.video_latest?.kie_state === 'fail' ? ' is-fail' : ''}${c.video_count ? ' has-image' : ''}"
           data-card="${c.id}">
    <div class="card-head">${headTools(c, isFirst, isLast, folded, 'fold-v', 'all',
      `<button data-act="insert-clip" data-card="${c.id}" title="Add a clip right after this one">${ic('plus')}</button>`)}</div>
    <div class="vkind">${isShot(c) ? 'Shot · has an image prompt' : 'Standalone clip'}</div>
    ${folded ? `<div class="shotwrap" data-act="fold-v" data-card="${c.id}">${thumb}<div class="fbadges">${badges}</div></div>` : ''}
    ${folded ? '' : engineSelectHtml(c, 'video')}
    ${folded ? '' : modeSeg}
    ${folded ? '' : body}
    ${!folded && c.clip_hidden ? `<div class="hiddennote">${c.clip_hidden} input${c.clip_hidden === 1 ? '' : 's'} kept but not used by this model</div>` : ''}
    ${!folded && c.clip_problem ? `<div class="problem">${esc(c.clip_problem)}</div>` : ''}
    ${folded ? '' : `<div class="peek" data-act="fold-v" data-card="${c.id}">${peek ? esc(peek) : '<i>No clip prompt yet</i>'}</div>`}
    <textarea class="prompt-in video-in" data-field="video_prompt" data-card="${c.id}" rows="3"
              placeholder="${placeholder}">${esc(c.video_prompt || '')}</textarea>
    <div class="card-set">${controlSelects(c, 'video')}</div>
    <div class="card-foot">
      <span class="status ${st.cls}">${st.text}</span>
      <span class="sp"></span>
      <button class="btn primary" data-act="gen-video" data-card="${c.id}" title="${esc(c.clip_problem || '')}"
              ${c.video_busy || !c.can_clip || !peek || S.firing.has('v' + c.id) ? 'disabled' : ''}>${
        c.video_busy ? 'Working…' : label}</button>
    </div>
    ${takesHtml(c.video_generations, 'video')}
  </article>`;
}

const ADD_CLIP = `
  <button class="card addcard" data-act="add-clip" title="Add a clip with no image prompt">
    <span class="plus">+</span><span class="lbl">Add clip</span>
  </button>`;

function renderVideoCards() {
  const cards = visibleCards();
  const wrap = $('#vcards');
  if (!S.session.cards.length) {
    wrap.innerHTML = ADD_CLIP + '<div class="empty side">No clips yet — add one, or write image prompts first and animate them here.</div>';
    return;
  }
  if (!cards.length) {
    wrap.classList.remove('compact');
    wrap.innerHTML = '<div class="empty side">Nothing matches that filter.</div>';
    return;
  }
  const all = S.session.cards;
  const tiles = cards.map((c) => ({
    id: c.id,
    html: videoCardHtml(c, c.seq === all[0].seq, c.seq === all[all.length - 1].seq),
  }));
  paintGrid(wrap, cards, tiles, S.vcollapsed, S.cardFilter ? '' : ADD_CLIP);
}

/* ═════════════════════════════════════════════ modal plumbing */

function modal(html, { wide = false, mid = false, onClose = null } = {}) {
  const scrim = document.createElement('div');
  scrim.className = 'scrim';
  scrim.innerHTML = `<div class="modal${wide ? ' wide' : ''}${mid ? ' mid' : ''}">${html}</div>`;
  scrim.addEventListener('mousedown', (e) => { if (e.target === scrim) close(); });
  // Escape closes only the topmost modal, so a dialog opened over another one
  // does not take both down with it.
  const onKey = (e) => {
    if (e.key === 'Escape' && $('#modal-root').lastElementChild === scrim) close();
  };
  document.addEventListener('keydown', onKey);
  let shut = false;
  function close() {
    if (shut) return;
    shut = true;
    document.removeEventListener('keydown', onKey);
    scrim.remove();
    if (onClose) onClose();
  }
  scrim.close = close;
  $('#modal-root').append(scrim);
  const first = $('input, textarea, button.primary', scrim);
  if (first) setTimeout(() => first.focus(), 40);
  return scrim;
}

/* Everything destructive asks here rather than through window.confirm — a
   browser-chrome dialog at the top of the screen, in a different typeface, is
   jarring and does not look like part of the app. */
function confirmModal({ title, body, ok = 'Delete', danger = true }) {
  return new Promise((resolve) => {
    let answer = false;
    const m = modal(`
      <h3>${esc(title)}</h3>
      <p class="sub">${body}</p>
      <div class="actions">
        <button class="btn" data-close>Cancel</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" id="cf-go">${esc(ok)}</button>
      </div>`, { onClose: () => resolve(answer) });
    $('#cf-go', m).onclick = () => { answer = true; m.close(); };
  });
}

const optionsHtml = (vals, cur) =>
  vals.map((v) => `<option${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');

async function copyText(text, label = 'Copied') {
  try { await navigator.clipboard.writeText(text); toast(label, 'good'); }
  catch { toast('Could not reach the clipboard — select the text and press Ctrl+C', 'bad'); }
}

/* ═════════════════════════════════════════════ previews */

function openPreview({ src, title, sub, video = false, actions = '' }) {
  const m = modal(`
    <div class="preview">
      <div class="big">${video
        ? `<video src="${src}" controls autoplay loop></video>`
        : `<img src="${src}" alt="">`}</div>
      <div class="pmeta">
        <div>
          <h4>${esc(title)}</h4>
          ${sub ? `<div class="meta">${esc(sub)}</div>` : ''}
        </div>
        <span class="sp"></span>
        ${actions}
        <button class="btn" data-close>Close</button>
      </div>
    </div>`, { wide: true });
  return m;
}

function openRefPreview(refId) {
  const r = S.session.refs.find((x) => x.id === refId)
    || S.session.cards.flatMap((c) => c.refs.concat(c.video_refs))
        .find((x) => x.id === refId);
  if (!r) return;
  openPreview({
    src: refSrc(r),
    title: r.name,
    sub: r.kind === 'character' ? 'Character · available in every session'
       : r.kind === 'url' ? 'Linked image' : 'Reference in this session',
  });
}

/* ═════════════════════════════════════════════ settings */

/* One row per service: its key, and where to get one. Used on the first-run
   panel and in the Services dialog, so there is only one place that knows how a
   key is entered. */
function servicesHtml() {
  return '<div class="svc-list">' + Object.entries(PROV()).map(([id, p]) => `
    <div class="svc" data-svc="${id}">
      <div>
        <h4>${badge(id, 20)} ${esc(p.label)} <span class="pill${p.has_key ? ' on' : ''}">${p.has_key ? 'Connected' : 'No key'}</span></h4>
        <p class="blurb">${esc(p.blurb)}</p>
        <div class="kinds">${p.kinds.map((k) => k === 'video' ? 'clips' : 'images').join(' + ')}</div>
      </div>
      <div>
        <div class="field">
          <input type="password" data-key="${id}" autocomplete="off"
                 placeholder="${p.has_key ? esc(p.key_hint) + ' — paste to replace' : 'Paste your API key'}">
          ${p.has_key ? `<button class="btn sm" data-act="remove-key" data-provider="${id}">Remove</button>` : ''}
        </div>
        <a class="getkey" href="${esc(p.key_url)}" target="_blank" rel="noreferrer">Get a key</a>
      </div>
    </div>`).join('') + '</div>';
}

async function saveKeys(root) {
  const keys = {};
  $$('input[data-key]', root).forEach((i) => { if (i.value.trim()) keys[i.dataset.key] = i.value.trim(); });
  if (!Object.keys(keys).length) return false;
  const r = await api('/api/settings', { keys });
  S.providers = r.providers;
  return true;
}

function openSettings(focus) {
  const m = modal(`
    <h3>Services</h3>
    <p class="sub">Connect the services you want to generate with. A key is stored in this
      app's local database and only ever sent to the service it belongs to. Pick which one
      each session uses when you create it.</p>
    ${servicesHtml()}
    <div class="actions">
      <button class="btn" data-close>Close</button>
      <button class="btn primary" id="set-save">Save keys</button>
    </div>`, { mid: true });
  const target = focus && $('input[data-key="' + focus + '"]', m);
  if (target) setTimeout(() => { target.scrollIntoView({ block: 'center' }); target.focus(); }, 60);
  $('#set-save', m).onclick = async () => {
    try {
      const saved = await saveKeys(m);
      m.close();
      toast(saved ? 'Keys saved' : 'Nothing changed', saved ? 'good' : '');
      await loadHome();
      if (S.session) refreshSession();
    } catch (e) { toast(e.message, 'bad'); }
  };
}

/* ═════════════════════════════════════════════ JSON help */

function openJsonHelp(kind) {
  kind = kind || (S.tab === 'images' ? 'video' : 'image');
  const shots = Math.max(1, S.session ? S.session.cards.length : 16) || 16;
  const m = modal(`
    <h3>JSON help</h3>
    <p class="sub">Build the prompt that makes ChatGPT write your JSON, then import what it gives back.</p>
    <div class="seg" id="jh-seg">
      <button data-kind="image"${kind === 'image' ? ' class="on"' : ''}>Images</button>
      <button data-kind="video"${kind === 'video' ? ' class="on"' : ''}>Videos</button>
    </div>
    <div id="jh-body"></div>`, { wide: true });

  function paint(k) {
    const forImages = k === 'image';
    $$('#jh-seg button', m).forEach((b) => b.classList.toggle('on', b.dataset.kind === k));
    $('#jh-body', m).innerHTML = `
      <div class="jhbar">
        ${forImages ? `<label class="inl">Shots
          <input type="number" id="jh-shots" min="1" max="60" value="${shots}">
        </label>
        <label class="inl">Art direction
          <select id="jh-design">${DESIGNS.map((d) =>
            `<option value="${d.id}">${esc(d.label)}</option>`).join('')}</select>
        </label>`
        : `<span class="inl static">Shots<b>${shots} — from your prompts</b></span>`}
        <span class="sp"></span>
        <button class="btn" id="jh-copy-json">Copy example</button>
        <button class="btn primary" id="jh-copy">Copy the prompt</button>
      </div>
      <p class="note" id="jh-note"></p>
      <div class="jhcols">
        <section class="jhcol">
          <div class="libhead"><h4>Paste this into ChatGPT</h4></div>
          <p class="note">Attach or paste your script after it. ${forImages
            ? 'It returns one image prompt per shot.'
            : 'Paste your image shot titles too — the clip prompts are matched back to them by number.'}</p>
          <textarea id="jh-main" spellcheck="false" readonly></textarea>
        </section>
        <section class="jhcol">
          <div class="libhead"><h4>What Cherry imports</h4></div>
          <p class="note">${forImages
            ? 'No <code>image_input</code> — references are attached on the card. Anything that is not a real link is dropped.'
            : 'No <code>image_urls</code> — the clip uses the image on that card. <code>seq</code> is what pins a prompt to a shot number.'}</p>
          <textarea id="jh-ex" spellcheck="false" readonly>${esc(
            forImages ? EXAMPLE_IMAGE_JSON : EXAMPLE_VIDEO_JSON)}</textarea>
        </section>
      </div>
      <div class="actions">
        <button class="btn" data-close>Close</button>
        <button class="btn primary" id="jh-import">Import JSON…</button>
      </div>`;

    const build = () => {
      // Clips have as many shots as there are prompts — nothing to choose.
      const n = forImages ? (Number($('#jh-shots', m).value) || shots) : shots;
      const design = forImages ? $('#jh-design', m).value : 'none';
      const first = S.session.cards[0] || {};
      const text = forImages
        ? buildImagePrompt({ shots: n, design,
            model: first.model || 'nano-banana-2',
            aspect: first.aspect_ratio || '9:16',
            resolution: first.resolution || '1K',
            format: first.output_format || 'jpg' })
        : buildVideoPrompt({ shots: n,
            model: first.video_model || 'gemini-omni-video',
            aspect: first.video_aspect || '9:16',
            resolution: first.video_resolution || '1080p',
            duration: String(first.video_duration || '8') });
      $('#jh-main', m).value = text;
      if (forImages) {
        const d = designById(design);
        $('#jh-note', m).textContent = d.note;
      } else {
        $('#jh-note', m).textContent =
          'Motion only — the image is the first frame. Duration is chosen per shot.';
      }
      return text;
    };
    build();

    if (forImages) {
      $('#jh-shots', m).oninput = build;
      $('#jh-design', m).onchange = build;
    }
    $('#jh-copy', m).onclick = () => copyText(build(), 'Prompt copied — paste it into ChatGPT');
    $('#jh-copy-json', m).onclick = () => copyText($('#jh-ex', m).value, 'Example copied');
    $('#jh-import', m).onclick = () => {
      m.close();
      if (forImages) openImport(); else openVideoImport();
    };
  }
  paint(kind);
  $('#jh-seg', m).onclick = (e) => {
    const b = e.target.closest('button[data-kind]');
    if (b) paint(b.dataset.kind);
  };
}

/* ═════════════════════════════════════════════ import */

/* Two ways in, one box. Paste a JSON array, or paste plain prompts with a blank
   line between them; the dialog says which one it is reading as you type, so
   there is no mode to get wrong. */
function importDialog({ title, sub, note, run }) {
  const m = modal(`
    <h3>${esc(title)}</h3>
    <p class="sub">${sub}</p>
    <textarea id="imp" spellcheck="false" placeholder="Paste a JSON array…

…or just paste the prompts themselves, with a blank line between each one:

A warm kitchen at dawn, a woman at the counter, medium close-up.

Extreme close-up of her hands around a white mug, steam rising."></textarea>
    <p class="note" id="imp-read">Paste JSON, or plain prompts separated by a blank line.</p>
    <p class="note">${note}</p>
    <div class="actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="imp-go">Import</button>
    </div>`, { wide: true });

  const box = $('#imp', m);
  const read = $('#imp-read', m);
  box.addEventListener('input', () => {
    const t = box.value.trim();
    if (!t) {
      read.textContent = 'Paste JSON, or plain prompts separated by a blank line.';
      return;
    }
    const looksJson = t.startsWith('[') || t.startsWith('{') || /"prompt"\s*:/.test(t);
    const blocks = t.split(/\n[ \t]*\n+/).filter((b) => b.trim()).length;
    read.innerHTML = looksJson
      ? '<b>Reading as JSON.</b>'
      : '<b>Reading as plain text</b> — ' + blocks + ' prompt' +
        (blocks === 1 ? '' : 's') + ', on your current settings.';
  });

  $('#imp-go', m).onclick = async () => {
    const text = box.value;
    if (!text.trim()) return toast('Nothing pasted', 'bad');
    try {
      const prev = captureRects();
      const r = await run(text);
      m.close();
      (r.warnings || []).forEach((w) => toast(w));
      await loadSession(S.session.id);
      playFlip(prev);
    } catch (e) { toast(e.message, 'bad'); }
  };
}

function openImport() {
  importDialog({
    title: 'Import prompts',
    sub: 'A JSON array from ChatGPT, or the prompts on their own with a blank line between them. Each one becomes a card.',
    note: 'Plain prompts use your current settings. In JSON, missing fields fall back to those too, a payload with no title becomes "Image N", and <code>callBackUrl</code> is ignored.',
    run: async (text) => {
      const r = await api('/api/session/' + S.session.id + '/import', { text });
      toast('Imported ' + r.created + ' prompt' + (r.created === 1 ? '' : 's') +
            (r.read_as === 'text' ? ' from plain text' : ''), 'good');
      return r;
    },
  });
}

function openVideoImport() {
  importDialog({
    title: 'Import clip prompts',
    sub: 'These attach to the shots you already have — nothing new is created. JSON fills in the prompt, duration, resolution and aspect; plain prompts fill the prompts in order.',
    note: 'JSON is matched by <code>seq</code> first, then by an exact title, then by position. That is why regenerating an image never moves a clip prompt onto the wrong shot. Plain text goes shot 1, shot 2, and so on.',
    run: async (text) => {
      const r = await api('/api/session/' + S.session.id + '/import_videos', { text });
      toast('Filled ' + r.filled + ' shot' + (r.filled === 1 ? '' : 's') +
            (r.read_as === 'text' ? ' from plain text' : ''), 'good');
      return r;
    },
  });
}

async function applyToAll(body) {
  await flushSaves();
  const r = await api('/api/session/' + S.session.id + '/apply_all', body);
  toast('Applied to ' + r.updated + ' prompt' + (r.updated === 1 ? '' : 's'), 'good');
  await loadSession(S.session.id);
}

async function applyVideoToAll(body) {
  await flushSaves();
  const r = await api('/api/session/' + S.session.id + '/video_defaults', body);
  toast('Applied to ' + r.updated + ' shot' + (r.updated === 1 ? '' : 's'), 'good');
  await loadSession(S.session.id);
}

/* ═════════════════════════════════════════════ folding */

function foldSet() { return S.tab === 'images' ? S.vcollapsed : S.collapsed; }
function foldSel() { return S.tab === 'images' ? '#vcards' : '#cards'; }


function foldAll() {
  const set = foldSet();
  const list = visibleCards();
  const prev = captureRects(foldSel());
  if (list.some((c) => !set.has(c.id))) list.forEach((c) => set.add(c.id));
  else list.forEach((c) => set.delete(c.id));
  refreshSession();
  playFlip(prev, foldSel());
}

function foldOne(cardId, which) {
  const set = which === 'v' ? S.vcollapsed : S.collapsed;
  const sel = which === 'v' ? '#vcards' : '#cards';
  const prev = captureRects(sel);
  if (set.has(cardId)) set.delete(cardId); else set.add(cardId);
  refreshSession();
  playFlip(prev, sel);
}

/* ═════════════════════════════════════════════ the cast
 *
 * Characters are references with no session, so they are global: upload a face
 * once and it is offered on every prompt of every video from then on. The file
 * lives in characters/ and that local copy is the durable one — kie.ai forgets
 * an upload after three days, so it is re-sent on demand, which is what lets a
 * character still work months later.
 */

function refTileHtml(r, chosen) {
  const sel = chosen && chosen.has(r.id) ? ' sel' : '';
  return `<div class="rtile${sel} m-${r.media || 'image'}" data-ref="${r.id}" title="${esc(r.name)}">
      ${mediaThumb(r)}
      ${chosen ? '<span class="pick">✓</span>' : ''}
      <button class="zoom" data-zoom="${r.id}" title="Preview">⤢</button>
      <button class="kill" data-kill="${r.id}" title="Delete permanently">✕</button>
      <span class="nm">${esc(r.name)}</span>
    </div>`;
}

function uploadingTiles() {
  return S.uploading.map((n) => `<div class="rtile busy">
      <i class="spin"></i><span class="nm">${esc(n)}</span></div>`).join('');
}

const MEDIA_MIME = { images: 'image/', videos: 'video/', audio: 'audio/' };

/* Files go up as the raw request body - no base64, so a 200MB video is fine.
   `accept` limits what is taken: "images", "videos" or "audio". */
async function uploadImages(files, { name = '', kind = 'upload', onTick, accept = 'images' } = {}) {
  const made = [];
  let i = 0;
  for (const file of files) {
    if (!file.type.startsWith(MEDIA_MIME[accept] || 'image/')) {
      toast(file.name + ' is not ' + (accept === 'audio' ? 'a sound file' : 'a' + (accept === 'videos' ? ' video' : 'n image')), 'bad');
      continue;
    }
    i += 1;
    // Several shots of one character share the name, numbered.
    const label = name ? (i === 1 ? name : name + ' ' + i) : '';
    S.uploading.push(label || file.name);
    if (onTick) onTick();
    try {
      const q = new URLSearchParams({ filename: file.name, name: label, kind });
      if (kind !== 'character') q.set('session', S.session.id);
      const res = await fetch('/api/upload?' + q, { method: 'POST', body: file });
      const r = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(r.error || res.statusText);
      if (kind !== 'character') S.session.refs = r.refs;
      else if (S.session) S.session.refs = (await api('/api/session/' + S.session.id)).refs;
      made.push(r.ref_id);
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      S.uploading.splice(S.uploading.indexOf(label || file.name), 1);
      if (onTick) onTick();
    }
  }
  return made;
}

function dropZone(el, take) {
  el.onclick = () => el.parentNode.querySelector('input[type=file]').click();
  ['dragenter', 'dragover'].forEach((ev) => el.addEventListener(ev, (e) => {
    e.preventDefault(); el.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => el.addEventListener(ev, (e) => {
    e.preventDefault(); el.classList.remove('over');
  }));
  el.addEventListener('drop', (e) => take([...(e.dataTransfer?.files || [])]));
}

function openAddCharacter() {
  const m = modal(`
    <h3>Add a character</h3>
    <p class="sub">Characters are kept across every session — upload a face once and it is there on every prompt you write from now on. The files live in your <code>characters</code> folder and are re-sent to kie.ai whenever a link expires, so they keep working for months.</p>
    <label>Name</label>
    <input type="text" id="ch-name" placeholder="e.g. Marta, the narrator" autocomplete="off">
    <div class="drop" id="ch-drop">
      <span class="big">🧑‍🎤</span>
      Drop images here, or <b>browse your PC</b>
      <small>One character at a time — add several shots of them at once if you have them</small>
    </div>
    <input type="file" id="ch-file" accept="image/*" multiple hidden>
    <div class="libhead"><h4>Your cast</h4><span class="cnt" id="ch-count"></span></div>
    <div class="refgrid" id="ch-grid"></div>
    <div class="actions"><button class="btn primary" data-close>Done</button></div>`);

  function paint() {
    const cast = S.session.refs.filter((r) => r.kind === 'character');
    $('#ch-count', m).textContent = cast.length
      ? cast.length + ' character' + (cast.length === 1 ? '' : 's') : '';
    $('#ch-grid', m).innerHTML = uploadingTiles() + (cast.length
      ? cast.map((r) => refTileHtml(r, null)).join('')
      : (S.uploading.length ? ''
         : '<div class="empty" style="grid-column:1/-1;padding:20px">Nobody yet.</div>'));
  }
  paint();

  $('#ch-grid', m).addEventListener('click', async (e) => {
    const zoom = e.target.closest('[data-zoom]');
    if (zoom) return openRefPreview(Number(zoom.dataset.zoom));
    const kill = e.target.closest('[data-kill]');
    if (!kill) return openRefPreview(Number(e.target.closest('.rtile').dataset.ref));
    const ok = await confirmModal({
      title: 'Remove this character?',
      body: 'It leaves every session. Prompts already using it lose the reference. The file in your characters folder is kept.',
      ok: 'Remove',
    });
    if (!ok) return;
    await api('/api/ref/' + Number(kill.dataset.kill) + '/delete', {});
    await loadSession(S.session.id, { quiet: true });
    paint();
  });

  const take = async (files) => {
    const name = $('#ch-name', m).value.trim();
    const made = await uploadImages(files, { name, kind: 'character', onTick: paint });
    if (made.length) {
      toast('Added ' + made.length + ' image' + (made.length === 1 ? '' : 's') +
            (name ? ' for ' + name : ''), 'good');
      $('#ch-name', m).value = '';
    }
    paint();
  };
  dropZone($('#ch-drop', m), take);
  $('#ch-file', m).onchange = async (e) => { await take([...e.target.files]); e.target.value = ''; };
}

/* ═════════════════════════════════════════════ reference picker */

function openRefPicker(cardId, kind, media = 'images') {
  const card = S.session.cards.find((c) => c.id === cardId);
  // "base" picks exactly one picture to BE the shot; "video" picks a clip's
  // references of one media kind, up to what its model takes.
  const single = kind === 'base';
  const isClip = kind === 'video';
  const one = MEDIA_ONE[media];
  const ofMedia = (r) => (r.media || 'image') === one;
  const current = single ? (card.base_image ? [card.base_image] : [])
                : isClip ? (card.vref_media[media] || []) : card.refs;
  const chosen = new Set(current.map((r) => r.id));
  const pre = isClip && media === 'images' && card.first_as_ref && card.first ? 1 : 0;
  const cap = single ? 1 : isClip ? Math.max(0, (clipModeCaps(card)[media] || 8) - pre) : 8;

  const m = modal(`
    <h3>${single ? 'Attach an image to this shot'
        : isClip ? 'Reference ' + MEDIA_LABEL[media].toLowerCase() + ' for the clip' : 'Reference images'}</h3>
    <p class="sub">${single
      ? 'This becomes the frame the clip is animated from, for <b>' +
        esc(card.title || 'this shot') + '</b> — no need to generate it first. ' +
        'A generated take will take over if you make one later.'
      : 'Up to ' + cap + ' for <b>' + esc(card.title || 'this prompt') + '</b>.' + (pre
        ? ' This shot\'s own image is already sent as the first one.' : '')}</p>

    <div class="drop" id="drop">
      <span class="big">Add ${one === 'audio' ? 'a sound' : 'a' + (one === 'video' ? ' video' : 'n image')}</span>
      Drop ${one === 'image' ? 'one' : 'it'} here, or <b>browse your computer</b>
      <small>${one === 'video' ? 'MP4, MOV or WebM · up to 300MB' : one === 'audio' ? 'MP3, WAV or M4A · up to 30MB' : 'PNG, JPG or WebP · up to 30MB'}</small>
    </div>
    <input type="file" id="ref-file" accept="${one}/*" multiple hidden>

    <div class="or">or paste a link</div>
    <div class="urlrow">
      <input type="text" id="ref-url" placeholder="https://…/${one === 'video' ? 'clip.mp4' : one === 'audio' ? 'sound.mp3' : 'picture.jpg'}" autocomplete="off">
      <button class="btn" id="ref-url-go">Add</button>
    </div>

    <div class="libhead"><h4>Available</h4><span class="cnt" id="ref-count"></span></div>
    <div class="refgrid" id="refgrid"></div>

    <div class="actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="ref-save">Save</button>
    </div>`);

  const save = $('#ref-save', m);
  function paint() {
    const refs = S.session.refs.filter(ofMedia);
    $('#ref-count', m).textContent = single
      ? (chosen.size ? 'one chosen' : 'pick one')
      : chosen.size + ' of ' + cap + ' selected';
    save.textContent = single ? (chosen.size ? 'Use this image' : 'Save')
                              : (chosen.size ? 'Use ' + chosen.size : 'Save');
    const grid = $('#refgrid', m);
    // The cast comes first and is labelled, so a face uploaded weeks ago is not
    // buried among this session's one-off references.
    const cast = refs.filter((r) => r.kind === 'character');
    const rest = refs.filter((r) => r.kind !== 'character');
    const group = (label, list) => list.length
      ? `<div class="grouphead">${label}</div>` + list.map((r) => refTileHtml(r, chosen)).join('')
      : '';
    // A clip can use any shot's finished image as a reference, too.
    const shotImgs = isClip && one === 'image'
      ? S.session.cards.filter((c) => c.active && c.active.file).map((c) => `
          <div class="rtile shotpick" data-shotgen="${c.active.id}" title="Use ${esc(c.title || 'shot ' + c.seq)}'s image">
            <img src="${fileUrl(c.active.file)}" alt="" loading="lazy"><span class="nm">${c.seq}. ${esc(c.title || '')}</span></div>`).join('')
      : '';
    const body = uploadingTiles() + group('Cast', cast) + group('This session', rest)
      + (shotImgs ? '<div class="grouphead">Shot images</div>' + shotImgs : '');
    grid.innerHTML = body ||
      '<div class="empty" style="grid-column:1/-1;padding:20px">Nothing here yet — add one above.</div>';
  }
  paint();

  $('#refgrid', m).addEventListener('click', async (e) => {
    const zoom = e.target.closest('[data-zoom]');
    if (zoom) { e.stopPropagation(); return openRefPreview(Number(zoom.dataset.zoom)); }
    const kill = e.target.closest('[data-kill]');
    if (kill) {
      e.stopPropagation();
      const id = Number(kill.dataset.kill);
      const ref = S.session.refs.find((r) => r.id === id);
      const ok = await confirmModal({
        title: 'Delete "' + (ref?.name || 'this reference') + '"?',
        body: ref?.kind === 'character'
          ? 'It leaves every session. Prompts already using it lose the reference. The file on disk is kept.'
          : 'It leaves this session. Prompts already using it lose the reference. The file on disk is kept.',
      });
      if (!ok) return;
      await api('/api/ref/' + id + '/delete', {});
      chosen.delete(id);
      await loadSession(S.session.id, { quiet: true });
      paint();
      return;
    }
    const shot = e.target.closest('[data-shotgen]');
    if (shot) {
      if (chosen.size >= cap) return toast(cap + ' is this model\'s limit', 'bad');
      const r = await api('/api/generation/' + shot.dataset.shotgen + '/use_as_ref', {});
      S.session.refs = r.refs;
      chosen.add(r.ref_id);
      return paint();
    }
    const tile = e.target.closest('.rtile');
    if (!tile || tile.classList.contains('busy')) return;
    const id = Number(tile.dataset.ref);
    if (chosen.has(id)) chosen.delete(id);
    else if (single) { chosen.clear(); chosen.add(id); }   // exactly one
    else if (chosen.size >= cap) return toast(cap + ' is this model\'s limit', 'bad');
    else chosen.add(id);
    paint();
  });

  const take = async (files) => {
    const made = await uploadImages(files, { onTick: paint, accept: media });
    if (single && made.length) { chosen.clear(); chosen.add(made[made.length - 1]); }
    else made.forEach((id) => { if (chosen.size < cap) chosen.add(id); });
    paint();
  };
  dropZone($('#drop', m), take);
  $('#ref-file', m).onchange = async (e) => { await take([...e.target.files]); e.target.value = ''; };

  const addUrl = async () => {
    const input = $('#ref-url', m);
    const url = input.value.trim();
    if (!url) return;
    try {
      const r = await api('/api/session/' + S.session.id + '/ref_url', { url, media: one });
      S.session.refs = r.refs;
      if (single) { chosen.clear(); chosen.add(r.ref_id); }
      else if (chosen.size < cap) chosen.add(r.ref_id);
      input.value = '';
      paint();
    } catch (e) { toast(e.message, 'bad'); }
  };
  $('#ref-url-go', m).onclick = addUrl;
  $('#ref-url', m).onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); addUrl(); } };

  save.onclick = async () => {
    if (single) {
      await api('/api/card/' + cardId + '/base_image', { ref_id: [...chosen][0] || null });
    } else if (isClip) {
      // Keep the other kinds as they are; replace only this kind, in order.
      const others = (card.video_refs || []).filter((r) => !ofMedia(r)).map((r) => r.id);
      await api('/api/card/' + cardId + '/video_refs', { ref_ids: others.concat([...chosen]) });
    } else {
      await api('/api/card/' + cardId + '/refs', { ref_ids: [...chosen] });
    }
    m.close();
    await loadSession(S.session.id);
  };
}

/* ═════════════════════════════════════════════ frame sources */

/* Grab the last frame of a finished clip in the browser (seek to the end, draw
   to a canvas) and keep it as a picture in the library, so the next clip can
   start exactly where this one ended - with any model, no extra tools. */
async function lastFrameRef(card, gen) {
  const v = document.createElement('video');
  v.muted = true; v.preload = 'auto'; v.src = fileUrl(gen.file);
  await new Promise((ok, bad) => { v.onloadedmetadata = ok; v.onerror = () => bad(new Error('could not read that clip')); });
  v.currentTime = Math.max(0, v.duration - 0.04);
  await new Promise((ok) => { v.onseeked = ok; });
  const cv = document.createElement('canvas');
  cv.width = v.videoWidth; cv.height = v.videoHeight;
  cv.getContext('2d').drawImage(v, 0, 0);
  const blob = await new Promise((ok) => cv.toBlob(ok, 'image/png'));
  const name = 'Last frame of ' + card.seq + '. ' + (card.title || 'clip') + ' (take ' + gen.n + ')';
  const q = new URLSearchParams({ session: S.session.id, filename: 'last-frame-' + card.seq + '-v' + gen.n + '.png',
                                  name, kind: 'frame' });
  const res = await fetch('/api/upload?' + q, { method: 'POST', body: blob });
  const r = await res.json();
  if (!res.ok) throw new Error(r.error || 'upload failed');
  S.session.refs = r.refs;
  return r.ref_id;
}

function openFramePicker(cardId, which) {
  const card = S.session.cards.find((c) => c.id === cardId);
  const name = which === 'first' ? 'first frame' : 'last frame';
  const cur = card[which] ? card[which].src : null;
  const shots = S.session.cards.filter((c) => isShot(c) && c.active && c.active.file && c.id !== cardId);
  const endings = S.session.cards.filter((c) => (c.video_generations || []).some((g) => g.kie_state === 'success' && g.file));
  const images = S.session.refs.filter((r) => (r.media || 'image') === 'image');
  const tile = (attrs, thumb, label, on) => `<button class="ftile${on ? ' on' : ''}" ${attrs}>${thumb}<span class="nm">${label}</span></button>`;
  const m = modal(`
    <h3>${which === 'first' ? 'First' : 'Last'} frame</h3>
    <p class="sub">For <b>${esc(card.title || 'this clip')}</b>. ${which === 'first'
      ? 'The clip starts exactly on this picture.' : 'The clip ends exactly on this picture.'}</p>
    <div class="fquick">
      ${isShot(card) && which === 'first' ? `<button class="btn${cur === 'self' ? ' primary' : ''}" data-src="self">This shot's own image</button>` : ''}
      <button class="btn" data-src="none">No ${name}</button>
    </div>
    ${shots.length ? `<div class="libhead"><h4>Another shot's image</h4><span class="cnt">follows that shot if you regenerate it</span></div>
      <div class="ftiles">${shots.map((c) => tile(`data-src="card:${c.id}"`, `<img src="${fileUrl(c.active.file)}" alt="" loading="lazy">`,
        c.seq + '. ' + esc(c.title || ''), cur === 'card:' + c.id)).join('')}</div>` : ''}
    ${endings.length ? `<div class="libhead"><h4>Where a clip ends</h4><span class="cnt">its last frame, so this clip carries on from it</span></div>
      <div class="ftiles">${endings.map((c) => {
        const g = c.video_generations.find((x) => x.kie_state === 'success' && x.file);
        return tile(`data-lastof="${c.id}" data-gen="${g.id}"`, `<video src="${fileUrl(g.file)}#t=999" muted preload="metadata"></video>`,
          'End of ' + c.seq + '. ' + esc(c.title || ''), false);
      }).join('')}</div>` : ''}
    <div class="libhead"><h4>From your library</h4></div>
    <div class="drop" id="fdrop"><span class="big">Add an image</span>Drop one here, or <b>browse your computer</b></div>
    <input type="file" id="ffile" accept="image/*" hidden>
    <div class="ftiles" id="flib">${images.map((r) => tile(`data-src="ref:${r.id}"`, `<img src="${refSrc(r)}" alt="" loading="lazy">`,
      esc(r.name), cur === 'ref:' + r.id)).join('')}</div>
    <div class="actions"><button class="btn" data-close>Cancel</button></div>`, { wide: true });

  const set = async (src) => {
    await flushSaves();
    await api('/api/card/' + cardId, { [which + '_src']: src });
    m.close();
    await loadSession(S.session.id, { quiet: true });
  };
  m.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-src]');
    if (b) return set(b.dataset.src);
    const end = e.target.closest('[data-lastof]');
    if (end) {
      end.classList.add('busy');
      try {
        const c = S.session.cards.find((x) => x.id === Number(end.dataset.lastof));
        const g = c.video_generations.find((x) => x.id === Number(end.dataset.gen));
        set('ref:' + await lastFrameRef(c, g));
      } catch (err) { end.classList.remove('busy'); toast(err.message, 'bad'); }
    }
  });
  const take = async (files) => {
    const made = await uploadImages(files.slice(0, 1), { accept: 'images' });
    if (made.length) set('ref:' + made[0]);
  };
  dropZone($('#fdrop', m), take);
  $('#ffile', m).onchange = (e) => take([...e.target.files]);
}

function insertToken(cardId, token) {
  const ta = $('.vcard[data-card="' + cardId + '"] .video-in');
  if (!ta || !token) return;
  const at = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, at), after = ta.value.slice(ta.selectionEnd ?? at);
  const pad = before && !/\s$/.test(before) ? ' ' : '';
  ta.value = before + pad + token + ' ' + after;
  ta.focus();
  const pos = (before + pad + token + ' ').length;
  ta.setSelectionRange(pos, pos);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

/* ═════════════════════════════════════════════ take lightbox */

function findTake(genId) {
  for (const c of S.session.cards) {
    let g = c.generations.find((x) => x.id === genId);
    if (g) return { card: c, gen: g, kind: 'image' };
    g = c.video_generations.find((x) => x.id === genId);
    if (g) return { card: c, gen: g, kind: 'video' };
  }
  return null;
}

function downloadName(card, gen, kind) {
  const stem = String(card.title || 'image').replace(/[^a-zA-Z0-9-_ ]/g, '-').trim();
  const ext = (gen.file || '').split('.').pop() || (kind === 'video' ? 'mp4' : 'jpg');
  return String(card.seq).padStart(2, '0') + '_' + (stem || 'image') + '_v' + gen.n + '.' + ext;
}

function openLightbox(genId) {
  const found = findTake(genId);
  if (!found) return;
  const { card, gen, kind } = found;

  let sent = {};
  try { sent = JSON.parse(gen.params || '{}'); } catch { /* ignore */ }
  const rows = Object.entries(sent)
    .filter(([k]) => k !== 'prompt')
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(
      Array.isArray(v) ? (v.length ? v.length + ' image(s)' : 'none') : v)}</td></tr>`).join('');

  const media = gen.kie_state === 'success' && gen.file
    ? (kind === 'video'
        ? `<video src="${fileUrl(gen.file)}" controls autoplay loop></video>`
        : `<img src="${fileUrl(gen.file)}" alt="">`)
    : `<div style="color:var(--dim);padding:60px">${gen.kie_state === 'fail'
        ? esc(gen.fail_msg || 'failed') : 'still working…'}</div>`;

  const m = modal(`
    <div class="light">
      <div class="big">${media}</div>
      <div class="side">
        <h4>${card.seq}. ${esc(card.title || 'Image ' + card.seq)}</h4>
        <div class="meta">${kind} · take ${gen.n} · ${esc(provLabel(gen.provider))} · ${esc(gen.model || '')} ·
          ${esc(gen.kie_state)}${gen.credits ? ' · ' + gen.credits + ' credits' : ''}</div>
        <pre>${esc(gen.prompt || '')}</pre>
        <table class="sentbox"><caption>exactly what was sent</caption>${rows}</table>
        <div class="actions">
          <button class="btn" data-close>Close</button>
          <span class="sp"></span>
          <button class="btn danger" id="lb-del">Delete take</button>
          ${gen.file ? `<a class="btn primary" href="${fileUrl(gen.file)}"
             download="${esc(downloadName(card, gen, kind))}">Download</a>` : ''}
        </div>
      </div>
    </div>`, { wide: true });

  $('#lb-del', m).onclick = async () => {
    const ok = await confirmModal({
      title: 'Delete take ' + gen.n + '?',
      body: 'It leaves the app only — nothing is deleted at kie.ai and the file on disk is kept.',
      ok: 'Delete take',
    });
    if (!ok) return;
    await api('/api/generation/' + gen.id + '/delete', {});
    m.close();
    await loadSession(S.session.id);
  };
}

/* ═════════════════════════════════════════════ generating */

async function generate(cardId, kind) {
  const key = (kind === 'video' ? 'v' : '') + cardId;
  if (S.firing.has(key)) return;              // the click already went out
  S.firing.add(key);
  refreshSession();
  try {
    // Never spend before every pending edit has landed, or the model is asked
    // for the settings that were on screen a moment ago.
    await flushSaves();
    const path = kind === 'video' ? '/generate_video' : '/generate';
    await api('/api/card/' + cardId + path, {});
    await loadSession(S.session.id);
  } catch (e) {
    if (e.status === 428) { toast(e.message, 'bad'); openSettings(); }
    else if (e.status === 409) toast(e.message);
    else toast(e.message, 'bad');
    await loadSession(S.session.id, { quiet: true });
  } finally {
    S.firing.delete(key);
    refreshSession();
  }
}

async function generateAll(kind) {
  await flushSaves();
  const video = kind === 'video';
  const ready = S.session.cards.filter((c) =>
    ((video ? c.video_prompt : c.prompt) || '').trim()
    && !(video ? c.video_busy : c.busy)
    && (!video || c.can_clip));
  if (!ready.length) {
    return toast(video
      ? 'Nothing to generate — clips need a video prompt and a finished image'
      : 'Nothing to generate', 'bad');
  }
  const stale = ready.filter((c) => video ? (!c.video_latest || c.video_dirty)
                                          : (!c.latest || c.dirty)).length;
  const noun = video ? 'clip' : 'image';

  const m = modal(`
    <h3>Generate all ${noun}s</h3>
    <p class="sub">${ready.length} ${noun}${ready.length === 1 ? '' : 's'} ready.
      ${stale} ${stale === 1 ? 'is' : 'are'} new or edited since the last take.
      This spends credits.</p>
    <div class="actions">
      <button class="btn" data-close>Cancel</button>
      ${stale && stale < ready.length
        ? `<button class="btn" id="ga-new">Only what changed (${stale})</button>` : ''}
      <button class="btn primary" id="ga-all">Generate all ${ready.length}</button>
    </div>`);

  let sent = false;
  const fire = async (onlyNew, btn) => {
    if (sent) return;                     // a double click on the confirm itself
    sent = true;
    $$('.actions button', m).forEach((b) => { b.disabled = true; });
    btn.textContent = 'Sending…';
    try {
      const path = video ? '/generate_all_videos' : '/generate_all';
      const r = await api('/api/session/' + S.session.id + path, { only_new: onlyNew });
      m.close();
      toast('Queued ' + r.queued + ' ' + noun + (r.queued === 1 ? '' : 's'), 'good');
      (r.results || []).filter((x) => !x.ok && !x.skipped).slice(0, 3)
        .forEach((f) => toast(f.error, 'bad'));
      await loadSession(S.session.id);
    } catch (e) {
      m.close();
      if (e.status === 428) { toast(e.message, 'bad'); openSettings(); }
      else toast(e.message, 'bad');
    }
  };
  $('#ga-all', m).onclick = (e) => fire(false, e.currentTarget);
  const only = $('#ga-new', m);
  if (only) only.onclick = (e) => fire(true, e.currentTarget);
}

function downloadAll(kind) {
  const n = S.session.cards.reduce((t, c) =>
    t + (kind === 'video' ? c.video_count : c.success_count), 0);
  if (!n) return toast('No finished ' + (kind === 'video' ? 'clips' : 'images') + ' yet', 'bad');
  location.href = '/download?s=' + S.session.id + '&kind=' + (kind === 'video' ? 'video' : 'image');
}

/* ═════════════════════════════════════════════ sessions */

/* A model id the catalogue does not list: pick the service, type the id. */
function customModel(kind, onPick, onBack) {
  const list = usableFor(kind).length ? usableFor(kind) : providersFor(kind);
  let done = false;
  const m = modal(`
    <h3>Use a model id</h3>
    <p class="sub">For a ${kind === 'video' ? 'clip' : 'image'} model that is not in the list. It is sent in the
      same shape as that service's usual models.</p>
    <div class="row">
      <div><label>Service</label><select id="cm-p">${list.map(([id, p]) =>
        `<option value="${id}">${esc(p.label)}</option>`).join('')}</select></div>
      <div><label>Model id</label><input type="text" id="cm-m" placeholder="exactly as the service names it"></div>
    </div>
    <div class="actions">
      <button class="btn" data-close>Back</button>
      <button class="btn primary" id="cm-go">Use this model</button>
    </div>`, { onClose: () => { if (!done && onBack) onBack(); } });
  setTimeout(() => $('#cm-m', m).focus(), 50);
  const go = () => {
    const model = $('#cm-m', m).value.trim();
    if (!model) return toast('Type the model id', 'bad');
    done = true;
    m.close();
    onPick({ provider: $('#cm-p', m).value, model });
  };
  $('#cm-go', m).onclick = go;
  $('#cm-m', m).onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } };
}

/* New session: the name first, then the image model, then the clip model -
   each its own window, and Esc steps back one. */
function newSession(state = {}) {
  const m = modal(`
    <h3>New session</h3>
    <p class="sub">Name it after the job. Next you pick the image model, then the clip model.</p>
    <label>Name</label>
    <input type="text" id="ns-name" placeholder="e.g. Throat clearing ad v3" value="${esc(state.name || '')}">
    <div class="actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="ns-go">Next: image model</button>
    </div>`);
  const next = () => {
    state.name = $('#ns-name', m).value.trim();
    m.close();
    pickImage();
  };
  $('#ns-go', m).onclick = next;
  $('#ns-name', m).onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); next(); } };

  const named = () => (state.name ? '“' + state.name + '”' : 'this session');
  const pickImage = () => openModelMenu(null, {
    kind: 'image', step: 'Step 2 of 3', title: 'Images for ' + named(),
    current: state.image || startingEngine('image'),
    onPick: (v) => { if (v) state.image = v; pickVideo(); },
    onCancel: () => newSession(state),
  });
  const pickVideo = () => openModelMenu(null, {
    kind: 'video', step: 'Step 3 of 3', title: 'Clips for ' + named(),
    current: state.video || startingEngine('video'), skip: 'Decide later',
    onPick: (v) => { state.video = v || startingEngine('video'); create(); },
    onCancel: pickImage,
  });
  const create = async () => {
    try {
      const img = state.image || startingEngine('image');
      const r = await api('/api/sessions', {
        name: state.name || '',
        image_provider: img.provider, image_model: img.model,
        video_provider: state.video.provider, video_model: state.video.model,
      });
      await openSession(r.session_id);
      toast('Session created', 'good');
    } catch (e) { toast(e.message, 'bad'); }
  };
}

/* The Images / Clips chip: straight into the picker, and the choice applies to
   every shot in the session. */
function openEngineDialog(kind, anchor) {
  const cur = kind === 'video' ? S.session.video_engine : S.session.image_engine;
  const own = S.session.cards.filter((c) => (kind === 'video' ? c.video_engine : c.engine).own).length;
  const noun = kind === 'video' ? 'clips' : 'images';
  openModelMenu(anchor, {
    kind, current: cur,
    step: kind === 'video' ? 'Clip engine' : 'Image engine',
    title: 'For every shot in “' + S.session.name + '”'
      + (own ? ' — replaces ' + own + ' shot' + (own === 1 ? "'s own choice" : "s' own choices") : ''),
    onPick: async (e) => {
      if (!e) return;
      try {
        await flushSaves();
        await api('/api/session/' + S.session.id + '/engine', { kind, provider: e.provider, model: e.model });
        await loadSession(S.session.id);
        toast(specOf(e.provider, kind, e.model).label + ' now makes the ' + noun, 'good');
      } catch (err) { toast(err.message, 'bad'); }
    },
  });
}

async function openSession(id) {
  S.cardFilter = '';
  $$('[data-filter]').forEach((i) => { i.value = ''; });
  await loadSession(id);
  show('session');
  location.hash = '#/s/' + id;
}

async function goHome() {
  S.session = null;
  document.title = 'Cherry';
  await loadHome();
  show('home');
  location.hash = '';
}

function sessionMenu(id) {
  const s = S.sessions.find((x) => x.id === id);
  const m = modal(`
    <h3>${esc(s.name)}</h3>
    <p class="sub">${s.card_count} prompts · ${s.image_count} images · updated ${esc(s.updated_at || '')}</p>
    <label>Rename</label>
    <input type="text" id="sm-name" value="${esc(s.name)}">
    <div class="actions">
      <button class="btn danger" id="sm-del">Delete session</button>
      <span class="sp"></span>
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="sm-save">Save</button>
    </div>`);
  $('#sm-save', m).onclick = async () => {
    await api('/api/session/' + id + '/rename', { name: $('#sm-name', m).value });
    m.close(); await loadHome();
  };
  $('#sm-del', m).onclick = async () => {
    const ok = await confirmModal({
      title: 'Delete "' + s.name + '"?',
      body: 'All ' + s.card_count + ' prompt' + (s.card_count === 1 ? '' : 's') +
            ' and their takes leave the app. Your characters and the files on disk are kept.',
      ok: 'Delete session',
    });
    if (!ok) return;
    await api('/api/session/' + id + '/delete', {});
    m.close(); await loadHome();
  };
}

/* ═════════════════════════════════════════════ wiring */

document.addEventListener('click', async (e) => {
  const closer = e.target.closest('[data-close]');
  if (closer) return closer.closest('.scrim').close();
  if (e.target.closest('[data-stop]')) return e.stopPropagation();

  const el = e.target.closest('[data-act]');
  if (el) {
    const act = el.dataset.act;
    const cardId = Number(el.dataset.card);
    try {
      switch (act) {
        case 'theme': {
          const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
          document.documentElement.dataset.theme = next;
          try { localStorage.setItem('cherry-theme', next); } catch { /* private mode */ }
          return;
        }
        case 'settings':      return openSettings();
        case 'check-update':  return openUpdates();
        case 'engine':        return openEngineDialog(el.dataset.kind, el);
        case 'pick-engine': {
          const kind = el.dataset.kind;
          const c = S.session.cards.find((x) => x.id === cardId);
          const own = kind === 'video' ? c.video_engine : c.engine;
          return openModelMenu(el, {
            kind,
            current: own.own ? own : null,
            session: kind === 'video' ? S.session.video_engine : S.session.image_engine,
            onPick: async (v) => {
              try {
                await flushSaves();
                await api('/api/card/' + cardId, kind === 'video'
                  ? { video_provider: v && v.provider, video_model: v && v.model }
                  : { provider: v && v.provider, model: v && v.model });
                if (v && !(PROV()[v.provider] || {}).has_key) {
                  toast('No ' + provLabel(v.provider) + ' key yet \u2014 add one in Services before generating', 'bad');
                }
                await loadSession(S.session.id, { quiet: true });
              } catch (err) { toast(err.message, 'bad'); }
            },
          });
        }
        case 'save-keys': {
          const saved = await saveKeys(el.closest('.onboard') || document);
          if (!saved) return toast('Paste a key first', 'bad');
          toast('Key saved', 'good');
          return loadHome();
        }
        case 'remove-key': {
          const id = el.dataset.provider;
          const ok = await confirmModal({
            title: 'Remove the ' + provLabel(id) + ' key?',
            body: 'Shots set to ' + esc(provLabel(id)) + ' will not generate until you add one again.',
            ok: 'Remove key',
          });
          if (!ok) return;
          S.providers = (await api('/api/settings', { clear: [id] })).providers;
          const scrim = el.closest('.scrim');
          if (scrim) scrim.close();
          await loadHome();
          return openSettings();
        }
        case 'new-session':   return newSession();
        case 'home':          return goHome();
        case 'session-menu':  e.stopPropagation(); return sessionMenu(Number(el.dataset.session));
        case 'import':        return openImport();
        case 'import-videos': return openVideoImport();
        case 'json-help':     return openJsonHelp();
        case 'add-character': return openAddCharacter();
        case 'generate-all':  return generateAll('image');
        case 'generate-all-videos': return generateAll('video');
        case 'download-all':  return downloadAll('image');
        case 'download-videos': return downloadAll('video');
        case 'gen':           return generate(cardId, 'image');
        case 'gen-video':     return generate(cardId, 'video');
        case 'pick-refs':     e.stopPropagation(); return openRefPicker(cardId, 'image');
        case 'pick-vrefs':    e.stopPropagation(); return openRefPicker(cardId, 'video', 'images');
        case 'pick-media':    e.stopPropagation(); return openRefPicker(cardId, 'video', el.dataset.media);
        case 'pick-frame':    e.stopPropagation(); return openFramePicker(cardId, el.dataset.which);
        case 'clear-frame':
          e.stopPropagation();
          await flushSaves();
          await api('/api/card/' + cardId, { [el.dataset.which + '_src']: 'none' });
          return loadSession(S.session.id, { quiet: true });
        case 'clip-mode':
          await flushSaves();
          await api('/api/card/' + cardId, { video_mode: el.dataset.mode });
          return loadSession(S.session.id, { quiet: true });
        case 'insert-token':  e.stopPropagation(); return insertToken(cardId, el.dataset.token);
        case 'add-clip':
          await restructure(() => api('/api/session/' + S.session.id + '/clips', {}), '#vcards');
          return;
        case 'insert-clip':
          return restructure(() => api('/api/session/' + S.session.id + '/clips', { after: cardId }), '#vcards');
        case 'pick-base':     e.stopPropagation(); return openRefPicker(cardId, 'base');
        case 'clear-base':
          e.stopPropagation();
          await api('/api/card/' + cardId + '/base_image', { ref_id: null });
          return loadSession(S.session.id);
        case 'fold-all':      case 'fold-all-v': return foldAll();
        case 'fold':          return foldOne(cardId, '');
        case 'fold-v':        return foldOne(cardId, 'v');
        case 'preview-ref':   e.stopPropagation(); return openRefPreview(Number(el.dataset.ref));
        case 'preview-take':  return openLightbox(Number(el.dataset.gen));

        case 'versions': {
          const prev = captureRects('#vcards');
          if (S.openVersions.has(cardId)) S.openVersions.delete(cardId);
          else S.openVersions.add(cardId);
          refreshSession();
          return playFlip(prev, '#vcards');
        }
        case 'activate':
          await api('/api/generation/' + Number(el.dataset.gen) + '/activate', {});
          return loadSession(S.session.id);
        case 'del-take': {
          e.stopPropagation();
          const ok = await confirmModal({
            title: 'Remove this version?',
            body: 'It leaves the app only — nothing is deleted at kie.ai and the file on disk is kept.',
            ok: 'Remove',
          });
          if (!ok) return;
          await api('/api/generation/' + Number(el.dataset.gen) + '/delete', {});
          return loadSession(S.session.id);
        }

        case 'add-card':
          await restructure(() => api('/api/session/' + S.session.id + '/cards', {}));
          requestAnimationFrame(() => {
            const last = $$('#cards .card .prompt-in').pop();
            if (last) { last.focus(); last.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
          });
          return;
        case 'dup':
          return restructure(() => api('/api/card/' + cardId + '/duplicate', {}), foldSel());
        case 'del-card': {
          const c = S.session.cards.find((x) => x.id === cardId);
          const takes = c ? c.generations.length + c.video_generations.length : 0;
          const onClips = S.tab === 'images';
          const both = onClips && c && isShot(c);
          const ok = await confirmModal({
            title: 'Delete ' + (c?.title || 'this prompt') + '?',
            body: (both ? 'This is a shot: its image prompt on the Prompts tab goes too. ' : '')
              + (takes
                ? 'Its ' + takes + ' take' + (takes === 1 ? '' : 's') +
                  ' leave the app too. The files on disk are kept.'
                : 'It has not generated anything yet.'),
          });
          if (!ok) return;
          await fadeOutCard(cardId);
          return restructure(() => api('/api/card/' + cardId + '/delete', {}), foldSel());
        }
        case 'up': case 'down':
          return restructure(() => api('/api/card/' + cardId + '/move',
            { direction: act, scope: el.dataset.scope || 'all' }), el.dataset.scope === 'shots' ? '#cards' : '#vcards');

        case 'unref': case 'unvref': {
          e.stopPropagation();
          const c = S.session.cards.find((x) => x.id === cardId);
          const list = act === 'unvref' ? c.video_refs : c.refs;
          const keep = list.map((r) => r.id).filter((r) => r !== Number(el.dataset.ref));
          const path = act === 'unvref' ? '/video_refs' : '/refs';
          await api('/api/card/' + cardId + path, { ref_ids: keep });
          return loadSession(S.session.id);
        }
      }
    } catch (err) { return toast(err.message, 'bad'); }
  }

  const scard = e.target.closest('.scard[data-session]');
  if (scard) return openSession(Number(scard.dataset.session));

  const take = e.target.closest('[data-gen]');
  if (take) return openLightbox(Number(take.dataset.gen));

  const tab = e.target.closest('.tab');
  if (tab) return setTab(tab.dataset.tab);
});

document.addEventListener('input', (e) => {
  const t = e.target;
  if (t.dataset.field && t.dataset.card) {
    if (t.tagName === 'TEXTAREA') autoGrow(t);
    return queueSave(Number(t.dataset.card), t.dataset.field, t.value);
  }
  if (t.id === 'session-filter') { S.filter = t.value; renderHome(); }
  if (t.dataset.filter !== undefined) {
    S.cardFilter = t.value;
    $$('[data-filter]').forEach((i) => { if (i !== t) i.value = t.value; });
    refreshSession();
  }
});

$('#clip-view').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-takes]');
  if (!b) return;
  S.allTakes = b.dataset.takes === 'all';
  $$('#clip-view button').forEach((x) => x.classList.toggle('on', x === b));
  renderClips();
});

document.addEventListener('change', async (e) => {
  // The session-wide settings rows: picking a value stamps every prompt at once.
  const all = e.target.dataset?.all;
  if (all && e.target.value) {
    return all.startsWith('video_')
      ? applyVideoToAll({ [all]: e.target.value })
      : applyToAll({ [all]: e.target.value });
  }
});

$('#session-name').addEventListener('blur', async (e) => {
  const name = e.target.textContent.trim();
  if (!S.session || !name || name === S.session.name) {
    if (S.session) e.target.textContent = S.session.name;
    return;
  }
  await api('/api/session/' + S.session.id + '/rename', { name });
  S.session.name = name;
  document.title = name + ' · Cherry';
  toast('Renamed', 'good');
});
$('#session-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
  if (e.key === 'Escape') { e.target.textContent = S.session.name; e.target.blur(); }
});

// Ctrl+Enter inside a card fires that card.
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    const card = e.target.closest?.('.card');
    if (card) {
      e.preventDefault();
      generate(Number(card.dataset.card), card.classList.contains('vcard') ? 'video' : 'image');
    }
  }
});

/* ═════════════════════════════════════════════ boot */

(async function boot() {
  try {
    await loadHome();
    const m = location.hash.match(/^#\/s\/(\d+)/);
    if (m && S.sessions.some((s) => s.id === Number(m[1]))) await openSession(Number(m[1]));
    else show('home');
  } catch (e) {
    toast('Could not reach the server: ' + e.message, 'bad');
    show('home');
  }
})();
