"""Auralis's full-screen terminal view. Session and commands stay in the existing runtime."""
import asyncio
from collections import deque
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Input, Markdown, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from core.cli import KNOWN_COMMANDS, check_unrecognized_command
from core.help_text import HELP_TOPICS
from core.terminal_bridge import TerminalBridge, Output, Ready, Finished, Prompt


# Built from the existing help registry, so completion cannot invent commands.
def command_choices(text='/'):
    value = text.lstrip().lower()
    if not value.startswith('/') or '\n' in value:
        return []
    entries = {}
    for name in KNOWN_COMMANDS:
        topic = HELP_TOPICS.get(name, {})
        entries[name] = topic.get('summary', 'Auralis command')
        for _, usage, description in topic.get('subcommands', []):
            words = []
            for word in usage.split():
                if word.startswith(('<', '[')):
                    break
                words.append(word)
            if words:
                entries[' '.join(words)] = description
    preferred = ['/help', '/chat', '/model', '/provider', '/character switch', '/new', '/status', '/stop']
    ranked = preferred + sorted(set(entries) - set(preferred))
    return [(name, entries[name]) for name in ranked if name in entries and name.startswith(value)]


class BridgeEvent(Message):
    def __init__(self, value):
        super().__init__()
        self.value = value


class VoiceChanged(Message):
    pass


class Composer(TextArea):
    BINDINGS = [
        Binding('up', 'context_up', show=False),
        Binding('down', 'context_down', show=False),
        Binding('tab', 'context_complete', show=False),
    ]

    def action_context_up(self):
        if self.app.suggestions.display:
            self.post_message(self.Navigate('up'))
        else:
            self.action_cursor_up()

    def action_context_down(self):
        if self.app.suggestions.display:
            self.post_message(self.Navigate('down'))
        else:
            self.action_cursor_down()

    def action_context_complete(self):
        if self.app.suggestions.display:
            self.post_message(self.Complete())
        else:
            self.screen.focus_next()

    class Submit(Message):
        pass

    class Navigate(Message):
        def __init__(self, direction):
            super().__init__()
            self.direction = direction

    class Complete(Message):
        pass

    class Escape(Message):
        pass

    async def _on_key(self, event: events.Key):
        if event.key in ('enter', 'ctrl+enter', 'shift+enter', 'ctrl+j'):
            event.stop()
            event.prevent_default()
            if event.key in ('shift+enter', 'ctrl+j'):
                self.replace('\n', *self.selection, maintain_selection_offset=False)
            else:
                self.post_message(self.Submit())
            return
        if event.key == 'escape':
            event.stop()
            event.prevent_default()
            self.post_message(self.Escape())
            return
        await super()._on_key(event)


class ChoiceScreen(ModalScreen):
    """Searchable keyboard/mouse picker. Returns an index into the original list."""
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, title, options, allow_cancel=True):
        super().__init__()
        self.title_text = title
        self.choices = list(options)
        self.allow_cancel = allow_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Static(self.title_text, classes='dialog-title', markup=False)
            yield Input(placeholder='Type to filter…', id='picker-search')
            yield OptionList(id='picker-options', markup=False)
            yield Static('↑ ↓ select   Enter confirm   Esc cancel', classes='dialog-hint')

    def on_mount(self):
        self.filter('')
        self.query_one(Input).focus()

    def filter(self, value):
        words = value.casefold().split()
        found = [(i, text) for i, text in enumerate(self.choices)
                 if all(word in text.casefold() for word in words)]
        widget = self.query_one('#picker-options', OptionList)
        widget.clear_options()
        widget.add_options([Option(Text(text), id=str(i)) for i, text in found])
        widget.highlighted = 0 if found else None

    @on(Input.Changed, '#picker-search')
    def filtered(self, event):
        self.filter(event.value)

    @on(Input.Submitted, '#picker-search')
    def confirm(self):
        widget = self.query_one('#picker-options', OptionList)
        if widget.highlighted is not None:
            self.dismiss(int(widget.get_option_at_index(widget.highlighted).id))

    @on(OptionList.OptionSelected, '#picker-options')
    def selected(self, event):
        self.dismiss(int(event.option.id))

    def on_key(self, event):
        if event.key in ('up', 'down') and self.query_one(Input).has_focus:
            widget = self.query_one(OptionList)
            getattr(widget, 'action_cursor_' + event.key)()
            event.stop()
            event.prevent_default()

    def action_cancel(self):
        if self.allow_cancel:
            self.dismiss(None)


class PromptScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt
        self.confirmation = '(y/n)' in prompt.casefold()

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Static(self.prompt, classes='dialog-title', markup=False)
            if self.confirmation:
                with Horizontal(classes='dialog-buttons'):
                    yield Button('Cancel', id='prompt-cancel')
                    yield Button('Yes, continue', id='prompt-confirm', variant='error')
            else:
                yield Input(placeholder='Enter an answer…', id='prompt-answer')
                yield Static('Enter confirm   Esc cancel', classes='dialog-hint')

    def on_mount(self):
        self.query_one('#prompt-cancel' if self.confirmation else '#prompt-answer').focus()

    @on(Input.Submitted)
    def submitted(self, event):
        self.dismiss(event.value)

    @on(Button.Pressed)
    def button(self, event):
        self.dismiss('y' if event.button.id == 'prompt-confirm' else None)

    def action_cancel(self):
        self.dismiss(None)


class DetailsScreen(ModalScreen):
    """Command journal kept out of the conversation; opening it preserves drafts."""
    BINDINGS = [Binding('escape,f8', 'cancel', 'Back')]

    def __init__(self, lines):
        super().__init__()
        self.lines = list(lines)

    def compose(self) -> ComposeResult:
        with Vertical(id='details-dialog'):
            yield Static('Activity details', classes='dialog-title')
            yield RichLog(id='details-log', wrap=True, markup=False, max_lines=500, min_width=1)
            yield Button('Back to chat', id='details-close')
            yield Static('Esc / F8 back · latest 500 lines · kept until you quit', classes='dialog-hint')

    def on_mount(self):
        self.query_one(RichLog).write(Text('\n'.join(self.lines) or 'No activity yet.'))
        self.query_one(Button).focus()

    @on(Button.Pressed, '#details-close')
    def close_details(self):
        self.action_cancel()

    def action_cancel(self):
        self.dismiss(None)


