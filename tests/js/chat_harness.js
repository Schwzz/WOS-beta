// Runs overlay/web/chat.js against a stub DOM and a fake backend (no browser,
// no network) and checks the UI logic: state mapping, audio-driven levels in
// step with the playback clock, return to idle, voice toggle, sending, and
// offline handling. Exits non-zero on the first failed assertion.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeEl(id) {
  const el = {
    id,
    children: [],
    listeners: {},
    hidden: false,
    value: '',
    disabled: false,
    clientHeight: 100,
    dataset: {},
    scrollTop: 0,
    scrollHeight: 0,
    title: '',
    placeholder: '',
    className: '',
    attrs: {},
    classes: new Set(),
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    append(...c) {
      this.children.push(...c);
    },
    getAttribute(k) { return this.attrs[k]; },
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    focus() {},
    blur() {},
  };
  el.classList = {
    add: (c) => el.classes.add(c),
    remove: (c) => el.classes.delete(c),
    contains: (c) => el.classes.has(c),
    toggle: (c, force) =>
      (force === undefined ? !el.classes.has(c) : force) ? el.classes.add(c) : el.classes.delete(c),
  };
  let text = '';
  Object.defineProperty(el, 'textContent', {
    get: () => text,
    set: (v) => {
      text = v;
      if (v === '') el.children = [];
    },
  });
  return el;
}

const ids = [
  'chat-log',
  'chat-form',
  'chat-input',
  'chat-send',
  'btn-voice',
  'btn-chat',
  'chat',
  'line-c',
  'btn-pin',
  'btn-minimize',
  'notice',
];
const els = Object.fromEntries(ids.map((i) => [i, makeEl(i)]));
const body = makeEl('body');

let now = 1000;
const timers = new Map();
let timerId = 0;
let rafCb = null;
const calls = { levels: [], states: [], status: [], sim: [], posts: [] };

const Sage = {
  state: 'idle',
  getState() {
    return this.state;
  },
  setState(s) {
    this.state = s;
    calls.states.push(s);
  },
  setAudioLevel(v) {
    calls.levels.push(v);
  },
  setStatus(t) {
    calls.status.push(t);
  },
  setSimulation(on) {
    calls.sim.push(on);
  },
};

const backend = {
  online: true,
  state: {
    state: 'idle',
    character: 'Nexis',
    voice: { enabled: false, available: true },
    chat_version: 0,
    audio: null,
  },
  history: [],
  messageError: null,
};

function respond(status, data) {
  return Promise.resolve({ ok: status < 400, status, json: async () => data });
}
function fetchStub(url, opts = {}) {
  assert.strictEqual(opts.headers['X-WOS-Token'], 'tok', 'every call carries the token');
  if (!backend.online) return Promise.reject(new Error('offline'));
  if (url.startsWith('/api/state')) {
    calls.lastStateUrl = url;
    return respond(200, backend.state);
  }
  if (url === '/api/history')
    return respond(200, { character: 'Nexis', messages: backend.history });
  const payload = opts.body ? JSON.parse(opts.body) : null;
  calls.posts.push([url, payload]);
  if (url === '/api/voice') {
    backend.state.voice.enabled = payload.enabled;
    return respond(200, backend.state);
  }
  if (url === '/api/message') {
    if (backend.messageError) return respond(409, { error: backend.messageError });
    backend.history.push(
      { role: 'user', content: payload.text },
      { role: 'assistant', content: 'hi <b>there</b>' },
    );
    backend.state.chat_version += 1;
    return respond(200, { reply: 'hi' });
  }
  return respond(404, { error: 'nope' });
}

const window = { Sage, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; } };
const sandbox = {
  window,
  location: { hash: '#tok' },
  performance: { now: () => now },
  document: { body, getElementById: (id) => els[id], createElement: () => makeEl('x') },
  requestAnimationFrame: (cb) => {
    rafCb = cb;
  },
  setTimeout: (fn, ms) => {
    const id = ++timerId;
    timers.set(id, { fn, ms });
    return id;
  },
  clearTimeout: (id) => timers.delete(id),
  AbortController,
  Map,
  Date,
  fetch: fetchStub,
  Promise,
  JSON,
  Math,
  String,
  Error,
  console,
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '..', '..', 'overlay', 'web', 'chat.js'), 'utf8'),
  sandbox,
);

