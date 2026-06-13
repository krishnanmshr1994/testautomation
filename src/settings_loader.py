import os
import json

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")
_settings_cache = None

def load_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw_content = f.read()
        import re
        # Strip lines starting with optional whitespace and // or #
        cleaned_content = re.sub(r'^\s*(?://|#).*$', '', raw_content, flags=re.MULTILINE)
        _settings_cache = json.loads(cleaned_content)
    return _settings_cache

def get_concurrency_settings() -> dict:
    return load_settings().get("concurrency", {})

def get_timeout_settings() -> dict:
    return load_settings().get("timeouts", {})
