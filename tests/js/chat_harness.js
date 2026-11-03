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
  'notice', 'btn-mic', 'btn-speech-cancel', 'speech-status', 'speech-feedback', 'speech-meter',
  'btn-speech-settings', 'speech-dialog', 'speech-form', 'speech-model', 'speech-shortcut',
  'speech-configured', 'speech-setting-error', 'speech-save', 'speech-done', 'btn-stop',
];
const els = Object.fromEntries(ids.map((i) => [i, makeEl(i)]));
const body = makeEl('body');
els['speech-dialog'].hidden = true;

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
    session_id: 'session_one',
    epoch: 'one',
    audio: null,
    speech: { phase: 'idle', level: 0, elapsed: 0, error: null, result: null, configured: true, provider: 'groq', shortcut: 'f7', model: 'whisper-large-v3-turbo' },
  },
  history: [],
  messageError: null,
  historyError: false,
};

function respond(status, data) {
  return Promise.resolve({ ok: status < 400, status, json: async () => data });
}
function fetchStub(url, opts = {}) {
  assert.strictEqual(opts.headers['X-WOS-Token'], 'tok', 'every call carries the token');
  if (!backend.online) return Promise.reject(new Error('offline'));
  if (url.startsWith('/api/state')) {
    calls.lastStateUrl = url;
    if (backend.deferState) {
      backend.deferState = false;
      const captured = JSON.parse(JSON.stringify(backend.state));
      return new Promise((resolve) => { backend.releaseState = () => resolve({ ok: true, status: 200, json: async () => captured }); });
    }
    return respond(200, backend.state);
  }
  if (url === '/api/history') {
    if (backend.historyError) { backend.historyError = false; return respond(503, { error: 'history failed' }); }
    return respond(200, { character: backend.state.character, messages: backend.history, session_id: backend.state.session_id });
  }
  const payload = opts.body ? JSON.parse(opts.body) : null;
  calls.posts.push([url, payload]);
  if (url === '/api/stt') {
    const speech = backend.state.speech;
    if (backend.speechError) { backend.speechError = false; return respond(400, { error: 'Microphone unavailable. Your draft is safe.' }); }
    if (payload.action === 'start') Object.assign(speech, { phase: 'recording', error: null, result: null });
    if (payload.action === 'stop') Object.assign(speech, { phase: 'transcribing', level: 0 });
    if (payload.action === 'cancel') Object.assign(speech, { phase: 'idle', result: null, level: 0 });
    if (payload.action === 'configure') Object.assign(speech, payload.settings);
    if (payload.action === 'ack') {
      if (backend.ackError) { backend.ackError = false; return respond(503, { error: 'Retry ack' }); }
      if (speech.result && speech.result.id === payload.result_id) speech.result = null;
    }
    if (backend.deferSpeech) {
      backend.deferSpeech = false;
      const captured = JSON.parse(JSON.stringify(speech));
      return new Promise((resolve) => { backend.releaseSpeech = () => resolve({ ok: true, status: 200, json: async () => captured }); });
    }
    return respond(200, { ...speech });
  }
  if (url === '/api/stop') {
    Object.assign(backend.state.speech, { phase: 'idle', result: null, level: 0 });
    backend.state.state = 'idle'; backend.state.audio = null;
    return respond(200, backend.state);
  }
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
  document: { body, hasFocus: () => backend.focused !== false, getElementById: (id) => els[id], createElement: () => makeEl('x') },
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

  // 2. idle never feeds levels after the initial session reset.
  calls.levels.length = 0;
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
  backend.messageError = 'Auralis is busy with a terminal command.';
  els['chat-input'].value = 'again';
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  await flush();
  assert.strictEqual(els['chat-input'].value, 'again');
  const err = els['chat-log'].children[els['chat-log'].children.length - 1];
  assert(err.className.includes('error'));
  assert.strictEqual(err.children[1].textContent, 'Auralis is busy with a terminal command.');

  // 10. empty messages are ignored
  const before = calls.posts.length;
  els['chat-input'].value = '   ';
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  assert.strictEqual(calls.posts.length, before);

  // Desktop starts with chat visible and does not expose native resizing controls.
  assert.strictEqual(els['chat'].hidden, false);
  assert(window.AuralisClient);
  assert(!('token' in window.AuralisClient));
  window.AuralisClient.setBusy(true);
  assert.strictEqual(els['chat-send'].disabled, true);
  window.AuralisClient.setBusy(false);
  assert.strictEqual(els['chat-send'].disabled, false);

  // Drafts are restored after switching chats and remain separate after failures.
  els['chat-input'].value = 'first draft';
  window.AuralisClient.applyState({ ...backend.state, session_id: 'session_two' });
  assert.strictEqual(els['chat-input'].value, '');
  els['chat-input'].value = 'second draft';
  window.AuralisClient.applyState(backend.state);
  assert.strictEqual(els['chat-input'].value, 'first draft');
  await flush();

  // A transient history failure retries even though chat_version stays unchanged.
  backend.historyError = true;
  backend.history = [{ role: 'assistant', content: 'Recovered history' }];
  backend.state.chat_version++;
  await pollOnce();
  assert(els.notice.textContent.includes('Retrying'));
  now += 1100;
  await pollOnce();
  assert.strictEqual(els['chat-log'].children[0].children[1].textContent, 'Recovered history');

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

  // A hanging endpoint bridge cannot freeze HTTP reconnects.
  window.pywebview = { api: { get_endpoint: () => new Promise(() => {}) } };
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
  // An old poll response cannot undo a completed Desktop session switch.
  backend.deferState = true;
  await pollOnce();
  window.AuralisClient.applyState({ ...backend.state, session_id: 'newer_session' });
  await flush();
  backend.releaseState();
  await flush();
  assert.strictEqual(window.AuralisClient.state().sessionId, 'newer_session');
  // Native microphone capture is deliberate; typing remains editable and sends wait.
  backend.state.session_id = 'newer_session';
  backend.state.state = 'idle';
  backend.state.audio = null;
  await pollOnce();
  const speechPosts = () => calls.posts.filter(([url]) => url === '/api/stt');
  const key = (name, extras = {}) => window.listeners.keydown({ key: name, preventDefault() { this.defaultPrevented = true; }, ...extras });
  els['chat-input'].value = 'Typed before recording';
  key('F7'); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'recording');
  assert.equal(window.AuralisClient.state().busy, true);
  assert.equal(els['chat-send'].disabled, true);
  assert.equal(els['chat-input'].disabled, false);
  assert.equal(Sage.state, 'listening');
  assert.deepEqual(speechPosts().at(-1)[1], { action: 'start', session_id: 'newer_session' });
  const messageCount = calls.posts.filter(([url]) => url === '/api/message').length;
  await els['chat-form'].listeners.submit({ preventDefault() {} });
  assert.equal(calls.posts.filter(([url]) => url === '/api/message').length, messageCount);
  backend.state.speech.level = 0.63;
  await pollOnce();
  assert.equal(calls.levels.at(-1), 0.63, 'avatar uses native microphone level');
  key('F7'); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'transcribing');
  els['chat-input'].value += ' and while waiting';
  backend.ackError = true;
  backend.state.speech = { ...backend.state.speech, phase: 'idle', result: { id: 1, session_id: 'newer_session', text: 'A spoken thought' } };
  await pollOnce();
  assert.equal(els['chat-input'].value, 'Typed before recording and while waiting\nA spoken thought');
  await pollOnce();
  assert.equal(els['chat-input'].value, 'Typed before recording and while waiting\nA spoken thought', 'retrying an ack never repeats the transcript');
  assert.equal(speechPosts().filter(([, data]) => data.action === 'ack' && data.result_id === 1).length, 2);
  assert.equal(calls.posts.filter(([url]) => url === '/api/message').length, messageCount, 'transcripts never auto-send');
  // Cross-chat and canceled results are acknowledged and discarded.
  backend.state.speech.result = { id: 2, session_id: 'older_session', text: 'Wrong chat' };
  await pollOnce();
  assert(!els['chat-input'].value.includes('Wrong chat'));
  await els['btn-mic'].listeners.click(); await flush();
  key('Escape'); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'idle');
  backend.state.speech.result = { id: 3, session_id: 'newer_session', text: 'Canceled text' };
  await pollOnce();
  assert(!els['chat-input'].value.includes('Canceled text'));
  // Cancel queues behind a slow start, and suppressed results cannot revive it.
  backend.deferSpeech = true;
  els['btn-mic'].listeners.click(); await flush();
  key('Escape');
  backend.releaseSpeech(); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'idle');
  assert.deepEqual(speechPosts().slice(-2).map(([, data]) => data.action), ['start', 'cancel']);
  // Microphone errors preserve the latest draft and recover on a new attempt.
  const draft = els['chat-input'].value;
  backend.speechError = true;
  await els['btn-mic'].listeners.click(); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'error');
  assert.equal(els['chat-input'].value, draft);
  assert(els['speech-status'].textContent.includes('Microphone unavailable'));
  // Configurable focus-only shortcuts never intercept Ctrl+C or held-key repeats.
  await els['btn-speech-settings'].listeners.click();
  els['speech-model'].value = 'whisper-large-v3'; els['speech-shortcut'].value = 'f9';
  await els['speech-form'].listeners.submit({ preventDefault() {} }); await flush();
  assert.equal(window.AuralisClient.state().speech.shortcut, 'f9');
  assert.equal(els['speech-dialog'].hidden, true);
  const beforeShortcut = speechPosts().length;
  key('F7'); key('F9', { repeat: true }); key('c', { ctrlKey: true });
  backend.focused = false; key('F9'); backend.focused = true; await flush();
  assert.equal(speechPosts().length, beforeShortcut);
  key('F9'); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'recording');
  await els['btn-stop'].listeners.click(); await flush();
  assert.equal(window.AuralisClient.state().speech.phase, 'idle');
  assert.equal(els['chat-input'].value, draft);
  backend.state.busy = true; await pollOnce();
  assert.equal(window.AuralisClient.state().backendBusy, true);
  assert.equal(els['chat-send'].disabled, true);
  const beforeTerminalBusy = speechPosts().length; key('F9'); await flush();
  assert.equal(speechPosts().length, beforeTerminalBusy, 'terminal work shares the same busy gate');
  backend.state.busy = false; await pollOnce();
  console.log('chat.js harness: all checks passed');
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
