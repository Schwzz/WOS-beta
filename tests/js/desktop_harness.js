// Desktop control paths run against a tiny DOM and synthetic backend, without network or real keys.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function element(id) {
  let text = '';
  const el = { id, children: [], listeners: {}, attrs: {}, value: '', hidden: false, disabled: false,
    classes: new Set(), addEventListener(type, fn) { this.listeners[type] = fn; },
    append(...nodes) { this.children.push(...nodes); }, appendChild(node) { this.children.push(node); },
    setAttribute(key, value) { this.attrs[key] = value; }, getAttribute(key) { return this.attrs[key]; }, focus() {},
  };
  Object.defineProperty(el, 'textContent', { get: () => text, set: (value) => { text = value; if (!value) el.children = []; } });
  el.classList = { contains: (name) => el.classes.has(name), toggle(name, force) {
    (force === undefined ? !el.classes.has(name) : force) ? el.classes.add(name) : el.classes.delete(name);
  } };
  return el;
}
const web = path.join(__dirname, '../../overlay/web');
const ids = [...fs.readFileSync(path.join(web, 'index.html'), 'utf8').matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
const els = Object.fromEntries(ids.map((id) => [id, element(id)])), body = element('body');
['chat-actions', 'desktop-dialog', 'credentials-dialog', 'avatar-panel'].forEach((id) => { els[id].hidden = true; });
let state = { linked: true, sending: false, switchingModel: false, busy: false, sessionId: 'one', characterId: 'nexis', character: 'Nexis', provider: 'cloud', model: 'model-a', chatTitle: 'First chat' };
let chats = [{ session_id: 'one', title: 'First chat', character_id: 'nexis' }, { session_id: 'two', title: 'Second chat', character_id: 'luna' }];
const calls = [], notices = [], subscribers = [], credentials = {
  providers: [{ id: 'cloud', label: 'Gemini', configured: true, key_env: 'GEMINI_API_KEY' }, { id: 'openai', label: 'OpenAI', configured: false, key_env: 'OPENAI_API_KEY' }, { id: 'ollama', label: 'Ollama', configured: true, key_env: null }], voice: { configured: false }, speech: { configured: false },
};
let failAction = null, pendingAction = null, release = null, refreshes = 0;
const publish = () => subscribers.forEach((fn) => fn({ ...state }));
const response = () => ({ session_id: state.sessionId, character: state.character, character_id: state.characterId, chat_title: state.chatTitle });
const client = {
  state: () => ({ ...state }), subscribe(fn) { subscribers.push(fn); fn({ ...state }); },
  setBusy(busy) { state.busy = busy; publish(); }, notice(text = '') { notices.push(text); },
  applyState(result) { state.sessionId = result.session_id; state.chatTitle = result.chat_title; state.character = result.character; state.characterId = result.character_id; publish(); },
  async refreshProviders() { refreshes++; }, async refreshModels() { refreshes++; },
  async request(url, options = {}) {
    const data = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, data });
    if (url === '/api/chats' && !data) return { chats: chats.map((item) => ({ ...item })), session_id: state.sessionId };
    if (url === '/api/characters') return { characters: [{ id: 'nexis', name: 'Nexis' }, { id: 'luna', name: 'Nexis' }], current: state.characterId };
    if (url === '/api/credentials') {
      if (data) {
        if (failAction === 'credentials') throw new Error('provider echoed pretend-secret');
        const target = data.provider === 'fish_audio' ? credentials.voice : data.provider === 'groq' ? credentials.speech : credentials.providers.find((item) => item.id === data.provider);
        target.configured = true;
      }
      return credentials;
    }
    if (url === '/api/character') {
      if (failAction === 'character') throw new Error('Character unavailable');
      return { ...response(), character: 'Nexis', character_id: data.character };
    }
    if (url === '/api/chats') {
      if (pendingAction === data.action) return new Promise((resolve) => { release = resolve; });
      if (failAction === data.action) throw new Error('Synthetic action failure');
      assert.equal(data.expected_session_id, state.sessionId);
      if (data.action === 'new') {
        chats.push({ session_id: 'three', title: 'New chat', character_id: 'nexis' });
        return { ...response(), session_id: 'three', chat_title: 'New chat' };
      }
      if (data.action === 'switch') { const item = chats.find((item) => item.session_id === data.session_id); return { ...response(), session_id: item.session_id, chat_title: item.title }; }
      if (data.action === 'rename') { chats.find((item) => item.session_id === data.session_id).title = data.title; return { ...response(), chat_title: data.title }; }
      if (data.action === 'delete') { assert.equal(data.confirm, true); chats = chats.filter((item) => item.session_id !== data.session_id); return { ...response(), session_id: 'one', chat_title: 'First chat' }; }
      if (data.action === 'clear') { assert.equal(data.confirm, true); return response(); }
    }
    throw new Error('Unexpected endpoint: ' + url);
  },
};
const window = { AuralisClient: client, innerWidth: 1100, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; } };
vm.runInNewContext(fs.readFileSync(path.join(web, 'desktop.js'), 'utf8'), {
  window, document: { body, getElementById: (id) => els[id], createElement: () => element('new') },
  Array, String, JSON, Object, Promise,
});
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise((resolve) => setImmediate(resolve)); };
const click = async (id) => { const result = els[id].listeners.click(); await result; await flush(); };
const submit = async (id) => { await els[id].listeners.submit({ preventDefault() {} }); await flush(); };
(async () => {
  await flush();
  assert.equal(els['saved-chats'].children.length, 2);
  assert.equal(els['saved-chats'].children[0].attrs['aria-current'], 'true');
  await click('btn-sidebar'); assert(body.classes.has('sidebar-collapsed'));
  await click('btn-sidebar'); assert(!body.classes.has('sidebar-collapsed'));
  await click('btn-avatar'); assert.equal(els['avatar-panel'].hidden, false);
  await click('btn-avatar'); assert.equal(els['avatar-panel'].hidden, true);
  failAction = 'switch';
  await els['saved-chats'].children[1].listeners.click(); await flush();
  assert.equal(state.sessionId, 'one'); assert(notices.at(-1).includes('failure')); assert.equal(state.busy, false);
  failAction = null;
  await click('btn-new-chat'); assert.equal(state.sessionId, 'three');
  assert.equal(els['saved-chats'].children.length, 3);
  await click('btn-rename-chat');
  els['dialog-input'].value = ' '; await submit('dialog-form');
  assert.equal(els['dialog-error'].hidden, false);
  els['dialog-input'].value = '  A thoughtful chat  '; await submit('dialog-form');
  assert.equal(state.chatTitle, 'A thoughtful chat'); assert.equal(els['desktop-dialog'].hidden, true);
  await click('btn-clear-chat');
  const beforeClear = calls.filter((call) => call.data && call.data.action === 'clear').length;
  await click('dialog-cancel');
  assert.equal(calls.filter((call) => call.data && call.data.action === 'clear').length, beforeClear);
  await click('btn-clear-chat'); await submit('dialog-form');
  assert(calls.some((call) => call.data?.action === 'clear' && call.data.confirm === true));
  failAction = 'delete'; await click('btn-delete-chat'); await submit('dialog-form');
  assert.equal(state.sessionId, 'three'); assert.equal(els['desktop-dialog'].hidden, false);
  assert.equal(els['dialog-error'].hidden, false);
  failAction = null; await submit('dialog-form'); assert.equal(state.sessionId, 'one');
  els['character-select'].value = 'luna'; await els['character-select'].listeners.change(); await flush();
  assert.equal(state.characterId, 'luna');
  assert.equal(els['character-select'].value, 'luna');
  failAction = 'character'; els['character-select'].value = 'nexis'; await els['character-select'].listeners.change();
  assert.equal(els['character-select'].value, 'luna'); failAction = null;
  await click('btn-credentials');
  assert.equal(els['credential-key'].value, '');
  assert(!els['credential-provider'].children.some((option) => option.value === 'ollama'));
  els['credential-provider'].value = 'openai'; els['credential-key'].value = 'synthetic-test-key';
  const saving = els['credentials-form'].listeners.submit({ preventDefault() {} });
  assert.equal(els['credential-key'].value, '', 'key cleared before request settles'); await saving; await flush();
  assert.equal(credentials.providers[1].configured, true); assert.equal(refreshes, 2);
  assert.equal(calls.filter((call) => call.url === '/api/message').length, 0, 'setup never sends a model message');
  failAction = 'credentials'; els['credential-key'].value = 'pretend-secret'; await submit('credentials-form');
  assert.equal(els['credential-key'].value, '');
  assert(!els['credential-error'].textContent.includes('pretend-secret'));
  assert(!notices.join('').includes('pretend-secret')); failAction = null;
  assert(els['credential-provider'].children.some((option) => option.value === 'groq' && option.textContent.includes('speech to text')));
  els['credential-provider'].value = 'groq'; els['credential-key'].value = 'synthetic-speech-key';
  await submit('credentials-form');
  assert.equal(credentials.speech.configured, true);
  assert.equal(refreshes, 2, 'speech setup does not refresh chat model catalogs');
  assert.equal(els['credential-key'].value, '');
  assert(els['credential-status'].textContent.includes('Speech input is ready'));
  assert(calls.some((call) => call.url === '/api/credentials' && call.data?.provider === 'groq'));
  assert.equal(calls.filter((call) => call.url === '/api/message').length, 0);
  await click('btn-credentials-done'); assert.equal(els['credentials-dialog'].hidden, true);
  // Shared busy state covers speech capture/transcription and work begun in Terminal.
  state.busy = true; state.speechBusy = true; publish();
  assert.equal(els['btn-new-chat'].disabled, true);
  assert.equal(els['character-select'].disabled, true);
  assert.equal(els['saved-chats'].children[0].disabled, true);
  const beforeBusy = calls.length;
  await click('btn-new-chat');
  await els['saved-chats'].children[1].listeners.click(); await flush();
  assert.equal(calls.length, beforeBusy, 'disabled navigation cannot mutate a speech session');
  state.busy = false; state.speechBusy = false; publish();

  // A delayed response cannot bring an old chat back after another control changed it.
  pendingAction = 'switch';
  els['saved-chats'].children[1].listeners.click(); await flush();
  state.sessionId = 'external'; state.chatTitle = 'External chat'; publish();
  release({ session_id: 'two', chat_title: 'Second chat', character: 'Nexis' }); await flush();
  assert.equal(state.sessionId, 'external'); assert.equal(state.busy, false);
  state.linked = false; publish(); assert.equal(els['btn-new-chat'].disabled, true);
  assert.equal(els['character-select'].disabled, true);
  console.log('desktop.js harness: all checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