class WosTerminal(App):
    TITLE = 'Auralis'
    CSS_PATH = 'terminal_ui.tcss'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding('ctrl+p', 'commands', 'Commands', priority=True),
        Binding('ctrl+n', 'new_chat', 'New chat', show=False, priority=True),
        Binding('ctrl+o', 'chats', 'Chats', show=False, priority=True),
        Binding('f2', 'models', 'Models', show=False, priority=True),
        Binding('f3', 'providers', 'Providers', show=False, priority=True),
        Binding('f4', 'characters', 'Characters', show=False, priority=True),
        Binding('f6', 'voice', 'Voice', show=False, priority=True),
        Binding('ctrl+b', 'sidebar', 'Sidebar', priority=True),
        Binding('ctrl+t', 'theme', 'Theme', show=False, priority=True),
        Binding('ctrl+l', 'dismiss_output', 'Dismiss notice', show=False, priority=True),
        Binding('f8', 'details', 'Details', show=False, priority=True),
        Binding('pageup', 'history_up', 'Scroll up', show=False, priority=True),
        Binding('pagedown', 'history_down', 'Scroll down', show=False, priority=True),
        Binding('ctrl+c', 'interrupt', 'Stop', show=False, priority=True),
        Binding('ctrl+q', 'quit', 'Quit', priority=True),
    ]

    def __init__(self, namespace, *, startup=True):
        super().__init__()
        self.namespace = namespace
        self.backend = namespace['overlay_backend']
        self.bridge = TerminalBridge(namespace, lambda value: self.post_message(BridgeEvent(value)))
        self.startup = startup
        self.busy = True
        self.session_id = None
        self.revision = None
        self.rows = {}
        self.total = 0
        self.signature = None
        self._history_nodes = {}
        self._rendered_session = None
        self._history_lock = asyncio.Lock()
        self.pending = None
        self._submission = None
        self.drafts = {}
        self._slash_choices = []
        self._dismissed_slash = None
        self._refreshing = False
        self._chat_signature = None
        self._terminal_closing = False
        self._sidebar_visible = False
        self.output_lines = deque(maxlen=500)
        self._notice_timer = None
        self._voice_toggle_busy = False
        self.register_theme(Theme(name='auralis-dark', primary='#8bd5c7', secondary='#dfbb81',
            background='#111b20', surface='#19282d', panel='#24373c', foreground='#e1ece9'))
        self.register_theme(Theme(name='auralis-light', primary='#286e64', secondary='#89632b',
            background='#f7f5ee', surface='#eeece3', panel='#e2e7df', foreground='#243c3a', dark=False))
        self.theme = 'auralis-dark'

    def compose(self) -> ComposeResult:
        with Horizontal(id='heading'):
            yield Static('auralis', id='brand')
            yield Static('', id='heading-subtitle', markup=False)
        with Horizontal(id='body'):
            with Vertical(id='main'):
                with VerticalScroll(id='transcript'):
                    yield Static('', id='welcome', markup=False)
                with Horizontal(id='notice-row'):
                    yield Static('', id='notice', markup=False)
                    yield Button('Details F8', id='details-toggle')
                yield Static('', id='pending', markup=False)
                yield OptionList(id='slash-menu', markup=False)
                with Vertical(id='input-box'):
                    yield Composer(id='composer', placeholder='Message your companion…  / for commands',
                        show_line_numbers=False, tab_behavior='focus')
            with Vertical(id='sidebar'):
                yield Static('CURRENT CHAT', classes='section-label')
                yield Static('', id='chat-details', markup=False)
                yield Static('SAVED CHATS', classes='section-label')
                yield OptionList(id='chat-list', markup=False)
                yield Static('F2  Models\nF3  Providers\nF4  Characters\nF6  Voice\nCtrl+T  Theme\nCtrl+B  Sidebar', id='shortcut-list')
        yield Static('', id='status', markup=False)
        yield Footer()

    async def on_mount(self):
        self.composer = self.query_one('#composer', Composer)
        self.suggestions = self.query_one('#slash-menu', OptionList)
        self.transcript = self.query_one('#transcript', VerticalScroll)
        self.notice_row = self.query_one('#notice-row')
        self.pending_label = self.query_one('#pending', Static)
        self.status_label = self.query_one('#status', Static)
        self.chat_list = self.query_one('#chat-list', OptionList)
        self.sidebar_widget = self.query_one('#sidebar')
        self.details = self.query_one('#chat-details', Static)
        self.notice_row.display = False
        self.pending_label.display = False
        self.suggestions.display = False
        self.begin_capture_print(self)
        self.composer.focus()
        self.resize_layout()
        await self.refresh_session()
        self.set_interval(0.2, self.refresh_session)
        if self.startup:
            self.bridge.start()
        else:
            self.busy = False

    def on_resize(self, event):
        if hasattr(self, 'sidebar_widget'):
            self.resize_layout(event.size.width, event.size.height)

    def resize_layout(self, width=None, height=None):
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        self.sidebar_widget.display = self._sidebar_visible and width >= 100 and height >= 24
        self.set_class(height < 22, 'short')

    async def refresh_session(self):
        if self._refreshing or self._terminal_closing:
            return
        self._refreshing = True
        try:
            state = self.backend.get_state()
            changed = self.session_id != state['session_id']
            if changed:
                if self.session_id is not None:
                    self.drafts[self.session_id] = self.composer.text
                self.composer.load_text(self.drafts.get(state['session_id'], ''))
                self.session_id = state['session_id']
                self.rows.clear()
                self.signature = None
                self.revision = None
                self.total = 0
                self.pending = None
            provider = self.namespace['PROVIDERS'].get(state['provider'], {}).get('label', state['provider'])
            voice = state['voice']
            voice_text = 'off' if not voice['enabled'] else 'on' if voice['available'] else 'on · no key'
            phase = {'idle': 'Ready', 'thinking': 'Thinking…', 'preparing': 'Preparing voice…',
                     'speaking': 'Speaking…'}.get(state['phase'], state['phase'])
            if self.busy and state['phase'] == 'idle':
                phase = 'Working…'
            self.status_label.update(f"{state['character']}  ·  {phase}  ·  Voice {voice_text}")
            self.query_one('#heading-subtitle', Static).update(state['chat_title'])
            self.details.update(f"{state['chat_title']}\n\n{state['character']}\n{provider}\n{state['model']}")
            self.composer.placeholder = f"Message {state['character']}…   / commands"
            if state['chat_version'] != self.revision or changed:
                self.revision = state['chat_version']
                history = self.backend.get_history()
                if history.get('session_id') != state['session_id']:
                    self.revision = None
                    return
                self.merge_history(history)
                await self.render_history(state['character'])
                self.update_chats()
            if state.get('voice_error') and state['voice_error'] != getattr(self, '_voice_error', None):
                self.write_output('Voice unavailable: ' + state['voice_error'])
            self._voice_error = state.get('voice_error')
            self.pending_label.display = self.pending is not None and self.busy
            if self.pending:
                self.pending_label.update('You · ' + self.pending[:160].replace('\n', ' ') + '\nWaiting for a reply…')
        except Exception as error:
            self.status_label.update(f'Could not refresh the session ({type(error).__name__}). Retrying…')
            # Do not cache a failed revision: the next tick retries the history.
            self.revision = None
        finally:
            self._refreshing = False

    def merge_history(self, history):
        start = history.get('before', 0)
        total = start + len(history['messages'])
        if total < self.total:
            self.rows.clear()
        self.total = total
        self.rows.update((start + i, message) for i, message in enumerate(history['messages']))

    async def render_history(self, character, *, older=False):
        async with self._history_lock:
            await self._render_history(character, older=older)

    async def _render_history(self, character, *, older=False):
        messages = tuple((i, m['role'], m['content']) for i, m in sorted(self.rows.items()))
        signature = (character, messages)
        if signature == self.signature:
            return
        follow = self.transcript.is_vertical_scroll_end or self._rendered_session != self.session_id
        nodes = self._history_nodes
        incoming = {i: (role, content) for i, role, content in messages}
        reset = self._rendered_session != self.session_id or any(
            i not in incoming or incoming[i] != entry[2:] for i, entry in nodes.items()
        )
        # Keep the first existing message fixed when earlier history is inserted.
        anchor = None
        if older and nodes:
            first_label = nodes[min(nodes)][0]
            anchor = (first_label, first_label.region.y - self.transcript.content_region.y)
        if reset or not messages or not nodes:
            await self.transcript.remove_children()
            nodes.clear()
        if not messages:
            await self.transcript.mount(Static(
                f'A little space to think.\n\nTalk with {character}.', id='welcome', markup=False))
        else:
            buttons = list(self.transcript.query('#load-older'))
            if min(incoming) == 0:
                for button in buttons:
                    await button.remove()
            elif not buttons:
                await self.transcript.mount(Button('Load earlier messages', id='load-older'), before=0)
            added = []
            try:
                new_messages = [(i, role, content) for i, role, content in messages if i not in nodes]
                # Consecutive new messages share one mount, preserving all existing
                # Markdown widgets (and their measured heights) on ordinary replies.
                groups = {}
                existing = sorted(nodes)
                for i, role, content in new_messages:
                    before = next((j for j in existing if j > i), None)
                    label = Static('YOU' if role == 'user' else character.upper(),
                        classes='speaker ' + role, markup=False)
                    document = Markdown(classes='message ' + role, open_links=False)
                    groups.setdefault(before, []).extend([label, document])
                    added.append((i, label, document, role, content))
                for before, widgets in groups.items():
                    await self.transcript.mount(*widgets, before=nodes[before][0] if before is not None else None)
                # Parsing runs asynchronously. Wait for content to mount before
                # restoring an older-history anchor or following the last message.
                await asyncio.gather(*(document.update(content) for _, _, document, _, content in added))
                nodes.update((i, (label, document, role, content)) for i, label, document, role, content in added)
                for label, _, role, _ in nodes.values():
                    label.update('YOU' if role == 'user' else character.upper())
            except Exception:
                self._rendered_session = None
                self.signature = None
                raise
        self._rendered_session = self.session_id
        self.signature = signature
        if anchor is not None and anchor[0].is_mounted:
            label, offset = anchor
            self.call_after_refresh(lambda: self.transcript.scroll_to(
                y=label.virtual_region.y - offset, animate=False, immediate=True))
        elif follow and not older:
            self.call_after_refresh(lambda: self.transcript.scroll_end(animate=False, immediate=True))
        # Otherwise leave scroll_y alone. Replacing the entire transcript used
        # to clamp it to zero before asynchronous Markdown regained its height.

    def update_chats(self):
        chats = self.namespace['load_index']()
        signature = [(c['session_id'], c.get('title', 'Untitled'), c['session_id'] == self.session_id) for c in chats]
        if signature == self._chat_signature:
            return
        self._chat_signature = signature
        self.chat_list.clear_options()
        self.chat_list.add_options(Option(Text(('● ' if active else '  ') + title), id=chat_id)
                                   for chat_id, title, active in signature)

    @on(Button.Pressed, '#load-older')
    async def older_history(self):
        if not self.rows:
            return
        history = self.backend.get_history(min(self.rows))
        self.rows.update((history['before'] + i, m) for i, m in enumerate(history['messages']))
        await self.render_history(self.namespace['session'].character_name, older=True)

    @on(OptionList.OptionSelected, '#chat-list')
    def switch_chat(self, event):
        self.send('/switch ' + event.option.id)

    def write_output(self, text):
        text = text.strip()
        name = self.namespace['session'].character_name + ':'
        if not text or text.startswith((name, 'You (overlay):')):
            return
        lines = text.splitlines()
        self.output_lines.extend(lines)
        if isinstance(self.screen, DetailsScreen):
            self.screen.query_one(RichLog).write(Text(text))
        summary = next((line.strip() for line in lines if line.strip()), '')
        summary = summary.removeprefix('Auralis:').strip()
        self.query_one('#notice', Static).update(summary[:160])
        self.notice_row.display = True
        if self._notice_timer is not None:
            self._notice_timer.stop()
        self._notice_timer = self.set_timer(8, self.hide_notice)

    def hide_notice(self):
        self.notice_row.display = False

    @on(Button.Pressed, '#details-toggle')
    def open_details(self):
        self.action_details()

    def action_details(self):
        if isinstance(self.screen, DetailsScreen):
            self.screen.action_cancel()
        elif len(self.screen_stack) == 1:
            self.push_screen(DetailsScreen(self.output_lines), lambda _: self.composer.focus())

    def on_print(self, event: events.Print):
        self.write_output(event.text)

    async def on_bridge_event(self, event):
        value = event.value
        if isinstance(value, Output):
            self.write_output(value.text)
        elif isinstance(value, Ready):
            self.busy = False
            self.pending = None
            await self.refresh_session()
            if self._submission is not None:
                session_id, text, before = self._submission
                self._submission = None
                session = self.namespace['session']
                saved = session.chat.get('messages', [])[before:]
                if session.session_id == session_id and not any(
                    m.get('role') == 'user' and m.get('content') == text for m in saved
                ):
                    if not self.composer.text:
                        self.composer.load_text(text)
                        self.composer.move_cursor(self.composer.document.end)
                        self.write_output('The message was not saved. Your draft has been restored.')
                    else:
                        self.write_output('The message was not saved. Your next draft was kept. Unsent message:\n' + text)
        elif isinstance(value, Finished):
            self.exit(value.result)
        elif isinstance(value, Prompt):
            screen = ChoiceScreen(value.title, value.options, value.allow_cancel) if value.options is not None else PromptScreen(value.title)
            self.push_screen(screen, value.resolve)

    @on(TextArea.Changed, '#composer')
    def update_commands(self):
        text = self.composer.text
        self._slash_choices = command_choices(text)
        self.suggestions.clear_options()
        self.suggestions.add_options(Option(Text(name + '  ' + description), id=name)
                                     for name, description in self._slash_choices)
        self.suggestions.highlighted = 0 if self._slash_choices else None
        self.suggestions.display = bool(self._slash_choices) and text != self._dismissed_slash

    @on(Composer.Navigate)
    def navigate_commands(self, event):
        getattr(self.suggestions, 'action_cursor_' + event.direction)()

    def selected_command(self):
        index = self.suggestions.highlighted
        return self._slash_choices[index][0] if index is not None else None

    @on(Composer.Complete)
    def complete_command(self):
        choice = self.selected_command()
        if choice:
            self.composer.load_text(choice + ' ')
            self.composer.move_cursor(self.composer.document.end)
            self.suggestions.display = False

    @on(OptionList.OptionSelected, '#slash-menu')
    def command_clicked(self, event):
        self.send(event.option.id, from_composer=True)

    @on(Composer.Submit)
    def submit(self):
        text = self.selected_command() if self.suggestions.display else self.composer.text.strip()
        if text:
            self.send(text, from_composer=True)

    @on(Composer.Escape)
    def escape_composer(self):
        if self.suggestions.display:
            self._dismissed_slash = self.composer.text
            self.suggestions.display = False
        else:
            self.action_interrupt()

    def send(self, text, *, from_composer=False):
        text = text.strip()
        if not text:
            return False
        session = self.namespace['session']
        before = len(session.chat.get('messages', []))
        if len(self.screen_stack) > 1 or self.busy or not self.bridge.submit(text):
            self.notify('Finish or cancel the current action first.', severity='warning')
            return False
        self.busy = True
        self.suggestions.display = False
        self._dismissed_slash = None
        is_message = text.split()[0].lower() not in KNOWN_COMMANDS and check_unrecognized_command(text) is None
        self.pending = text if is_message else None
        self._submission = (session.session_id, text, before) if is_message else None
        if from_composer:
            self.composer.load_text('')
        self.composer.focus()
        return True

    def action_commands(self):
        if len(self.screen_stack) > 1:
            return
        choices = command_choices('/')
        self.push_screen(ChoiceScreen('Commands', [name + '  ' + desc for name, desc in choices]),
            lambda index: self.send(choices[index][0]) if index is not None else None)

    def action_new_chat(self): self.send('/new')
    def action_chats(self): self.send('/switch')
    def action_models(self): self.send('/model')
    def action_providers(self): self.send('/provider')
    def action_characters(self): self.send('/character switch')

    def action_interrupt(self):
        if len(self.screen_stack) > 1:
            self.screen.action_cancel()
        self.backend.stop_speech()
        self.notify('Speech stopped. Future replies can still use voice.')

    def action_voice(self):
        if self._voice_toggle_busy:
            return
        self._voice_toggle_busy = True
        def toggle():
            try:
                self.backend.set_voice(not self.backend.get_state()['voice']['enabled'])
            except Exception:
                self.post_message(BridgeEvent(Output('Could not save the voice setting. Try again.')))
            finally:
                self.post_message(VoiceChanged())
        self.run_worker(toggle, thread=True, group='voice', exclusive=False)

    def on_voice_changed(self):
        self._voice_toggle_busy = False

    def action_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.resize_layout()

    def action_theme(self):
        self.theme = 'auralis-light' if self.theme == 'auralis-dark' else 'auralis-dark'

    def action_history_up(self):
        if isinstance(self.screen, DetailsScreen):
            self.screen.query_one(RichLog).scroll_page_up(animate=False)
        elif len(self.screen_stack) == 1:
            self.transcript.scroll_page_up(animate=False)

    def action_history_down(self):
        if isinstance(self.screen, DetailsScreen):
            self.screen.query_one(RichLog).scroll_page_down(animate=False)
        elif len(self.screen_stack) == 1:
            self.transcript.scroll_page_down(animate=False)

    def action_dismiss_output(self):
        self.hide_notice()
        if len(self.screen_stack) == 1:
            self.composer.focus()

    def action_quit(self):
        self._terminal_closing = True
        self.bridge.close()
        self.exit()

    def on_unmount(self):
        self._terminal_closing = True
        self.end_capture_print(self)
        self.bridge.close()


def run_terminal_ui(namespace):
    app = WosTerminal(namespace)
    try:
        return app.run()
    finally:
        app.bridge.close()
