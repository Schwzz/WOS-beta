/* Shared WOS session controls. Credentials stay in the Python process.
 * Message identifiers make a retry safe when a response was lost in transit. */
(() => {
  'use strict';
  const Sage = window.Sage,
    body = document.body,
    $ = (id) => document.getElementById(id);
  const log = $('chat-log'),
    form = $('chat-form'),
    input = $('chat-input');
  const sendBtn = $('chat-send'),
    voiceBtn = $('btn-voice'),
    chatBtn = $('btn-chat');
  const chatPanel = $('chat'),
    caption = $('line-c');
  const STATUS = {
    idle: 'standing by',
    thinking: 'analysing',
    preparing: 'preparing voice',
    speaking: 'speaking',
    listening: 'listening',
  };
  const defaults = {
    preset: 'crystal',
    accent: '#a78bfa',
    size: 1,
    glow: 0.7,
    motion: 1,
    theme: 'dark',
  };
  let token = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  let linked = false,
    everLinked = false,
    offlineSince = null,
    sending = false,
    expanded = false;
  let character = 'Nexis',
    chatVersion = -1,
    sessionId = null,
    epoch = null,
    model = '',
    provider = 'cloud';
  let voice = { enabled: false, available: true },
    lastState = 'idle',
    haveAudio = null,
    env = null;
  let historySerial = 0,
    rendered = '',
    historyTotal = 0,
    rows = new Map(),
    uncertain = null;
  let modelSerial = 0,
    prefs = { ...defaults },
    prefSave = null,
    lastVoiceError = null;
  let preferenceVersion = 0,
    savingPreferences = false,
    saveAgain = false,
    modelLoading = false,
    switchingModel = false;
  let chatScrollTop = 0,
    followChat = true,
    openedChat = false;
  const drafts = new Map();
  const pyApi = () => window.pywebview && window.pywebview.api;
  function listen(id, event, fn) {
    if ($(id)) $(id).addEventListener(event, fn);
  }
  function notice(text = '') {
    const el = $('notice');
    if (el) {
      el.textContent = text;
      el.hidden = !text;
    }
  }
  function controls() {
    sendBtn.disabled = !linked || sending || switchingModel || chosenProvider() !== provider;
    voiceBtn.disabled = !linked;
    if ($('btn-stop')) $('btn-stop').disabled = !linked;
    if ($('model-select')) $('model-select').disabled = !linked || sending || modelLoading || switchingModel;
    if ($('provider-select')) $('provider-select').disabled = !linked || sending || switchingModel;
    if ($('manual-model')) $('manual-model').disabled = !linked || sending || switchingModel;
    if ($('btn-model-refresh')) $('btn-model-refresh').disabled = !linked || modelLoading || switchingModel;
  }
  // Native bridge calls can fail or never settle. They must not stop the
  // HTTP polling loop or label a healthy backend as disconnected.
  async function shellCall(method, args = [], timeout = 2000) {
    const shell = pyApi();
    if (!shell || typeof shell[method] !== 'function')
      throw new Error('Window controls are unavailable.');
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(() => shell[method](...args)),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('The window control did not respond.')), timeout);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }
  async function windowAction(method, args = []) {
    try {
      const state = await shellCall(method, args);
      if (state && state.error) throw new Error(state.error);
      return state;
    } catch (_) {
      notice('Could not change the window. Try again; connection checks will continue.');
      return null;
    }
  }
  let endpointRefresh = null;
  function refreshEndpoint() {
    if (!endpointRefresh)
      endpointRefresh = readFreshEndpoint().finally(() => { endpointRefresh = null; });
    return endpointRefresh;
  }
  async function readFreshEndpoint() {
    try {
      const shell = pyApi(),
        ep = shell && shell.get_endpoint ? await shellCall('get_endpoint') : null;
      if (!ep || !ep.token) return false;
      if (ep.port && location.port && +location.port !== ep.port) {
        location.replace(`http://127.0.0.1:${ep.port}/#${ep.token}`);
        return false;
      }
      const changed = ep.token !== token;
      token = ep.token;
      return changed;
    } catch (_) {
      return false;
    }
  }
  async function api(path, options = {}, timeout = 5000, retry = true) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const res = await fetch(path, {
        ...options,
        signal: controller.signal,
        headers: { 'X-WOS-Token': token, 'Content-Type': 'application/json' },
      });
      if (res.status === 401 && retry && (await refreshEndpoint()))
        return api(path, options, timeout, false);
      let data = null;
      try {
        data = await res.json();
      } catch (_) {}
      if (!res.ok) {
        const err = new Error((data && data.error) || `WOS returned HTTP ${res.status}.`);
        err.status = res.status;
        throw err;
      }
      return data;
    } catch (err) {
      if (err.status) throw err;
      const failure = new Error(
        err.name === 'AbortError'
          ? 'WOS took too long to respond. Check the connection and try again.'
          : 'Connection to WOS was lost. Reconnecting…',
      );
      failure.uncertain = true;
      throw failure;
    } finally {
      clearTimeout(timer);
    }
  }
  function addLine(role, text, extra) {
    const row = document.createElement('div');
    row.className = 'msg ' + role + (extra ? ' ' + extra : '');
    const who = document.createElement('b');
    who.textContent = role === 'user' ? 'you' : role === 'assistant' ? character : '!';
    const span = document.createElement('span');
    span.textContent = text;
    row.append(who, span);
    if (role !== 'error') {
      const copy = document.createElement('button');
      copy.className = 'copy-message';
      copy.textContent = 'Copy';
      copy.title = 'Copy message';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(text);
          copy.textContent = 'Copied';
          setTimeout(() => {
            copy.textContent = 'Copy';
          }, 1400);
        } catch (_) {
          notice('Copy is unavailable here. Select the message text and use Ctrl+C.');
        }
      });
      row.append(copy);
    }
    log.appendChild(row);
    return row;
  }
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  async function loadHistory(before = null) {
    const serial = ++historySerial,
      expected = sessionId;
    try {
      const h = await api('/api/history' + (before === null ? '' : '?before=' + before));
      if (
        serial !== historySerial ||
        expected !== sessionId ||
        (h.session_id && h.session_id !== sessionId)
      )
        return;
      if (h.character) character = h.character;
      const start = h.before || 0,
        total = start + h.messages.length;
      if (before === null && total < historyTotal) rows.clear();
      h.messages.forEach((m, i) => rows.set(start + i, m));
      historyTotal = before === null ? total : historyTotal;
      const messages = [...rows.entries()].sort((a, b) => a[0] - b[0]).map((item) => item[1]);
      const signature = JSON.stringify(messages);
      if (signature !== rendered) {
        const follow = nearBottom(),
          top = log.scrollTop,
          height = log.scrollHeight;
        log.textContent = '';
        messages.forEach((m) => addLine(m.role, m.content));
        rendered = signature;
        log.scrollTop =
          before !== null ? top + log.scrollHeight - height : follow ? log.scrollHeight : top;
      }
      const earliest = rows.size ? Math.min(...rows.keys()) : 0;
      if ($('btn-older')) {
        $('btn-older').hidden = earliest === 0;
        $('btn-older').dataset.before = earliest;
      }
      const last = messages
        .slice()
        .reverse()
        .find((m) => m.role === 'assistant');
      caption.textContent = last ? last.content.replace(/\s+/g, ' ') : 'A little space to think.';
    } catch (_) {
      /* State polling owns connection feedback. */
    }
  }
  listen('btn-older', 'click', () => loadHistory(+($('btn-older').dataset.before || 0)));
  function requestId() {
    return window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || sending || switchingModel || !linked || chosenProvider() !== provider) return;
    const id =
      uncertain && uncertain.text === text && uncertain.session === sessionId
        ? uncertain.id
        : requestId();
    const submittedSession = sessionId;
    sending = true;
    controls();
    input.value = '';
    addLine('user', text, 'pending');
    log.scrollTop = log.scrollHeight;
    let failure = null;
    try {
      await api(
        '/api/message',
        {
          method: 'POST',
          body: JSON.stringify({ text, request_id: id, session_id: submittedSession }),
        },
        135000,
      );
      uncertain = null;
      notice();
    } catch (err) {
      failure = err.message;
      if (err.uncertain) {
        uncertain = { id, text, session: submittedSession };
        failure +=
          ' Your reply may have been received; retrying this message will not send it twice.';
      } else uncertain = null;
      if (!input.value && submittedSession === sessionId) input.value = text;
    }
    sending = false;
    controls();
    rendered = '';
    await loadHistory();
    if (failure) addLine('error', failure);
    input.focus();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
    if (e.key === 'Escape') {
      input.blur();
      stopSpeech();
    }
  });
  function renderVoice() {
    const on = voice.enabled;
    voiceBtn.textContent = 'voice: ' + (on ? (voice.available ? 'on' : 'on (no key)') : 'off');
    voiceBtn.classList.toggle('on', on && voice.available);
    voiceBtn.classList.toggle('warn', on && !voice.available);
    voiceBtn.setAttribute('aria-pressed', String(on));
    voiceBtn.title =
      on && !voice.available
        ? 'Set FISH_AUDIO_API_KEY in .env and restart WOS'
        : 'Speak future replies aloud';
  }
  voiceBtn.addEventListener('click', async () => {
    if (!linked) return;
    voiceBtn.disabled = true;
    try {
      const s = await api('/api/voice', {
        method: 'POST',
        body: JSON.stringify({ enabled: !voice.enabled }),
      });
      onState(s, 0);
      notice();
    } catch (err) {
      notice(err.message);
    } finally {
      controls();
    }
  });
  async function stopSpeech() {
    if (!linked) return;
    try {
      onState(await api('/api/stop', { method: 'POST', body: '{}' }), 0);
      notice();
    } catch (err) {
      notice(err.message);
    }
  }
  listen('btn-stop', 'click', stopSpeech);
  function setExpanded(value, resize = true) {
    if (!value && expanded) {
      chatScrollTop = log.scrollTop;
      followChat = nearBottom();
    }
    expanded = value;
    body.classList.toggle('expanded', value);
    chatPanel.hidden = !value;
    chatBtn.textContent = value ? 'companion' : 'chat';
    chatBtn.setAttribute('aria-expanded', String(value));
    const shell = pyApi();
    if (resize && shell && shell.set_expanded)
      windowAction('set_expanded', [value]);
    if (value) {
      loadModels();
      input.focus();
      log.scrollTop = !openedChat || followChat ? log.scrollHeight : chatScrollTop;
      openedChat = true;
    }
  }
  chatBtn.addEventListener('click', () => setExpanded(!expanded));
  function resetHistory() {
    historySerial++;
    rows.clear();
    historyTotal = 0;
    rendered = '';
    log.textContent = '';
  }
  function onState(s, rtt) {
    const first = !linked;
    if (first) {
      linked = true;
      everLinked = true;
      body.classList.add('linked');
      Sage.setSimulation(false);
    }
    body.classList.remove('offline');
    offlineSince = null;
    const changed =
      (epoch !== null && s.epoch !== epoch) ||
      (s.session_id !== undefined && sessionId !== s.session_id);
    if (changed) {
      if (s.session_id !== undefined && s.session_id !== sessionId) {
        drafts.set(sessionId, input.value);
        input.value = drafts.get(s.session_id) || '';
      }
      if (uncertain) {
        uncertain = null;
        notice('WOS changed sessions. Check the conversation before resending your draft.');
      }
      resetHistory();
      chatVersion = -1;
      haveAudio = null;
      env = null;
      Sage.setAudioLevel(0);
    }
    if (s.epoch !== undefined) epoch = s.epoch;
    if (s.session_id !== undefined) sessionId = s.session_id;
    if ($('chat-title') && s.chat_title) $('chat-title').textContent = s.chat_title;
    if (s.character) {
      if (character !== s.character) rendered = '';
      character = s.character;
      input.placeholder = 'message ' + character.toLowerCase();
      if ($('character-name')) $('character-name').textContent = character;
    }
    const modelChanged = s.model !== undefined && (model !== s.model || provider !== s.provider);
    if (s.model !== undefined) {
      model = s.model;
      provider = s.provider;
    }
    voice = s.voice || voice;
    renderVoice();
    controls();
    const state = s.phase || s.state;
    if (lastState === 'speaking' && state !== 'speaking') Sage.setAudioLevel(0);
    Sage.setState(state);
    Sage.setStatus(STATUS[state] || state);
    lastState = state;
    if (s.audio) {
      if (s.audio.levels) {
        env = { id: s.audio.id, fps: s.audio.fps || 50, levels: s.audio.levels, t0: 0 };
        haveAudio = s.audio.id;
      }
      if (env && env.id === s.audio.id)
        env.t0 = performance.now() - s.audio.elapsed * 1000 - rtt / 2;
    } else {
      env = null;
      haveAudio = null;
    }
    if (s.voice_error && s.voice_error !== lastVoiceError)
      notice('Voice unavailable: ' + s.voice_error);
    lastVoiceError = s.voice_error || null;
    if (s.chat_version !== chatVersion) {
      chatVersion = s.chat_version;
      if (!sending) loadHistory();
    }
    if (first) {
      loadPreferences();
      loadProviders();
      if (expanded) loadModels();
    } else if (modelChanged) {
      if ($('provider-select')) $('provider-select').value = provider;
      if (expanded) loadModels();
    }
  }
  function onOffline() {
    if (offlineSince === null) offlineSince = performance.now();
    linked = false;
    controls();
    body.classList.add('offline');
    body.classList.remove('linked');
    env = null;
    haveAudio = null;
    Sage.setAudioLevel(0);
    Sage.setState('idle');
    lastState = 'idle';
    Sage.setStatus(everLinked ? 'backend offline' : 'waiting for WOS');
    notice('Waiting for the WOS terminal to reconnect.');
    // Keep the window/draft open during slow restarts instead of destroying it.
  }
  async function poll() {
    const start = performance.now();
    let s;
    try {
      s = await api('/api/state' + (haveAudio !== null ? '?have=' + haveAudio : ''));
    } catch (_) {
      onOffline();
      // Recovery is independent of HTTP polling even if the native bridge hangs.
      refreshEndpoint();
    }
    try {
      if (s) {
        if (!linked) notice();
        onState(s, performance.now() - start);
      }
    } catch (_) {
      notice('Could not update the Companion. Retrying…');
    } finally {
      setTimeout(poll, offlineSince === null ? 150 : 1000);
    }
  }
  function drive(now) {
    requestAnimationFrame(drive);
    if (env && lastState === 'speaking') {
      const i = Math.floor(((now - env.t0) / 1000) * env.fps);
      Sage.setAudioLevel(i >= 0 && i < env.levels.length ? env.levels[i] : 0);
    }
  }
  const providerLabels = { cloud: 'Gemini', ollama: 'Ollama', openai: 'OpenAI',
    anthropic: 'Claude', openrouter: 'OpenRouter', deepseek: 'DeepSeek', xai: 'Grok' };
  const chosenProvider = () => ($('provider-select') && $('provider-select').value) || provider;
  async function loadProviders() {
    if (!$('provider-select')) return;
    try {
      const data = await api('/api/providers');
      const select = $('provider-select');
      select.textContent = '';
      data.providers.forEach((item) => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label + (item.configured ? '' : ' · no key');
        select.appendChild(option);
      });
      select.value = provider;
      if (expanded) loadModels();
    } catch (err) { notice(err.message); }
  }
  listen('provider-select', 'change', () => {
    loadModels();
    if (chosenProvider() !== provider) notice('Choose a model to switch providers.');
    else notice();
  });
  async function loadModels(refresh = false) {
    if (!linked || !$('model-select')) return;
    const requestedProvider = chosenProvider();
    const serial = ++modelSerial;
    modelLoading = true;
    const select = $('model-select');
    select.textContent = '';
    const loading = document.createElement('option');
    loading.value = '';
    loading.textContent = 'Loading models…';
    select.appendChild(loading);
    controls();
    try {
      const data = await api('/api/models?provider=' + encodeURIComponent(requestedProvider) +
        (refresh ? '&refresh=1' : ''), {}, 65000);
      if (serial !== modelSerial || data.provider !== chosenProvider()) return;
      select.textContent = '';
      const choices = data.models.slice();
      if (requestedProvider === provider && model && !choices.some((item) => item.id === model))
        choices.unshift({ id: model, label: model + ' (current)' });
      if (requestedProvider !== provider || !choices.length) {
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = choices.length ? 'Choose a model to switch' : 'No models found · see below';
        select.appendChild(placeholder);
      }
      choices.forEach((item) => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label === item.id ? item.id : item.label + ' · ' + item.id;
        select.appendChild(option);
      });
      select.value = requestedProvider === provider ? model : '';
      $('provider-label').textContent = (providerLabels[requestedProvider] || requestedProvider).toUpperCase();
      $('model-source').textContent = data.source === 'live' ? 'From provider' :
        data.source === 'cached' ? 'Cached models' : 'Saved models';
      $('model-warning').textContent = data.warning || '';
      $('model-warning').hidden = !data.warning;
    } catch (err) {
      notice(err.message);
    } finally {
      if (serial === modelSerial) {
        modelLoading = false;
        controls();
      }
    }
  }
  async function changeModel(value) {
    if (!linked || !value || sending || switchingModel || modelLoading) return;
    const selected = chosenProvider();
    switchingModel = true;
    controls();
    try {
      const changing = selected !== provider;
      const s = await api(changing ? '/api/provider' : '/api/model', {
        method: 'POST',
        body: JSON.stringify(changing ? { provider: selected, model: value,
          expected_provider: provider, session_id: sessionId } :
          { model: value, provider, session_id: sessionId }),
      });
      onState(s, 0);
      notice();
    } catch (err) {
      notice(err.message);
      if ($('model-select')) $('model-select').value = selected === provider ? model : '';
    } finally {
      switchingModel = false;
      controls();
    }
  }
  listen('model-select', 'change', () => changeModel($('model-select').value));
  listen('btn-model-refresh', 'click', () => loadModels(true));
  listen('btn-manual-model', 'click', () => {
    $('manual-model-form').hidden = !$('manual-model-form').hidden;
    $('manual-model').focus();
  });
  listen('manual-model-form', 'submit', (e) => {
    e.preventDefault();
    changeModel($('manual-model').value.trim());
  });
  function applyPreferences(values) {
    prefs = { ...prefs, ...values };
    body.dataset.theme = prefs.theme;
    document.documentElement.style.setProperty('--accent', prefs.accent);
    const c = prefs.accent.slice(1);
    document.documentElement.style.setProperty(
      '--accent-rgb',
      [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16)).join(','),
    );
    if (Sage.setAppearance) Sage.setAppearance(prefs);
    Object.entries(prefs).forEach(([key, value]) => {
      if ($('pref-' + key)) $('pref-' + key).value = value;
    });
  }
  async function loadPreferences() {
    if (!$('appearance')) return;
    try {
      applyPreferences(await api('/api/preferences'));
    } catch (_) {}
  }
  async function savePreferences() {
    if (savingPreferences) {
      saveAgain = true;
      return;
    }
    savingPreferences = true;
    const version = preferenceVersion;
    try {
      const saved = await api('/api/preferences', { method: 'POST', body: JSON.stringify(prefs) });
      if (version === preferenceVersion) applyPreferences(saved);
    } catch (err) {
      notice(err.message);
    } finally {
      savingPreferences = false;
      if (saveAgain) {
        saveAgain = false;
        savePreferences();
      }
    }
  }
  Object.keys(defaults).forEach((key) =>
    listen('pref-' + key, 'input', () => {
      const raw = $('pref-' + key).value;
      preferenceVersion++;
      applyPreferences({ [key]: ['size', 'glow', 'motion'].includes(key) ? +raw : raw });
      clearTimeout(prefSave);
      prefSave = setTimeout(savePreferences, 350);
    }),
  );
  function settings(open) {
    $('appearance').hidden = !open;
    $('btn-settings').setAttribute('aria-expanded', String(open));
  }
  listen('btn-settings', 'click', () => settings($('appearance').hidden));
  listen('btn-settings-done', 'click', () => settings(false));
  listen('btn-reset-appearance', 'click', () => {
    preferenceVersion++;
    clearTimeout(prefSave);
    applyPreferences(defaults);
    savePreferences();
  });
  listen('btn-close', 'click', async () => {
    const shell = pyApi();
    if (shell && shell.close) await windowAction('close');
    else {
      await stopSpeech();
      window.close();
    }
  });
  listen('btn-minimize', 'click', () => {
    const shell = pyApi();
    if (shell && shell.minimize) windowAction('minimize');
  });
  function renderPin(state) {
    const pin = $('btn-pin');
    if (!pin || !state || typeof state.on_top !== 'boolean') return;
    pin.setAttribute('aria-pressed', String(state.on_top));
    pin.title = state.on_top ? 'Unpin: allow other windows in front' : 'Pin: keep WOS above other windows';
    pin.setAttribute('aria-label', pin.title);
  }
  listen('btn-pin', 'click', async () => {
    const pin = $('btn-pin');
    if (pin.disabled) return;
    pin.disabled = true;
    try {
      renderPin(await windowAction('set_on_top', [pin.getAttribute('aria-pressed') !== 'true']));
    } finally {
      pin.disabled = false;
    }
  });
  async function shellReady() {
    const shell = pyApi();
    if (!shell) return;
    body.classList.add('desktop');
    if ($('btn-pin')) $('btn-pin').hidden = false;
    if ($('btn-minimize')) $('btn-minimize').hidden = false;
    if (shell.get_window_state) {
      const state = await windowAction('get_window_state');
      if (state) {
        setExpanded(state.expanded, false);
        renderPin(state);
      }
    }
  }
  window.addEventListener('pywebviewready', () => shellReady().catch(() => notice('Window controls are unavailable.')));
  if (pyApi()) shellReady().catch(() => notice('Window controls are unavailable.'));
  window.addEventListener('pagehide', () => {
    if (!pyApi() && linked)
      fetch('/api/stop', {
        method: 'POST',
        headers: { 'X-WOS-Token': token, 'Content-Type': 'application/json' },
        body: '{}',
        keepalive: true,
      }).catch(() => {});
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.target !== input) {
      if ($('appearance') && !$('appearance').hidden) settings(false);
      else stopSpeech();
    }
  });
  const handle = $('resize-handle');
  if (handle) {
    let drag = null,
      busy = false,
      next = null;
    async function resizeWindow() {
      if (busy || !next) return;
      const shell = pyApi();
      if (!shell || !shell.resize) return;
      const size = next;
      next = null;
      busy = true;
      try {
        await windowAction('resize', size);
      } finally {
        busy = false;
        if (next) resizeWindow();
      }
    }
    handle.addEventListener('pointerdown', (e) => {
      if (!pyApi()) return;
      e.preventDefault();
      drag = { x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight };
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener('pointermove', (e) => {
      if (!drag) return;
      next = [Math.round(drag.w + e.screenX - drag.x), Math.round(drag.h + e.screenY - drag.y)];
      resizeWindow();
    });
    handle.addEventListener('pointercancel', () => {
      drag = null;
    });
    handle.addEventListener('pointerup', () => {
      drag = null;
    });
    handle.addEventListener('lostpointercapture', () => {
      drag = null;
    });
  }
  renderVoice();
  controls();
  Sage.setStatus('waiting for WOS');
  requestAnimationFrame(drive);
  poll();
})();
