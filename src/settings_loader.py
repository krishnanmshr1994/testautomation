import os
import json

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")
_settings_cache = None

def load_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                raw_content = f.read()
            import re
            # Strip lines starting with optional whitespace and // or #
            cleaned_content = re.sub(r'^\s*(?://|#).*$', '', raw_content, flags=re.MULTILINE)
            _settings_cache = json.loads(cleaned_content)
        except Exception:
            # Safe fallbacks reflecting original hardcoded values
            _settings_cache = {
                "concurrency": {
                    "max_page_concurrency": 3,
                    "max_llm_concurrency": 3,
                    "min_llm_request_delay": 1.0,
                    "max_llm_requests_per_minute": 5
                },
                "timeouts": {
                    "page_navigation": 10000,
                    "element_scroll": 3000,
                    "element_fill": 10000,
                    "element_press": 5000,
                    "element_click": 10000,
                    "element_force_click": 5000,
                    "page_load_state_network_idle": 10000,
                    "page_load_state_dom_loaded": 5000,
                    "reasoning_llm_timeout": 60.0,
                    "fast_llm_timeout": 60.0,
                    "openrouter_reasoning_request_timeout": 60.0,
                    "openrouter_fast_request_timeout": 30.0,
                    "non_openrouter_llm_timeout": 120.0,
                    "context_input_timeout": 120.0
                }
            }
    return _settings_cache

def get_concurrency_settings() -> dict:
    return load_settings().get("concurrency", {})

def get_timeout_settings() -> dict:
    return load_settings().get("timeouts", {})
