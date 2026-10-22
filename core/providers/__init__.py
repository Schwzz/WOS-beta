from core.providers.ollama import chat as ollama_chat
from core.providers.cloud import chat as cloud_chat
from core.providers.remote import chat as remote_chat, ProviderError
from core.provider_registry import PROVIDERS


def chat(provider, model, messages):
    if not isinstance(provider, str):
        print("WLLM: Provider name must be a string.")
        return None

    provider = provider.strip().lower()

    try:
        if provider == "ollama":
            return ollama_chat(model, messages)

        if provider == "cloud":
            return cloud_chat(model, messages)

        if provider in PROVIDERS:
            return remote_chat(provider, model, messages)

        print(f"WLLM: Unknown provider '{provider}'.")
        return None

    except ProviderError:
        raise
    except Exception as error:
        print(
            f"WLLM: Unexpected error from provider "
            f"'{provider}': {error}"
        )
        return None
