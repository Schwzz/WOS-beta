"""Provider identities and fixed API endpoints. No credentials are persisted."""
import os

PROVIDERS = {
    "cloud": {"label": "Gemini", "key_env": "GEMINI_API_KEY"},
    "ollama": {"label": "Ollama", "key_env": None},
    "openai": {"label": "OpenAI", "key_env": "OPENAI_API_KEY", "base": "https://api.openai.com/v1"},
    "anthropic": {"label": "Claude", "key_env": "ANTHROPIC_API_KEY", "base": "https://api.anthropic.com/v1"},
    "openrouter": {"label": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api/v1"},
    "deepseek": {"label": "DeepSeek", "key_env": "DEEPSEEK_API_KEY", "base": "https://api.deepseek.com"},
    "xai": {"label": "Grok", "key_env": "XAI_API_KEY", "base": "https://api.x.ai/v1"},
}


def api_key(provider):
    name = PROVIDERS.get(provider, {}).get("key_env")
    value = os.getenv(name, "").strip() if name else ""
    return value if value and not value.lower().startswith("your_") else None


def headers(provider, key):
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def http_error(provider, status):
    reason = {400: "request or model not supported", 401: "API key rejected",
              402: "insufficient credits", 403: "access denied", 404: "model or endpoint not found",
              429: "rate limit or quota reached"}.get(status, "provider request failed")
    return f"{PROVIDERS[provider]['label']}: {reason} (HTTP {status})."


def provider_options():
    return [{"id": name, "label": info["label"], "configured": name == "ollama" or bool(api_key(name)),
             "key_env": info["key_env"]} for name, info in PROVIDERS.items()]
