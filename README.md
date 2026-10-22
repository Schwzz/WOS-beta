# WaifuOS (WOS)

A terminal-based AI companion. Chats and memories are saved locally in a `data/` folder, which WOS creates on first run.

## Requirements

- Python 3.11 or newer
- Packages: `requests`, `python-dotenv`, `google-genai`

```
pip install requests python-dotenv google-genai
```

All three are needed whichever provider you use, because the provider modules are loaded at startup.

## First-time setup

1. Install the packages above.
2. Choose a provider in `config.json`, or use `/provider` after starting. `cloud` remains the Gemini ID. OpenAI, Claude, OpenRouter, DeepSeek, Grok, and Ollama are also supported. The default character is `nexis`.
3. Set up the provider you chose (see below).
4. Start WOS from this folder:

```
python start.py
```

Type `/help` to see all commands.

### Cloud providers

Configure the keys you use in `.env`, then restart WOS and use `/provider` or the
Companion chat provider dropdown to select a provider and a discovered model.
See the [provider/key table](OVERLAY.md#models) for setup and limitations. No model
is chosen automatically for a newly added provider.

### Gemini setup

WOS reads your API key from a file named `.env` in this folder. The key is not included, and `.env.txt` is only a template.

1. Copy the template:
   - Windows: `copy .env.txt .env`
   - macOS / Linux: `cp .env.txt .env`
2. Open `.env` and replace `your_api_key_here` with your own Gemini API key.
3. Restart WOS. The key is read at startup.

Never share or commit `.env`. The included `.gitignore` excludes it.

### Ollama (local)

1. Install and start Ollama (WOS connects to `http://localhost:11434`).
2. Download a model, for example `ollama pull <model-name>`.
3. Before the first launch, set `"provider": "ollama"` and `"model": "<model-name>"` in `config.json`.

If you have already started WOS, each saved chat keeps its own provider and model. Use `/provider ollama <model-name>` (or `/provider ollama` and WOS will ask for a model) to change the current chat, and `/model <model-name>` to change only the model.

## Using WOS

Type `/help` to see every command in alphabetical order, or `/help <command>` to learn what one command does, how to type it, and what its subcommands are. For example: `/help chat`, `/help model`, `/help provider`, or `/help chat switch`.

### Managing chats

All chat actions live under `/chat`. Type `/chat` on its own to choose an action from a numbered menu.

| Command | What it does |
| --- | --- |
| `/chat` | Choose an action from a numbered menu |
| `/chat list` | List saved chats |
| `/chat switch [id]` | Switch to a saved chat |
| `/chat rename [id] [new title]` | Rename a saved chat |
| `/chat delete [id]` | Delete a saved chat (asks you to confirm) |
| `/chat clear` | Remove all messages from the current chat (asks you to confirm) |

You never need to remember a chat's id. Leave it out and WOS shows your chats as a numbered list. For `/chat rename`, you pick the chat and then WOS asks for the new title.

**Numbered lists:** type the number and press Enter. Press Enter on its own, or type `0`, to cancel and leave everything unchanged. Ctrl+C also cancels. If you type something that isn't one of the numbers, WOS tells you and asks again.

Deleting a chat and clearing a chat always ask for confirmation first, and only an answer of `y` or `yes` goes ahead. Clearing removes the messages in the current chat but keeps the chat itself and your saved memories. `/new` follows the same rule: when the saved-chat limit is reached, it asks which chat to delete instead of removing one for you.

The numbered list is also used by `/character switch` when you don't give a character id.

**Older commands still work.** They are kept so existing habits keep working, but `/chat ...` is the recommended form:

| Older command | Same as |
| --- | --- |
| `/chats` | `/chat list` |
| `/switch [id]` | `/chat switch [id]` |
| `/rename [id] [new title]` | `/chat rename [id] [new title]` |
| `/delete [id]` | `/chat delete [id]` |
| `/clear` | `/chat clear` |

Note that `/clear` now asks for confirmation, as `/chat clear` does.

### Choosing a provider and model

Type `/provider` on its own to pick a provider from a numbered list, then pick one of its models from a second numbered list. Type `/model` on its own to pick a different model for the provider you are already using. Nothing changes until you have made your choice, and you can press Enter or type `0` at any point to cancel.

| Command | What it does |
| --- | --- |
| `/provider` | Choose a provider, then a model, from numbered lists |
| `/provider <provider> [model]` | Switch directly, for example `/provider ollama llama3` |
| `/model` | Choose a model for the current provider from a numbered list |
| `/model <model>` | Switch the model directly, for example `/model llama3` |

**Where the model lists come from.** WOS never makes up model names.

- **Ollama:** the list shows the models actually installed on your computer, asked from Ollama while it is running, with each model's size when Ollama reports it (for example `llama3:latest - 4.7 GB`). Pick one by number. WOS only accepts an installed model, and tells you how to install one (`ollama pull <model>`) if you type something else.
- **If Ollama is not running:** WOS says so and shows an "Ollama Unavailable" menu: start Ollama and choose **Try again**, choose to **type a model name yourself** (WOS cannot check it while Ollama is unavailable), or press Enter or `0` to cancel. Cancelling leaves your provider and model unchanged.
- **Cloud providers:** WOS discovers model IDs using each provider’s configured API key. Gemini filters for chat generation support; other model lists may include text models incompatible with the chat endpoint. A newly configured key opens a picker for the current chat. `/model`, `/provider`, and the Companion chat dropdown also offer discovered models. If discovery fails, WOS identifies a cached/saved fallback and explains the error. **Other** still accepts an exact model name. Listing a model does not guarantee account access or quota.

**What a change affects.** `/provider` and `/model` change the **current chat only**, and WOS tells you so after each change. New chats start with your defaults, which you change separately with `/defaults provider`, `/defaults model` and `/defaults character`.

## Customization

- `config.json`: default provider, model, and character, used when you start a new chat with `/new`. Change them with `/defaults`. Existing chats keep their own settings. You can also list model names you use under `suggested_models`, and WOS will offer them in the `/provider` and `/model` lists, for example `"suggested_models": {"cloud": ["your-model-id"]}`.
- `settings.json`: terminal colors, the maximum number of saved chats, and context limits. Restart WOS after editing it. WOS never deletes a chat on its own: when the limit is reached, `/new` asks which chat to delete first.
- `characters/`: one JSON file per character. A character needs at least a `name` and a `personality` (see `characters/nexis.json` for a full example). If a character does not show up, `/character list` lists each skipped file with the reason, such as a JSON typo, a missing field, or a file name WOS cannot use. Files saved with a UTF-8 BOM, which some Windows editors add, are accepted for `config.json`, `settings.json` and character files. Use `/character list` to see them and `/character switch` to change the current chat's character.

## Running the tests

The tests use Python's built-in `unittest`, installed `requests`/`python-dotenv`, Node.js, a temporary copy of the settings, and fake provider responses. They need no API key, no network, and do not touch your real chats or `config.json`.

```
python -m unittest discover -s tests -t .
```

## WOS Companion

The resizable Companion adds a hollow Crystal core avatar that reacts to playing speech, other customizable styles, light/dark themes, chat and model controls, and a Stop speaking action. See [OVERLAY.md](OVERLAY.md) for setup, controls, screenshots, and validation details.
