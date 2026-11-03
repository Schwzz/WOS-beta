/* Shared Auralis session controls. Credentials stay in the Python process.
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
    voiceBtn = $('btn-voice');
  const caption = $('line-c');
  const STATUS = {
    idle: 'standing by',
    thinking: 'analysing',
    preparing: 'preparing voice',
    speaking: 'speaking',
    listening: 'listening',
  };
  const defaults = {
    preset: 'lattice',
    accent: '#65dfcc',
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
    expanded = true;
  let desktopBusy = false,
    chatTitle = 'New chat',
    historyNeeded = true,
    historyInFlight = 0,
    historyRetryAt = 0,
    stateGeneration = 0;
  const subscribers = new Set();
  let characterId = null, character = 'Nexis',
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
  const drafts = new Map();
  let backendBusy = false;
  let speech = { phase: 'idle', level: 0, elapsed: 0, error: null, result: null,
    shortcut: 'f7', model: 'whisper-large-v3-turbo', provider: 'groq', configured: false };
  let speechPending = 0, speechQueue = Promise.resolve(), suppressSpeechResult = false;
  const speechResults = new Set(), speechAcks = new Set();
  const speechActive = () => speech.phase === 'recording' || speech.phase === 'transcribing';
  const speechBusy = () => speechActive() || speechPending > 0;
  const busy = () => desktopBusy || backendBusy || speechBusy();
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
  function snapshot() {
    return { linked, sending, switchingModel, busy: busy(), desktopBusy, backendBusy, speechBusy: speechBusy(),
      speech: { ...speech }, sessionId, characterId, character, provider, model, chatTitle };
  }
  function publish() {
    subscribers.forEach((fn) => { try { fn(snapshot()); } catch (_) {} });
  }
  function controls() {
    const blocked = busy();
    sendBtn.disabled = !linked || sending || switchingModel || blocked || chosenProvider() !== provider;
    voiceBtn.disabled = !linked || blocked;
    if ($('btn-stop')) $('btn-stop').disabled = !linked;
    if ($('model-select')) $('model-select').disabled = !linked || sending || blocked || modelLoading || switchingModel;
    if ($('provider-select')) $('provider-select').disabled = !linked || sending || blocked || switchingModel;
    if ($('manual-model')) $('manual-model').disabled = !linked || sending || blocked || switchingModel;
    if ($('btn-manual-model')) $('btn-manual-model').disabled = !linked || sending || blocked || switchingModel;
    if ($('btn-model-refresh')) $('btn-model-refresh').disabled = !linked || blocked || modelLoading || switchingModel;
    if ($('btn-mic')) $('btn-mic').disabled = !linked || sending || switchingModel || desktopBusy || (backendBusy && speech.phase !== 'recording') ||
      speechPending > 0 || speech.phase === 'transcribing';
    if ($('btn-speech-cancel')) $('btn-speech-cancel').disabled = !linked;
    if ($('speech-save')) $('speech-save').disabled = !linked || sending || switchingModel || blocked;
    if ($('speech-model')) $('speech-model').disabled = blocked;
    if ($('speech-shortcut')) $('speech-shortcut').disabled = blocked;
    renderSpeech();
    publish();
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
        const err = new Error((data && data.error) || `Auralis returned HTTP ${res.status}.`);
        err.status = res.status;
        throw err;
      }
      return data;
    } catch (err) {
      if (err.status) throw err;
      const failure = new Error(
        err.name === 'AbortError'
          ? 'Auralis took too long to respond. Check the connection and try again.'
          : 'Connection to Auralis was lost. Reconnecting…',
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
    if ($('empty-chat')) $('empty-chat').hidden = true;
    return row;
  }
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  async function loadHistory(before = null) {
    if (before === null && historyInFlight) return;
    const serial = ++historySerial,
      expected = sessionId, expectedVersion = chatVersion;
    historyInFlight = serial;
    try {
      const h = await api('/api/history' + (before === null ? '' : '?before=' + before));
      if (
        serial !== historySerial ||
        expected !== sessionId ||
        (h.session_id && h.session_id !== sessionId)
      )
        return;
      if (h.character) character = h.character;
      historyNeeded = expectedVersion !== chatVersion;
      historyRetryAt = 0;
      if ($('notice') && $('notice').textContent === 'Could not load the conversation. Retrying…') notice();
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
      if ($('empty-chat')) $('empty-chat').hidden = messages.length !== 0;
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
      if (serial === historySerial && expected === sessionId) {
        historyNeeded = true;
        historyRetryAt = performance.now() + 1000;
        notice('Could not load the conversation. Retrying…');
      }
    } finally {
      if (historyInFlight === serial) historyInFlight = 0;
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
    if (!text || sending || switchingModel || busy() || !linked || chosenProvider() !== provider) return;
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
      if (submittedSession === sessionId) {
        if (!input.value) input.value = text;
      } else if (!drafts.get(submittedSession)) drafts.set(submittedSession, text);
    }
    sending = false;
    controls();
    if (submittedSession === sessionId) {
      rendered = '';
      await loadHistory();
      if (failure) addLine('error', failure);
      input.focus();
    } else if (failure) notice('The message in the previous chat failed. Its draft is saved.');
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
    if (e.key === 'Escape') {
      if (speechBusy()) { e.preventDefault(); cancelSpeech(); }
      else { input.blur(); stopSpeech(); }
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
        ? 'Add a Fish Audio key in API keys to enable voice'
        : 'Speak future replies aloud';
  }
  voiceBtn.addEventListener('click', async () => {
    if (!linked || busy()) return;
    const expected = sessionId;
    voiceBtn.disabled = true;
    try {
      const s = await api('/api/voice', {
        method: 'POST',
        body: JSON.stringify({ enabled: !voice.enabled }),
      });
      acceptState(s, expected);
      notice();
    } catch (err) {
      notice(err.message);
    } finally {
      controls();
    }
  });
  function renderSpeech() {
    const recording = speech.phase === 'recording', active = speechBusy();
    const shortcut = (speech.shortcut || 'f7').toUpperCase();
    if ($('btn-mic')) {
      $('btn-mic').setAttribute('aria-pressed', String(recording));
      $('btn-mic').setAttribute('aria-label', recording ? 'Finish recording' : 'Record a message');
      $('btn-mic').title = (recording ? 'Finish recording' : 'Record a message') + ' · ' + shortcut;
      $('btn-mic').classList.toggle('recording', recording);
    }
    if ($('speech-status')) {
      $('speech-status').textContent = speech.phase === 'recording'
        ? 'Recording · ' + Math.min(60, Math.floor(speech.elapsed || 0)) + 's · press ' + shortcut + ' again to finish'
        : speech.phase === 'transcribing' ? 'Transcribing… You can keep editing your draft.'
        : speechPending ? 'Updating microphone…'
        : speech.error || '';
      $('speech-status').classList.toggle('error', speech.phase === 'error');
    }
    if ($('speech-feedback')) $('speech-feedback').hidden = !active && !speech.error;
    if ($('btn-speech-cancel')) $('btn-speech-cancel').hidden = !active;
    if ($('speech-meter')) {
      $('speech-meter').value = recording ? Math.max(0, Math.min(1, speech.level || 0)) : 0;
      $('speech-meter').hidden = !recording;
    }
    if ($('speech-configured')) $('speech-configured').textContent = speech.configured
      ? 'Groq key configured. Your recording is sent to Groq when you finish.'
      : 'Add a Groq key in API keys before recording.';
  }
  function avatarState() {
    if (speech.phase === 'recording' || speech.phase === 'transcribing') {
      Sage.setState('listening');
      Sage.setStatus(speech.phase === 'recording' ? 'listening' : 'transcribing');
      Sage.setAudioLevel(speech.phase === 'recording' ? Math.max(0, Math.min(1, speech.level || 0)) : 0);
    } else {
      Sage.setState(lastState);
      Sage.setStatus(STATUS[lastState] || lastState);
    }
  }
  async function ackSpeech(result, key) {
    if (speechAcks.has(key)) return;
    speechAcks.add(key);
    try {
      // A repeated acknowledgement is harmless. Never apply its possibly stale state.
      await api('/api/stt', { method: 'POST', body: JSON.stringify({ action: 'ack', result_id: result.id }) });
    } catch (_) { /* The next poll retries without inserting the transcript again. */ }
    finally { speechAcks.delete(key); }
  }
  function acceptSpeech(next) {
    if (!next) return;
    const previousPhase = speech.phase;
    speech = { ...speech, ...next };
    if (previousPhase === 'recording' && speech.phase !== 'recording') Sage.setAudioLevel(0);
    const result = speech.result;
    if (result && result.id !== undefined) {
      const key = String(epoch) + ':' + result.id + ':' + result.session_id;
      if (!speechResults.has(key)) {
        // Mark before acknowledging, including discarded/canceled/cross-chat results.
        speechResults.add(key);
        if (!suppressSpeechResult && result.session_id === sessionId && typeof result.text === 'string' && result.text.trim()) {
          input.value += (input.value && !/\s$/.test(input.value) ? '\n' : '') + result.text.trim();
          drafts.set(sessionId, input.value);
          notice('Transcript added. Review or edit it, then send when you are ready.');
        }
      }
      ackSpeech(result, key);
    }
    renderSpeech();
  }
  function speechCommand(action, extra = {}) {
    speechPending++;
    stateGeneration++;
    controls();
    const operation = speechQueue.catch(() => {}).then(async () => {
      try {
        const result = await api('/api/stt', { method: 'POST', body: JSON.stringify({ action, ...extra }) });
        stateGeneration++;
        acceptSpeech(result);
        avatarState();
        return true;
      } catch (err) {
        if (action !== 'ack') {
          speech = { ...speech, phase: 'error', level: 0, error: err.message };
          notice(err.message);
          avatarState();
        }
        return false;
      } finally {
        speechPending--;
        stateGeneration++;
        controls();
      }
    });
    speechQueue = operation;
    return operation;
  }
  function toggleSpeech() {
    if (!linked || sending || switchingModel || desktopBusy || (backendBusy && speech.phase !== 'recording') || speechPending || speech.phase === 'transcribing') return;
    if (speech.phase === 'recording') return speechCommand('stop');
    suppressSpeechResult = false;
    return speechCommand('start', { session_id: sessionId });
  }
  function cancelSpeech() {
    if (!linked || !speechBusy()) return;
    suppressSpeechResult = true;
    return speechCommand('cancel');
  }
  listen('btn-mic', 'click', toggleSpeech);
  listen('btn-speech-cancel', 'click', cancelSpeech);
  async function stopSpeech() {
    if (!linked) return;
    suppressSpeechResult = true;
    // Queue behind microphone actions so a delayed start cannot revive capture.
    speechPending++;
    stateGeneration++;
    controls();
    const expected = sessionId;
    const operation = speechQueue.catch(() => {}).then(async () => {
      try {
        acceptState(await api('/api/stop', { method: 'POST', body: '{}' }), expected);
        notice();
      } catch (err) { notice(err.message); }
      finally { speechPending--; stateGeneration++; controls(); }
    });
    speechQueue = operation;
    return operation;
  }
  listen('btn-stop', 'click', stopSpeech);
  function speechSettings(open) {
    if (!$('speech-dialog')) return;
    $('speech-dialog').hidden = !open;
    $('btn-speech-settings').setAttribute('aria-expanded', String(open));
    if (open) {
      $('speech-model').value = speech.model;
      $('speech-shortcut').value = speech.shortcut;
      $('speech-setting-error').hidden = true;
      $('speech-shortcut').focus();
    } else $('btn-speech-settings').focus();
  }
  listen('btn-speech-settings', 'click', () => speechSettings(true));
  listen('speech-done', 'click', () => speechSettings(false));
  listen('speech-form', 'submit', async (e) => {
    e.preventDefault();
    if (!linked || sending || switchingModel || busy()) return;
    const ok = await speechCommand('configure', { settings: { model: $('speech-model').value, shortcut: $('speech-shortcut').value } });
    if (ok) speechSettings(false);
    else { $('speech-setting-error').textContent = 'Could not save speech input settings. Try again.'; $('speech-setting-error').hidden = false; }
  });
  function resetHistory() {
    historySerial++;
    historyInFlight = 0;
    historyNeeded = true;
    historyRetryAt = 0;
    rows.clear();
    historyTotal = 0;
    rendered = '';
    log.textContent = '';
    if ($('empty-chat')) $('empty-chat').hidden = true;
  }
  function acceptState(s, expected = sessionId) {
    if (expected !== sessionId && s.session_id !== sessionId) return;
    stateGeneration++;
    onState(s, 0);
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
        notice('Auralis changed sessions. Check the conversation before resending your draft.');
      }
      resetHistory();
      chatVersion = -1;
      haveAudio = null;
      env = null;
      Sage.setAudioLevel(0);
    }
    if (s.epoch !== undefined) epoch = s.epoch;
    if (s.session_id !== undefined) sessionId = s.session_id;
    if (s.chat_title) chatTitle = s.chat_title;
    if ($('chat-title')) $('chat-title').textContent = chatTitle;
    if (s.character_id) characterId = s.character_id;
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
    backendBusy = !!s.busy;
    if (s.speech && (!speechPending || suppressSpeechResult)) acceptSpeech(s.speech);
    renderVoice();
    controls();
    const state = s.phase || s.state;
    if (lastState === 'speaking' && state !== 'speaking') Sage.setAudioLevel(0);
    lastState = state;
    avatarState();
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
      historyNeeded = true;
      if (!sending) loadHistory();
    }
    if (historyNeeded && !sending && !historyInFlight && performance.now() >= historyRetryAt) loadHistory();
    if (first) {
      loadPreferences();
      loadProviders();
      if (expanded) loadModels();
    } else if (modelChanged) {
      if ($('provider-select')) $('provider-select').value = provider;
      if (expanded) loadModels();
    }
    if ($('connection-status')) $('connection-status').textContent = sending ? 'Replying…' : 'Connected';
    publish();
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
    Sage.setStatus(everLinked ? 'backend offline' : 'waiting for Auralis');
    if ($('connection-status')) $('connection-status').textContent = 'Reconnecting…';
    notice('Connection lost. Reconnecting to Auralis…');
    publish();
    // Keep the window/draft open during slow restarts instead of destroying it.
  }
  async function poll() {
    const start = performance.now(), generation = stateGeneration;
    let s;
    try {
      s = await api('/api/state' + (haveAudio !== null ? '?have=' + haveAudio : ''));
    } catch (_) {
      if (generation === stateGeneration) onOffline();
      // Recovery is independent of HTTP polling even if the native bridge hangs.
      refreshEndpoint();
    }
    try {
      if (s && generation === stateGeneration) {
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
    if (env && lastState === 'speaking' && !speechActive()) {
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
    if (!linked || !value || sending || busy() || switchingModel || modelLoading) return;
    const selected = chosenProvider(), expected = sessionId;
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
      acceptState(s, expected);
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
    prefs = { ...prefs, ...values, preset: 'lattice' };
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
    if (e.defaultPrevented) return;
    if (e.key.toLowerCase() === (speech.shortcut || 'f7') && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
      if (!document.hasFocus || document.hasFocus()) { e.preventDefault(); toggleSpeech(); }
      return;
    }
    if (e.key === 'Escape' && speechBusy()) { e.preventDefault(); cancelSpeech(); return; }
    if (e.key === 'Escape' && $('speech-dialog') && !$('speech-dialog').hidden) {
      e.preventDefault(); speechSettings(false); return;
    }
    if (e.key === 'Escape' && e.target !== input) {
      if (($('credentials-dialog') && !$('credentials-dialog').hidden) ||
          ($('desktop-dialog') && !$('desktop-dialog').hidden)) return;
      if ($('appearance') && !$('appearance').hidden) settings(false);
      else stopSpeech();
    }
  });
  // A deliberately small shared client: the authentication token never leaves this closure.
  window.AuralisClient = Object.freeze({
    request: api,
    state: snapshot,
    applyState: acceptState,
    notice,
    subscribe(fn) { subscribers.add(fn); fn(snapshot()); return () => subscribers.delete(fn); },
    setBusy(value) { desktopBusy = !!value; stateGeneration++; controls(); },
    toggleSpeech, cancelSpeech,
    refreshProviders: loadProviders,
    refreshModels: () => loadModels(true),
    refreshHistory() { historyNeeded = true; return loadHistory(); },
  });
  renderVoice();
  controls();
  Sage.setStatus('waiting for Auralis');
  requestAnimationFrame(drive);
  poll();
})();