const flush = async () => {
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
};
async function pollOnce() {
  const t = [...timers.values()];
  timers.clear();
  t.forEach(({ fn }) => fn());
  await flush();
}
function frame() {
  rafCb(now);
}

(async () => {
  await flush();

  // 1. connects, hides preview buttons via class, turns the made-up motion off
  assert(body.classes.has('linked'));
  assert.deepStrictEqual(calls.sim, [false]);
  assert.strictEqual(Sage.state, 'idle');
  assert.strictEqual(els['chat-input'].placeholder, 'message nexis');
  assert.strictEqual(els['btn-voice'].textContent, 'voice: off');

  // 2. idle never feeds levels
  frame();
  assert.strictEqual(calls.levels.length, 0);

  // 3. thinking comes straight from the backend
  backend.state = { ...backend.state, state: 'thinking' };
  await pollOnce();
  assert.strictEqual(Sage.state, 'thinking');
  assert.strictEqual(calls.status[calls.status.length - 1], 'analysing');

  // 4. speaking: levels are stepped through with the playback clock
  backend.state = {
    ...backend.state,
    state: 'speaking',
    audio: { id: 7, elapsed: 0.5, fps: 50, duration: 0.1, levels: [0.2, 1.0, 0.5, 0.0] },
  };
  now = 2000;
  await pollOnce();
  assert.strictEqual(Sage.state, 'speaking');
  // playback began 0.5 s before "now" => t0 = 1500 (rtt is 0 here)
  const feed = (at) => {
    now = at;
    calls.levels.length = 0;
    frame();
    return calls.levels[0];
  };
  assert.strictEqual(feed(1500), 0.2); // window 0
  assert.strictEqual(feed(1521), 1.0); // window 1
  assert.strictEqual(feed(1541), 0.5); // window 2
  assert.strictEqual(feed(1561), 0.0); // window 3
  assert.strictEqual(feed(1700), 0); // past the end of the audio: silence
  assert.strictEqual(feed(1400), 0); // before the start: silence

  // 5. the levels are not requested again for the same utterance
  now = 2100;
  await pollOnce();
  assert(calls.lastStateUrl.endsWith('?have=7'), calls.lastStateUrl);
  backend.state = { ...backend.state, audio: { id: 7, elapsed: 0.6, fps: 50, duration: 0.1 } };
  await pollOnce();
  assert.strictEqual(feed(2100 - 600 + 21), 1.0); // clock re-synced, same levels kept

  // 6. playback ends: level cleared immediately, back to idle, nothing more fed
  backend.state = { ...backend.state, state: 'idle', audio: null };
  calls.levels.length = 0;
  await pollOnce();
  assert.strictEqual(Sage.state, 'idle');
  assert.deepStrictEqual(calls.levels, [0]);
  calls.levels.length = 0;
  frame();
  assert.strictEqual(calls.levels.length, 0);

  // 7. voice toggle posts the opposite of the current value and updates the label
  await els['btn-voice'].listeners.click();
  await flush();
  assert.deepStrictEqual(calls.posts.pop(), ['/api/voice', { enabled: true }]);
  assert.strictEqual(els['btn-voice'].textContent, 'voice: on');
  backend.state.voice.available = false;
  await pollOnce();
  assert.strictEqual(els['btn-voice'].textContent, 'voice: on (no key)');
  await els['btn-voice'].listeners.click();
  await flush();
  assert.deepStrictEqual(calls.posts.pop(), ['/api/voice', { enabled: false }]);

  // 8. sending a message: text goes out, history is rendered as plain text
  els['chat-input'].value = '  hello  ';
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  await flush();
  const sent = calls.posts.pop();
  assert.strictEqual(sent[0], '/api/message');
  assert.strictEqual(sent[1].text, 'hello');
  assert(sent[1].request_id);
  assert.strictEqual(els['chat-input'].value, '');
  const last = els['chat-log'].children[1];
  assert.strictEqual(last.children[1].textContent, 'hi <b>there</b>'); // not parsed as HTML
  assert.strictEqual(els['line-c'].textContent, 'hi <b>there</b>');

  // 9. a rejected message restores the text and shows the error
  backend.messageError = 'WOS is busy with a terminal command.';
  els['chat-input'].value = 'again';
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  await flush();
  assert.strictEqual(els['chat-input'].value, 'again');
  const err = els['chat-log'].children[els['chat-log'].children.length - 1];
  assert(err.className.includes('error'));
  assert.strictEqual(err.children[1].textContent, 'WOS is busy with a terminal command.');

  // 10. empty messages are ignored
  const before = calls.posts.length;
  els['chat-input'].value = '   ';
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  assert.strictEqual(calls.posts.length, before);

  // 11. expand / collapse
  els['btn-chat'].listeners.click();
  assert(body.classes.has('expanded') && els['chat'].hidden === false);
  assert.strictEqual(els['btn-chat'].textContent, 'companion');
  els['btn-chat'].listeners.click();
  assert(!body.classes.has('expanded') && els['chat'].hidden === true);

  // 12. backend goes away: idle + offline, no levels; comes back: recovers
  backend.state = {
    ...backend.state,
    state: 'speaking',
    audio: { id: 8, elapsed: 0, fps: 50, duration: 1, levels: [1, 1, 1] },
  };
  await pollOnce();
  assert.strictEqual(Sage.state, 'speaking');
  backend.online = false;
  await pollOnce();
  assert.strictEqual(Sage.state, 'idle');
  assert(body.classes.has('offline'));
  assert.strictEqual(calls.status[calls.status.length - 1], 'backend offline');
  calls.levels.length = 0;
  frame();
  assert.strictEqual(calls.levels.length, 0);
  backend.online = true;
  backend.state = { ...backend.state, state: 'idle', audio: null };
  await pollOnce();
  assert(!body.classes.has('offline'));
  assert.strictEqual(calls.status[calls.status.length - 1], 'standing by');

  // Native failures stay separate from HTTP health, and a hanging bridge
  // cannot prevent the next state poll or leave the pin permanently disabled.
  const pin = els['btn-pin'];
  window.pywebview = { api: {
    get_window_state: async () => ({ expanded: false, on_top: true }),
    set_on_top: async () => { throw new Error('native failure'); },
    get_endpoint: () => new Promise(() => {}),
  } };
  await window.listeners.pywebviewready();
  await flush();
  assert.strictEqual(pin.getAttribute('aria-pressed'), 'true');
  await pin.listeners.click();
  assert.strictEqual(pin.disabled, false);
  assert.strictEqual(pin.getAttribute('aria-pressed'), 'true');
  assert(!body.classes.has('offline'));
  assert(!els['chat-send'].disabled);
  window.pywebview.api.set_on_top = async (on_top) => ({ on_top });
  await pin.listeners.click();
  assert.strictEqual(pin.getAttribute('aria-pressed'), 'false');
  assert(pin.title.startsWith('Pin:'));
  window.pywebview.api.set_on_top = () => new Promise(() => {});
  const pendingPin = pin.listeners.click();
  await flush();
  assert(pin.disabled);
  [...timers.values()].filter((t) => t.ms === 2000).forEach((t) => t.fn());
  await pendingPin;
  assert(!pin.disabled);
  backend.online = false;
  await pollOnce();
  assert(body.classes.has('offline'));
  backend.online = true;
  await pollOnce();
  assert(!body.classes.has('offline'), 'HTTP recovers while get_endpoint never resolves');
  // A rendering exception must not be reported as a network disconnect or
  // prevent the next refresh.
  const original = Sage.setState;
  Sage.setState = () => { throw new Error('render failure'); };
  await pollOnce();
  assert(!body.classes.has('offline'));
  Sage.setState = original;
  backend.state.state = 'thinking';
  await pollOnce();
  assert.strictEqual(Sage.state, 'thinking');
  console.log('chat.js harness: all checks passed');
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
