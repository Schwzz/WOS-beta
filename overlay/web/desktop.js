/* Desktop navigation and setup. No credentials are stored in browser storage. */
(() => {
  'use strict';
  const client = window.AuralisClient;
  if (!client) return;
  const $ = (id) => document.getElementById(id),
    body = document.body;
  let state = client.state(), busy = false, initialized = false,
    chats = [], characters = [], credentials = null, listSerial = 0,
    characterSerial = 0, credentialSerial = 0, lastSession = null, lastTitle = '',
    dialog = null, returnFocus = null;
  const listen = (id, event, fn) => $(id) && $(id).addEventListener(event, fn);
  const unavailable = () => !state.linked || state.sending || state.switchingModel || busy;
  const setBusy = (value) => { busy = value; client.setBusy(value); renderControls(); };
  function renderControls() {
    ['btn-new-chat', 'btn-rename-chat', 'btn-clear-chat', 'btn-delete-chat',
      'character-select', 'dialog-confirm'].forEach((id) => { if ($(id)) $(id).disabled = unavailable(); });
    if ($('saved-chats')) Array.from($('saved-chats').children).forEach((el) => { el.disabled = unavailable(); });
    if ($('btn-save-credential')) $('btn-save-credential').disabled = !state.linked || busy || !credentials;
    if ($('credential-provider')) $('credential-provider').disabled = busy || !credentials;
    if ($('credential-key')) $('credential-key').disabled = busy;
    if ($('chat-count')) $('chat-count').textContent = chats.length ? String(chats.length) : '';
  }
  function closeActions() {
    $('chat-actions').hidden = true;
    $('btn-chat-actions').setAttribute('aria-expanded', 'false');
  }
  function renderChats() {
    const list = $('saved-chats');
    list.textContent = '';
    chats.forEach((chat) => {
      const id = chat.session_id || chat.id;
      if (!id) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'saved-chat';
      button.setAttribute('aria-current', String(id === state.sessionId));
      const title = document.createElement('strong'), detail = document.createElement('small');
      title.textContent = chat.title || 'New chat';
      const persona = characters.find((item) => item.id === chat.character_id);
      detail.textContent = persona ? persona.name : (chat.model || 'Saved conversation');
      button.append(title, detail);
      button.addEventListener('click', () => {
        if (id !== state.sessionId) mutateChat('switch', id);
        if (window.innerWidth <= 540) toggleSidebar(false);
      });
      list.appendChild(button);
    });
    $('sidebar-message').textContent = chats.length ? '' : 'Your conversations will appear here.';
    renderControls();
  }
  async function loadChats() {
    const serial = ++listSerial;
    try {
      const result = await client.request('/api/chats');
      if (serial !== listSerial) return;
      chats = result.chats || [];
      renderChats();
    } catch (error) {
      if (serial === listSerial) $('sidebar-message').textContent = 'Could not load saved chats. Reconnect or try New chat.';
    }
  }
  function renderCharacters(current) {
    const select = $('character-select');
    select.textContent = '';
    characters.forEach((character) => {
      const option = document.createElement('option');
      option.value = character.id;
      option.textContent = character.name;
      select.appendChild(option);
    });
    const active = characters.find((item) => item.id === state.characterId);
    select.value = active ? active.id : (current || '');
    renderControls();
  }
  async function loadCharacters() {
    const serial = ++characterSerial, session = state.sessionId;
    try {
      const result = await client.request('/api/characters');
      if (serial !== characterSerial || session !== state.sessionId) return;
      characters = result.characters || [];
      renderCharacters(result.current);
      renderChats();
    } catch (error) { if (serial === characterSerial) client.notice('Could not load companions. Try reconnecting.'); }
  }
  function acceptResult(result, expected) {
    // A response from an older chat must never replace a newer selection.
    if (state.sessionId !== expected && state.sessionId !== result.session_id) return false;
    client.applyState(result);
    return true;
  }
  async function mutateChat(action, target = state.sessionId, extra = {}) {
    if (unavailable()) return false;
    const expected = state.sessionId;
    setBusy(true);
    closeActions();
    try {
      const result = await client.request('/api/chats', {
        method: 'POST', body: JSON.stringify({ action, session_id: target, expected_session_id: expected, ...extra }),
      });
      if (acceptResult(result, expected)) client.notice();
      await loadChats();
      return true;
    } catch (error) { client.notice(error.message); return false; }
    finally { setBusy(false); }
  }
  listen('btn-new-chat', 'click', () => mutateChat('new'));
  function toggleSidebar(open) {
    const expanded = open === undefined ? body.classList.contains('sidebar-collapsed') : open;
    body.classList.toggle('sidebar-collapsed', !expanded);
    $('btn-sidebar').setAttribute('aria-expanded', String(expanded));
  }
  toggleSidebar(window.innerWidth > 760);
  listen('btn-sidebar', 'click', () => toggleSidebar());
  listen('btn-avatar', 'click', () => {
    const open = $('avatar-panel').hidden;
    $('avatar-panel').hidden = !open;
    $('btn-avatar').setAttribute('aria-expanded', String(open));
    // The unchanged renderer observes canvas layout through ResizeObserver.
  });
  listen('btn-chat-actions', 'click', () => {
    const open = $('chat-actions').hidden;
    $('chat-actions').hidden = !open;
    $('btn-chat-actions').setAttribute('aria-expanded', String(open));
  });
  function openDialog(action) {
    if (unavailable()) return;
    closeActions();
    dialog = { action, session: state.sessionId };
    returnFocus = $('btn-chat-actions');
    const rename = action === 'rename';
    $('dialog-title').textContent = rename ? 'Rename chat' : action === 'delete' ? 'Delete this chat?' : 'Clear this conversation?';
    $('dialog-description').textContent = rename ? 'Give this conversation a name you can find later.' :
      action === 'delete' ? 'This saved conversation will be permanently deleted.' : 'All messages in this chat will be permanently removed. The chat will stay in your list.';
    $('dialog-field').hidden = !rename;
    $('dialog-input').value = rename ? state.chatTitle : '';
    $('dialog-input').required = rename;
    $('dialog-confirm').textContent = rename ? 'Save name' : action === 'delete' ? 'Delete chat' : 'Clear messages';
    $('dialog-error').hidden = true;
    $('desktop-dialog').hidden = false;
    (rename ? $('dialog-input') : $('dialog-cancel')).focus();
  }
  function closeDialog() {
    if (busy) return;
    $('desktop-dialog').hidden = true;
    dialog = null;
    if (returnFocus) returnFocus.focus();
  }
  ['rename', 'delete', 'clear'].forEach((action) => listen('btn-' + action + '-chat', 'click', () => openDialog(action)));
  listen('dialog-cancel', 'click', closeDialog);
  listen('dialog-form', 'submit', async (event) => {
    event.preventDefault();
    if (!dialog || unavailable()) return;
    const pending = dialog;
    if (pending.session !== state.sessionId) {
      closeDialog(); client.notice('The active chat changed. Open its actions again.'); return;
    }
    const title = $('dialog-input').value.trim();
    if (pending.action === 'rename' && !title) { $('dialog-error').textContent = 'Enter a chat name.'; $('dialog-error').hidden = false; return; }
    const success = await mutateChat(pending.action, pending.session, pending.action === 'rename' ? { title } : { confirm: true });
    if (success) closeDialog();
    else { $('dialog-error').textContent = 'Could not update this chat. Your conversation and draft are still available.'; $('dialog-error').hidden = false; }
  });
  listen('character-select', 'change', async () => {
    if (unavailable()) { renderCharacters(); return; }
    const expected = state.sessionId, character = $('character-select').value;
    setBusy(true);
    try {
      const result = await client.request('/api/character', { method: 'POST', body: JSON.stringify({ character, session_id: expected }) });
      if (acceptResult(result, expected)) client.notice();
      await loadChats();
    } catch (error) { client.notice(error.message); renderCharacters(); }
    finally { setBusy(false); }
  });
  function renderCredentialStatus() {
    if (!credentials) return;
    const provider = $('credential-provider').value;
    const item = provider === 'fish_audio' ? credentials.voice : credentials.providers.find((item) => item.id === provider);
    $('credential-status').textContent = item && item.configured ? 'A key is configured. Save a new key to replace it.' : 'No key configured yet.';
  }
  function renderCredentials() {
    const select = $('credential-provider'), previous = select.value;
    select.textContent = '';
    (credentials.providers || []).forEach((provider) => {
      // Ollama runs locally and does not need an API key.
      if (!provider.key_env) return;
      const option = document.createElement('option');
      option.value = provider.id; option.textContent = provider.label + (provider.configured ? ' · configured' : '');
      select.appendChild(option);
    });
    const voice = document.createElement('option');
    voice.value = 'fish_audio'; voice.textContent = 'Fish Audio · voice' + (credentials.voice && credentials.voice.configured ? ' · configured' : '');
    select.appendChild(voice);
    const values = Array.from(select.children).map((option) => option.value);
    select.value = values.includes(previous) ? previous : values.includes(state.provider) ? state.provider : values[0];
    renderCredentialStatus(); renderControls();
  }
  async function loadCredentials() {
    const serial = ++credentialSerial;
    try {
      const result = await client.request('/api/credentials');
      if (serial !== credentialSerial) return;
      credentials = result; renderCredentials();
    } catch (error) { if (serial === credentialSerial) { $('credential-error').textContent = 'Could not load provider setup. Close this panel and try again.'; $('credential-error').hidden = false; } }
  }
  listen('btn-credentials', 'click', () => {
    returnFocus = $('btn-credentials');
    $('credentials-dialog').hidden = false;
    $('credential-error').hidden = true;
    $('credential-key').value = '';
    loadCredentials();
    $('credential-key').focus();
  });
  function closeCredentials() {
    if (busy) return;
    credentialSerial++;
    $('credential-key').value = '';
    $('credentials-dialog').hidden = true;
    if (returnFocus) returnFocus.focus();
  }
  listen('btn-credentials-done', 'click', closeCredentials);
  listen('credential-provider', 'change', () => { $('credential-key').value = ''; $('credential-error').hidden = true; renderCredentialStatus(); });
  listen('credentials-form', 'submit', async (event) => {
    event.preventDefault();
    if (!state.linked || busy || !credentials) return;
    const key = $('credential-key').value.trim(), provider = $('credential-provider').value;
    if (!key || !provider) return;
    $('credential-key').value = '';
    $('credential-error').hidden = true;
    setBusy(true);
    try {
      credentials = await client.request('/api/credentials', { method: 'POST', body: JSON.stringify({ provider, key }) });
      renderCredentials();
      $('credential-status').textContent = 'Key saved. Provider models will refresh automatically.';
      client.notice('API key saved. No message has been sent.');
      await client.refreshProviders();
      await client.refreshModels();
    } catch (_) {
      // Never reflect a credential-bearing request or backend diagnostic into UI/logs.
      $('credential-error').textContent = 'Could not save this key. Check the key and connection, then try again.';
      $('credential-error').hidden = false;
    } finally { setBusy(false); }
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!$('credentials-dialog').hidden) { event.preventDefault(); closeCredentials(); }
      else if (!$('desktop-dialog').hidden) { event.preventDefault(); closeDialog(); }
      else closeActions();
    }
    if (event.key === 'Tab') {
      const panel = !$('credentials-dialog').hidden ? $('credentials-dialog') : !$('desktop-dialog').hidden ? $('desktop-dialog') : null;
      if (!panel || !panel.querySelectorAll) return;
      const focusable = Array.from(panel.querySelectorAll('button, input, select')).filter((el) => !el.disabled && !el.hidden && el.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  client.subscribe((next) => {
    state = next;
    renderControls();
    if (!state.linked) initialized = false;
    if (state.linked && (!initialized || state.sessionId !== lastSession || state.chatTitle !== lastTitle)) {
      initialized = true; lastSession = state.sessionId; lastTitle = state.chatTitle;
      loadChats(); loadCharacters();
    }
    const active = characters.find((item) => item.id === state.characterId);
    if (active) $('character-select').value = active.id;
  });
})();
