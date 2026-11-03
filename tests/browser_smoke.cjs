// Optional browser smoke checks. Install Playwright and its Chromium browser,
// then run: node tests/browser_smoke.cjs. All backend data is synthetic.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const web = path.join(__dirname, '..', 'overlay', 'web');
const state = {
  state: 'idle',
  phase: 'idle',
  epoch: 'one',
  session_id: 'session_001',
  chat_title: 'A saved conversation',
  character: 'Nexis',
  provider: 'cloud',
  model: 'chat-alpha',
  voice: { enabled: false, available: true },
  chat_version: 0,
  audio: null,
};
let prefs = { preset: 'crystal', accent: '#a78bfa', size: 1, glow: 0.7, motion: 1, theme: 'dark' };
let messages = Array.from({ length: 80 }, (_, i) => ({
  role: i % 2 ? 'assistant' : 'user',
  content: 'A saved message ' + i + '\nA little more detail for reading and scrolling.',
}));
let online = true,
  loseNext = false,
  stopCount = 0;
const savedChats = [
  { session_id: 'session_001', title: 'A saved conversation', character_id: 'nexis', provider: 'cloud', model: 'chat-alpha' },
  { session_id: 'session_002', title: 'Another thought', character_id: 'nexis', provider: 'cloud', model: 'chat-alpha' },
];
const histories = new Map([['session_001', messages], ['session_002', []]]);
let sessionNumber = 2;
const credentialMetadata = { providers: [
  { id: 'cloud', label: 'Gemini', configured: true, key_env: 'GEMINI_API_KEY' },
  { id: 'openai', label: 'OpenAI', configured: false, key_env: 'OPENAI_API_KEY' },
  { id: 'ollama', label: 'Ollama', configured: true, key_env: null },
], voice: { configured: false } };
const sent = [],
  dedup = new Map();
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  function json(status, data) {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  }
  if (url.pathname.startsWith('/api/')) {
    if (req.headers['x-wos-token'] !== 'test-token')
      return json(401, { error: 'Missing or wrong token.' });
    if (!online) return json(503, { error: 'offline' });
    let data = {};
    if (req.method === 'POST') {
      let text = '';
      for await (const part of req) text += part;
      data = JSON.parse(text || '{}');
    }
    if (url.pathname === '/api/state') return json(200, state);
    if (url.pathname === '/api/chats') {
      if (req.method === 'GET') return json(200, { chats: savedChats, session_id: state.session_id });
      if (data.expected_session_id !== state.session_id) return json(409, { error: 'The active chat changed.' });
      histories.set(state.session_id, messages);
      let chat = savedChats.find((item) => item.session_id === data.session_id);
      if (data.action === 'new') {
        chat = { session_id: 'session_00' + (++sessionNumber), title: 'New chat', character_id: 'nexis' };
        savedChats.push(chat); histories.set(chat.session_id, []);
      } else if (!chat) return json(404, { error: 'Chat not found.' });
      if (data.action === 'rename') chat.title = data.title;
      if (data.action === 'clear') {
        if (data.confirm !== true) return json(400, { error: 'Confirmation required.' });
        histories.set(chat.session_id, []);
      }
      if (data.action === 'delete') {
        if (data.confirm !== true) return json(400, { error: 'Confirmation required.' });
        savedChats.splice(savedChats.indexOf(chat), 1); histories.delete(chat.session_id); chat = savedChats[0];
      }
      state.session_id = chat.session_id; state.chat_title = chat.title;
      state.character = chat.character_id === 'luna' ? 'Luna' : 'Nexis';
      messages = histories.get(state.session_id) || []; state.chat_version++;
      return json(200, state);
    }
    if (url.pathname === '/api/characters') return json(200, { characters: [
      { id: 'nexis', name: 'Nexis' }, { id: 'luna', name: 'Luna' },
    ], current: state.character.toLowerCase() });
    if (url.pathname === '/api/character') {
      if (data.session_id !== state.session_id) return json(409, { error: 'Chat changed.' });
      state.character = data.character === 'luna' ? 'Luna' : 'Nexis';
      savedChats.find((item) => item.session_id === state.session_id).character_id = data.character;
      return json(200, state);
    }
    if (url.pathname === '/api/credentials') {
      if (req.method === 'POST') {
        if (data.key === 'invalid-test-key') return json(400, { error: 'Synthetic credential rejected' });
        const item = data.provider === 'fish_audio' ? credentialMetadata.voice : credentialMetadata.providers.find((item) => item.id === data.provider);
        item.configured = true;
      }
      return json(200, credentialMetadata);
    }

    if (url.pathname === '/api/history') {
      const end = url.searchParams.has('before')
        ? +url.searchParams.get('before')
        : messages.length;
      const start = Math.max(0, end - 100);
      return json(200, {
        character: 'Nexis',
        messages: messages.slice(start, end),
        before: start,
        has_more: start > 0,
        session_id: state.session_id,
      });
    }
    if (url.pathname === '/api/providers') return json(200, { providers: [
      { id: 'cloud', label: 'Gemini', configured: true },
      { id: 'openrouter', label: 'OpenRouter', configured: true },
      { id: 'openai', label: 'OpenAI', configured: false },
    ] });
    if (url.pathname === '/api/models') {
      const provider = url.searchParams.get('provider') || state.provider;
      return json(200, { provider, current: provider === state.provider ? state.model : null,
        source: 'live', warning: null,
        models: provider === 'cloud' ? [
          { id: 'chat-alpha', label: 'Chat Alpha' }, { id: 'chat-beta', label: 'Chat Beta' },
        ] : [{ id: 'vendor/chat-gamma', label: 'Chat Gamma' }],
      });
    }
    if (url.pathname === '/api/provider') {
      if (data.expected_provider !== state.provider || data.session_id !== state.session_id)
        return json(409, { error: 'The chat or provider changed.' });
      state.provider = data.provider;
      state.model = data.model;
      state.chat_version++;
      return json(200, state);
    }
    if (url.pathname === '/api/model') {
      state.model = data.model;
      state.chat_version++;
      return json(200, state);
    }
    if (url.pathname === '/api/preferences') {
      if (req.method === 'POST') prefs = { ...prefs, ...data };
      return json(200, prefs);
    }
    if (url.pathname === '/api/voice') {
      state.voice.enabled = data.enabled;
      return json(200, state);
    }
    if (url.pathname === '/api/stop') {
      stopCount++;
      state.state = state.phase = 'idle';
      state.audio = null;
      return json(200, state);
    }
    if (url.pathname === '/api/message') {
      sent.push(data);
      if (!dedup.has(data.request_id)) {
        messages.push(
          { role: 'user', content: data.text },
          { role: 'assistant', content: 'I’m here. Let’s make something good.' },
        );
        state.chat_version++;
        dedup.set(data.request_id, true);
      }
      if (loseNext) {
        loseNext = false;
        res.destroy();
        return;
      }
      return json(200, { reply: 'I’m here.' });
    }
    return json(404, { error: 'Unknown route' });
  }
  const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  if (!['index.html', 'style.css', 'chat.js', 'sage.js', 'desktop.js'].includes(name)) {
    res.writeHead(404);
    return res.end();
  }
  res.writeHead(200, {
    'Content-Type': name.endsWith('.js')
      ? 'text/javascript'
      : name.endsWith('.css')
        ? 'text/css'
        : 'text/html',
  });
  res.end(fs.readFileSync(path.join(web, name)));
});
(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.WOS_CHROMIUM_EXECUTABLE || undefined,
    args: process.env.WOS_CHROMIUM_EXECUTABLE
      ? [
          '--no-sandbox',
          '--disable-dev-shm-usage',
          '--no-zygote',
          '--single-process',
          '--use-gl=angle',
          '--use-angle=swiftshader',
        ]
      : [],
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1100, height: 760 },
      reducedMotion: 'reduce',
    });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/#test-token`);
    await page.waitForFunction(() => document.body.classList.contains('linked'));
    await page.waitForFunction(() => document.querySelectorAll('.msg').length === 80);
    assert.equal(await page.locator('#hud-title').innerText(), 'Auralis\nDESKTOP');
    assert.equal(await page.locator('#chat').isVisible(), true);
    await page.waitForFunction(() => document.querySelectorAll('.saved-chat').length === 2);
    assert.equal(await page.locator('#btn-pin, #btn-minimize, #btn-close, #resize-handle, .pywebview-drag-region').count(), 0);
    assert.equal(await page.locator('#character-name').innerText(), 'Nexis');
    const shots = process.env.WOS_SCREENSHOTS;
    if (shots) {
      fs.mkdirSync(shots, { recursive: true });
      await page.screenshot({ path: path.join(shots, 'desktop.png') });
    }
    await page.setViewportSize({ width: 960, height: 720 });
    await page.waitForFunction(() => document.querySelector('#model-select').options.length === 2);
    assert.equal(await page.locator('#model-select').inputValue(), 'chat-alpha');
    await page.locator('#model-select').selectOption('chat-beta');
    await page.waitForFunction(() => document.querySelector('#model-select').value === 'chat-beta');
    assert.equal(state.model, 'chat-beta');
    await page.locator('#provider-select').selectOption('openrouter');
    await page.waitForFunction(() => document.querySelector('#model-select').options.length === 2 &&
      document.querySelector('#model-select').value === '');
    assert.equal(state.provider, 'cloud', 'previewing a provider does not change the chat');
    assert.equal(await page.locator('#chat-send').isDisabled(), true, 'choose a model before sending to a new provider');
    await page.locator('#model-select').selectOption('vendor/chat-gamma');
    await page.waitForFunction(() => document.querySelector('#provider-label').textContent === 'OPENROUTER' &&
      document.querySelector('#model-select').value === 'vendor/chat-gamma');
    assert.equal(state.provider, 'openrouter');
    await page.locator('#provider-select').selectOption('cloud');
    await page.waitForFunction(() => document.querySelector('#model-select').options.length === 3);
    await page.locator('#model-select').selectOption('chat-beta');
    await page.waitForFunction(() => document.querySelector('#provider-label').textContent === 'GEMINI' &&
      document.querySelector('#model-select').value === 'chat-beta');
    // Reading older text must survive background updates without jumping.
    await page.locator('#chat-log').evaluate((el) => {
      el.scrollTop = 100;
    });
    state.chat_version++;
    messages.push({ role: 'assistant', content: 'An update while you are reading.' });
    await page.waitForFunction(() => document.querySelectorAll('.msg').length === 81);
    assert.equal(await page.locator('#chat-log').evaluate((el) => el.scrollTop), 100);
    // A lost response can be retried with the same identifier.
    await page.route(
      '**/api/message',
      async (route) => {
        await route.fetch();
        await route.abort('failed');
      },
      { times: 1 },
    );
    await page.locator('#chat-input').fill('Do you remember this?');
    await page.locator('#chat-send').click();
    await page.waitForFunction(
      () =>
        document.querySelector('#chat-input').value === 'Do you remember this?' &&
        !document.querySelector('#chat-send').disabled,
    );
    await page.locator('#chat-send').click();
    await page.waitForFunction(
      () =>
        !document.querySelector('#chat-send').disabled &&
        document.querySelector('#chat-input').value === '',
    );
    assert.equal(sent.length, 2);
    assert.equal(sent[0].request_id, sent[1].request_id);
    assert.equal(messages.filter((m) => m.content === 'Do you remember this?').length, 1);
    // Message content remains text rather than becoming executable markup.
    await page.locator('#chat-input').fill('<img src=x onerror="window.injected=true">');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => !document.querySelector('#chat-send').disabled);
    assert.equal(await page.locator('#chat-log img').count(), 0);
    // Appearance updates persist and zero motion remains a valid preference.
    await page.locator('#btn-settings').click();
    await page.locator('#pref-theme').selectOption('light');
    await page.locator('#pref-preset').selectOption('rings');
    await page.locator('#pref-motion').fill('0');
    await page.locator('#pref-motion').dispatchEvent('input');
    await page.waitForFunction(() => document.body.dataset.theme === 'light');
    await page.waitForResponse(
      async (response) =>
        response.url().endsWith('/api/preferences') &&
        response.request().method() === 'POST' &&
        (await response.json()).motion === 0,
    );
    assert.equal(prefs.motion, 0);
    assert.equal(prefs.preset, 'rings');
    await page.locator('#btn-settings-done').click();
    // Offline controls cannot launch more requests, and a draft survives reconnect.
    await page.locator('#chat-input').fill('A draft to keep');
    online = false;
    await page.waitForFunction(() => document.querySelector('#chat-send').disabled);
    assert.equal(await page.locator('#btn-voice').isDisabled(), true);
    assert.equal(await page.locator('#chat-input').inputValue(), 'A draft to keep');
    online = true;
    await page.waitForFunction(() => !document.querySelector('#chat-send').disabled);
    // A new backend with the same revision still refreshes the conversation.
    state.epoch = 'two';
    state.session_id = 'session_002';
    state.chat_version = 0;
    messages = [
      { role: 'user', content: 'Let’s work on Auralis.' },
      {
        role: 'assistant',
        content:
          'Ready when you are. We can start with the voice controls, then make the Companion feel more like yours.',
      },
    ];
    await page.waitForFunction(() => document.querySelectorAll('.msg').length === 2);
    await page.locator('#btn-stop').click();
    assert.equal(stopCount, 1);
    if (shots) {
      await page.screenshot({ path: path.join(shots, 'chat.png') });
    }
    // Responsive layouts remain usable at the minimum native size.
    for (const [width, height] of [
      [280, 300],
      [340, 420],
      [480, 720],
      [760, 900],
    ]) {
      await page.setViewportSize({ width, height });
      const box = await page.locator('#chat-input').boundingBox();
      assert(
        box && box.width > 70 && box.y + box.height <= height,
        `composer fits ${width}x${height}`,
      );
    }
    await page.setViewportSize({ width: 1100, height: 760 });
    // Saved-chat navigation preserves per-chat drafts and uses confirmed destructive actions.
    await page.locator('#chat-input').fill('A draft in another chat');
    await page.locator('#btn-new-chat').click();
    await page.waitForFunction(() => document.querySelector('#chat-input').value === '' && !document.querySelector('#btn-new-chat').disabled);
    assert.equal(await page.locator('#empty-chat').isVisible(), true);
    await page.locator('#btn-chat-actions').click(); await page.locator('#btn-rename-chat').click();
    await page.locator('#dialog-input').fill('A fresh perspective'); await page.locator('#dialog-confirm').click();
    await page.waitForFunction(() => document.querySelector('#chat-title').textContent === 'A fresh perspective' && document.querySelector('#desktop-dialog').hidden);
    await page.locator('#character-select').selectOption('luna');
    await page.waitForFunction(() => document.querySelector('#character-name').textContent === 'Luna' && !document.querySelector('#btn-new-chat').disabled);
    await page.locator('#chat-input').fill('A new chat draft');
    await page.locator('.saved-chat').filter({ hasText: 'Another thought' }).click();
    await page.waitForFunction(() => document.querySelector('#chat-input').value === 'A draft in another chat');
    await page.locator('.saved-chat').filter({ hasText: 'A fresh perspective' }).click();
    await page.waitForFunction(() => document.querySelector('#chat-input').value === 'A new chat draft');
    await page.locator('#btn-chat-actions').click(); await page.locator('#btn-clear-chat').click();
    await page.locator('#dialog-cancel').click();
    assert.equal(await page.locator('#chat-input').inputValue(), 'A new chat draft');
    await page.locator('#btn-chat-actions').click(); await page.locator('#btn-clear-chat').click();
    await page.locator('#dialog-confirm').click();
    await page.waitForFunction(() => document.querySelector('#desktop-dialog').hidden);
    await page.locator('#btn-chat-actions').click(); await page.locator('#btn-delete-chat').click();
    await page.locator('#dialog-confirm').click();
    await page.waitForFunction(() => document.querySelector('#desktop-dialog').hidden && document.querySelectorAll('.saved-chat').length === 2);
    // Masked key setup only configures a provider and refreshes model discovery.
    const sentBeforeSetup = sent.length;
    await page.locator('#btn-credentials').click();
    await page.waitForFunction(() => document.querySelector('#credential-provider').options.length === 3);
    assert.equal(await page.locator('#credential-key').getAttribute('type'), 'password');
    await page.locator('#credential-provider').selectOption('openai');
    await page.locator('#credential-key').fill('synthetic-test-key');
    await page.locator('#btn-save-credential').click();
    await page.waitForFunction(() => document.querySelector('#credential-status').textContent.includes('Key saved') && !document.querySelector('#btn-save-credential').disabled);
    assert.equal(await page.locator('#credential-key').inputValue(), '');
    assert.equal(sent.length, sentBeforeSetup);
    assert.equal(await page.evaluate(() => document.body.innerText.includes('synthetic-test-key')), false);
    await page.locator('#credential-key').fill('invalid-test-key');
    await page.locator('#btn-save-credential').click();
    await page.waitForFunction(() => !document.querySelector('#credential-error').hidden);
    assert.equal(await page.locator('#credential-key').inputValue(), '');
    await page.locator('#btn-credentials-done').click();
    await page.locator('#btn-avatar').click(); assert.equal(await page.locator('#stage').isVisible(), true);
    await page.locator('#btn-avatar').click(); assert.equal(await page.locator('#avatar-panel').isVisible(), false);
    // The native shell owns all window controls. A hanging endpoint bridge leaves HTTP healthy.
    await page.evaluate(() => { window.pywebview = { api: { get_endpoint: () => new Promise(() => {}), set_expanded: () => { throw new Error('Must not resize'); } } }; });
    online = false; await page.waitForFunction(() => document.querySelector('#chat-send').disabled);
    online = true; await page.waitForFunction(() => !document.querySelector('#chat-send').disabled);
    assert.deepEqual(errors, []);
    console.log('Desktop browser smoke: all checks passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch((error) => {
  console.error(error);
  server.close();
  process.exitCode = 1;
});
