import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(dotenv_path=ENV_PATH)

PLACEHOLDER_API_KEY = "your_api_key_here"


def chat(model, messages):
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print(
            "WLLM: GEMINI_API_KEY was not found. Copy .env.txt to .env, "
            "add your own key, and restart WOS."
        )
        return None

    if api_key.strip() == PLACEHOLDER_API_KEY:
        print(
            "WLLM: GEMINI_API_KEY is still the placeholder value. "
            "Replace it in .env with your own key and restart WOS."
        )
        return None

    try:
        client = genai.Client(api_key=api_key)

        system_prompt = ""
        contents = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if not isinstance(content, str):
                print("WLLM: A message contains invalid content.")
                return None

            if role == "system":
                system_prompt = content
            elif role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=content)],
                    )
                )
            elif role == "assistant":
                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(text=content)],
                    )
                )

        if not contents:
            print("WLLM: There are no messages to send.")
            return None

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                automatic_function_calling=(
                    types.AutomaticFunctionCallingConfig(
                        disable=True
                    )
                ),
            ),
        )

        reply = response.text

        if not isinstance(reply, str) or not reply.strip():
            print("WLLM: Gemini returned an empty response.")
            return None

        return reply.strip()

    except Exception as error:
        print(f"WLLM: Gemini request failed ({type(error).__name__}). Check the key, model, connection and quota.")
        return None