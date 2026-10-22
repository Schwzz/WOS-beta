"""Text chat adapters. API keys stay in headers and provider errors are redacted."""
import requests

from core.provider_registry import PROVIDERS, api_key, headers, http_error


class ProviderError(Exception):
    """An error safe to display in the terminal or Companion."""


def chat(provider, model, messages, post=None):
    info = PROVIDERS[provider]
    key = api_key(provider)
    if not key:
        raise ProviderError(f"Set {info['key_env']} in .env and restart WOS.")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant')
        or not isinstance(m.get('content'), str) for m in messages
    ):
        raise ProviderError("The conversation contains invalid messages.")
    payload = {"model": model, "messages": messages, "stream": False}
    endpoint = "/chat/completions"
    if provider == "anthropic":
        endpoint = "/messages"
        payload.update(system="\n\n".join(m['content'] for m in messages if m['role'] == 'system'),
                       messages=[m for m in messages if m['role'] != 'system'], max_tokens=4096)
    try:
        response = (post or requests.post)(info['base'] + endpoint,
            headers=headers(provider, key), json=payload, timeout=(5, 120), allow_redirects=False)
        if response.status_code != 200:
            raise ProviderError(http_error(provider, response.status_code))
        data = response.json()
        if provider == "anthropic":
            reply = "\n".join(block['text'] for block in data['content']
                              if isinstance(block, dict) and block.get('type') == 'text'
                              and isinstance(block.get('text'), str))
        else:
            message = data['choices'][0]['message']
            reply = message.get('content') or message.get('refusal')
        if not isinstance(reply, str) or not reply.strip():
            raise ProviderError(f"{info['label']} returned no text. Try another text chat model.")
        return reply.strip()
    except ProviderError:
        raise
    except requests.exceptions.Timeout:
        raise ProviderError(f"{info['label']} took too long to respond.") from None
    except requests.exceptions.RequestException:
        raise ProviderError(f"Could not connect to {info['label']}.") from None
    except Exception:
        raise ProviderError(f"{info['label']} returned an unreadable response.") from None
