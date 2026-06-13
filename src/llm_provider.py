import os
import json
from openai import AsyncOpenAI

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "llm_config.json")
_config_cache = None

def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    return _config_cache

def get_active_provider() -> dict:
    config = _load_config()
    active_name = config.get("active_provider")
    return config["providers"][active_name]

def is_openrouter() -> bool:
    config = _load_config()
    return config.get("active_provider") == "openrouter"

def get_llm_client() -> AsyncOpenAI:
    provider = get_active_provider()
    api_key = os.getenv(provider["env_key"])
    if not api_key:
        raise ValueError(f"Missing API key for active provider. Please set {provider['env_key']} in your .env file.")
    
    return AsyncOpenAI(
        base_url=provider["base_url"],
        api_key=api_key,
    )

def get_fast_model() -> str:
    provider = get_active_provider()
    # Allow environment variable overrides for custom model swapping without touching JSON
    return os.getenv("FAST_MODEL_NAME", provider["fast_model"])

def get_reasoning_model() -> str:
    provider = get_active_provider()
    # Allow environment variable overrides
    return os.getenv("MODEL_NAME", provider["reasoning_model"])

def get_provider_priority() -> list:
    config = _load_config()
    priority = config.get("provider_priority", [])
    if not priority:
        # Fallback to active_provider if priority is missing/empty
        active = config.get("active_provider")
        if active:
            priority = [active]
    return priority

def get_provider_client_and_model(provider_name: str, model_type: str) -> tuple:
    config = _load_config()
    provider = config["providers"][provider_name]
    api_key = os.getenv(provider["env_key"])
    if not api_key:
        raise ValueError(f"Missing API key for provider {provider_name}. Please set {provider['env_key']} in your .env file.")
    
    return AsyncOpenAI(
        base_url=provider["base_url"],
        api_key=api_key,
    ), provider["reasoning_model"] if model_type == "reasoning" else provider["fast_model"]
