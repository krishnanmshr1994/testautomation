import os
import asyncio
import httpx
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from openai import AsyncOpenAI

# Use llm_provider for all model and client configuration
from src.llm_provider import get_llm_client, is_openrouter, get_fast_model, get_reasoning_model, get_provider_priority, get_provider_client_and_model
from src.settings_loader import get_concurrency_settings, get_timeout_settings

# Global references
_playwright = None
_browser: Browser = None
_last_successful_provider_idx: int = 0
_provider_cooldowns: dict = {}  # provider_name -> epoch timestamp when cooldown expires

# ── Model Pools (round-robin across free-tier models) ──────────────────
# Each model has its own independent rate-limit bucket on OpenRouter.
# On a 429, we immediately rotate to the next model instead of waiting.
# This gives us N× the effective throughput with zero added latency.
_FAST_MODEL_POOL = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
]

_REASONING_MODEL_POOL = [
    "poolside/laguna-m.1:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]

_pool_index: int = 0            # round-robin cursor (best-effort, no lock needed)

# Burst control: Free OpenRouter keys allow max ~2-3 concurrent connections.
# This prevents all audit passes across all pages from firing on the exact same millisecond.
_llm_semaphore: asyncio.Semaphore | None = None
_total_tokens_consumed: int = 0

def _get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        concurrency = get_concurrency_settings()
        limit = concurrency.get("max_llm_concurrency", 3)
        _llm_semaphore = asyncio.Semaphore(limit)
    return _llm_semaphore

import time

_last_llm_request_time: float = 0.0
_request_timestamps: list[float] = []
_rate_limit_lock = asyncio.Lock()

async def _throttle_llm_request():
    global _last_llm_request_time, _request_timestamps, _last_successful_provider_idx
    providers = get_provider_priority()
    if providers and (_last_successful_provider_idx % len(providers)) != 0:
        return
    concurrency_cfg = get_concurrency_settings()
    
    # 1. Enforce per-second minimum delay
    delay = concurrency_cfg.get("min_llm_request_delay", 1.0)
    if delay > 0:
        async with _rate_limit_lock:
            now = time.time()
            elapsed = now - _last_llm_request_time
            if elapsed < delay:
                sleep_time = delay - elapsed
                await asyncio.sleep(sleep_time)
            _last_llm_request_time = time.time()

    # 2. Enforce per-minute sliding window limit
    max_rpm = concurrency_cfg.get("max_llm_requests_per_minute", 0)
    if max_rpm > 0:
        async with _rate_limit_lock:
            now = time.time()
            # Retain only timestamps from the last 60 seconds
            _request_timestamps = [t for t in _request_timestamps if now - t < 60.0]
            
            if len(_request_timestamps) >= max_rpm:
                oldest_timestamp = _request_timestamps[0]
                sleep_time = 60.0 - (now - oldest_timestamp)
                if sleep_time > 0:
                    from src.logger import stream_log
                    await stream_log(
                        f"[Rate Limiter] Approaching rolling per-minute API limit ({max_rpm} RPM). "
                        f"Throttling request pipeline for {sleep_time:.1f}s to avoid 429 block..."
                    )
                    await asyncio.sleep(sleep_time)
            
            _request_timestamps.append(time.time())

# Removed get_ai_client as it's now handled by llm_provider
async def distill_dom(page) -> str:
    """
    Executes a JavaScript minifier in the browser to strip out non-semantic
    elements (scripts, styles, svgs, classes) and returns a clean HTML skeleton.
    Repeats are collapsed/pruned to prevent token limits from being exceeded.
    """
    js_code = """() => {
        // Capture native HTML5 validation messages from the real DOM before cloning
        let validationErrors = [];
        document.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.validationMessage) {
                validationErrors.push(`[Native Validation on ${el.name || el.id || el.type}]: ${el.validationMessage}`);
            }
        });

        const clone = document.body.cloneNode(true);
        // Remove junk tags
        const junk = clone.querySelectorAll('script, style, svg, noscript, iframe, path, meta, link');
        junk.forEach(el => el.remove());
        
        // Remove visual attributes to save tokens
        const allElements = clone.querySelectorAll('*');
        allElements.forEach(el => {
            el.removeAttribute('class');
            el.removeAttribute('style');
            el.removeAttribute('data-testid');
            el.removeAttribute('width');
            el.removeAttribute('height');
        });

        // Prune repeating sibling elements to keep DOM compact
        const pruneRepeatingSiblings = (parent, selector, maxCount = 8) => {
            if (!parent) return;
            const children = Array.from(parent.querySelectorAll(`:scope > ${selector}`));
            if (children.length > maxCount) {
                const keepStart = Math.max(1, maxCount - 3);
                const keepEnd = 2;
                for (let i = keepStart; i < children.length - keepEnd; i++) {
                    children[i].remove();
                }
                const placeholder = document.createElement(selector);
                placeholder.setAttribute('data-pruned', 'true');
                placeholder.innerHTML = `[... Collapsed/Pruned ${children.length - keepStart - keepEnd} repeating ${selector} elements ...]`;
                parent.insertBefore(placeholder, children[children.length - keepEnd]);
            }
        };

        // Run pruning on tables, lists, select options
        clone.querySelectorAll('table, tbody, thead, tr, ul, ol, select').forEach(container => {
            pruneRepeatingSiblings(container, 'tr', 6);
            pruneRepeatingSiblings(container, 'li', 6);
            pruneRepeatingSiblings(container, 'option', 4);
        });

        // Also prune repeating anchor tags directly under any container
        clone.querySelectorAll('*').forEach(parent => {
            if (parent.children && parent.children.length > 15) {
                pruneRepeatingSiblings(parent, 'a', 6);
                pruneRepeatingSiblings(parent, 'div', 6);
            }
        });
        
        let html = clone.innerHTML;
        if (validationErrors.length > 0) {
            html = `<div id="playwright-native-validation-errors" style="color:red; font-weight:bold;">\n` + 
                   validationErrors.join('\\n') + 
                   `\n</div>\n` + html;
        }
        return html;
    }"""
    distilled_html = await page.evaluate(js_code)
    # Strip excessive newlines and whitespace
    import re
    clean_html = re.sub(r'\s+', ' ', distilled_html).strip()
    return clean_html


async def _call_openrouter(
    messages: list,
    model: str,
    temperature: float,
    max_tokens: int,
    use_reasoning: bool,
    model_pool: list[str] | None = None
) -> tuple[str, str | None]:
    """
    Internal: POST to OpenRouter.
    - use_reasoning=True  → single model (reasoning), standard backoff on 429.
    - use_reasoning=False → rotates through model_pool on 429 (immediate, no wait).
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-OpenRouter-Title": "AI QA Security Automation"
    }

    candidates = model_pool if model_pool else [model]
    pool_cursor = 0          # which candidate to try next
    max_attempts = len(candidates) * 4   # allow 4 full cycles
    backoff_factor = 2.0
    timeouts = get_timeout_settings()
    reasoning_req_timeout = timeouts.get("openrouter_reasoning_request_timeout", 60.0)
    fast_req_timeout = timeouts.get("openrouter_fast_request_timeout", 30.0)
    timeout_per_request = reasoning_req_timeout if use_reasoning else fast_req_timeout

    from src.logger import stream_log
    semaphore = _get_llm_semaphore()

    for attempt in range(max_attempts):
        current_model = candidates[pool_cursor % len(candidates)]
        payload: dict = {
            "model": current_model,
            "messages": messages,
            "temperature": temperature,
            "top_p": 0.7,
            "max_tokens": max_tokens,
        }
        if use_reasoning:
            payload["reasoning"] = {"enabled": True}

        try:
            # Prevent "thundering herd" bursts across all pages
            async with semaphore:
                await _throttle_llm_request()
                async with httpx.AsyncClient(timeout=timeout_per_request) as client:
                    response = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json=payload
                    )

            # ── 429: rotate to next pool model (no wait) or backoff if exhausted ──
            if response.status_code == 429:
                if len(candidates) > 1:
                    next_model = candidates[(pool_cursor + 1) % len(candidates)]
                    pool_cursor += 1
                    
                    # If we've exhausted all models in the pool, OpenRouter is rate-limiting
                    # the entire API Key. We MUST sleep before the next cycle.
                    if pool_cursor % len(candidates) == 0:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            sleep_time = float(retry_after) if retry_after else backoff_factor * (2 ** (attempt // len(candidates)))
                        except ValueError:
                            sleep_time = backoff_factor * (2 ** (attempt // len(candidates)))
                        await stream_log(
                            f"[Pool] Key rate-limited across all {len(candidates)} models. "
                            f"Sleeping for {sleep_time:.1f}s before next cycle..."
                        )
                        await asyncio.sleep(sleep_time)
                    else:
                        await stream_log(f"[Pool] 429 on '{current_model}' → rotating to '{next_model}'...")
                    
                    continue  # retry on next model
                else:
                    # Single-model path: backoff and retry same
                    retry_after = response.headers.get("Retry-After")
                    try:
                        sleep_time = float(retry_after) if retry_after else backoff_factor * (2 ** attempt)
                    except ValueError:
                        sleep_time = backoff_factor * (2 ** attempt)
                    await stream_log(
                        f"[Rate Limit] 429 for '{current_model}'. "
                        f"Retrying in {sleep_time:.0f}s (attempt {attempt+1}/{max_attempts})..."
                    )
                    await asyncio.sleep(sleep_time)
                    continue

            # ── 5xx server errors: short backoff, same model ──────────────────
            if response.status_code in (502, 503, 504):
                sleep_time = min(backoff_factor * (2 ** attempt), 30.0)
                await stream_log(
                    f"[Server Error] {response.status_code} for '{current_model}'. "
                    f"Retrying in {sleep_time:.0f}s..."
                )
                await asyncio.sleep(sleep_time)
                continue

            response.raise_for_status()
            data = response.json()

            # ── OpenRouter sometimes embeds errors in a 200 OK body ────────────
            if "choices" not in data or not data["choices"]:
                if "error" in data:
                    err_msg = data["error"].get("message", "Unknown error")
                    code = data["error"].get("code")
                    if code == 429 or "rate limit" in err_msg.lower() or "too many" in err_msg.lower():
                        if len(candidates) > 1:
                            pool_cursor += 1
                            if pool_cursor % len(candidates) == 0:
                                sleep_time = backoff_factor * (2 ** (attempt // len(candidates)))
                                await stream_log(f"[Pool] Inline 429 on all models. Sleeping {sleep_time:.1f}s...")
                                await asyncio.sleep(sleep_time)
                            else:
                                await stream_log(f"[Pool] Inline 429 on '{current_model}' → rotating pool...")
                            continue
                        await asyncio.sleep(backoff_factor * (2 ** attempt))
                        continue
                    raise ValueError(f"OpenRouter Error: {err_msg}")
                raise ValueError(f"Unexpected OpenRouter response format: {data}")

            message_data = data["choices"][0]["message"]
            content = message_data.get("content") or ""
            reasoning_details = message_data.get("reasoning_details")

            # Token tracking
            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            if total_tokens > 0:
                global _total_tokens_consumed
                _total_tokens_consumed += total_tokens
                await stream_log(f"[Token Tracker] Model '{current_model}' consumed {total_tokens:,} tokens. Session total: {_total_tokens_consumed:,}")

            return content, reasoning_details

        except httpx.RequestError as req_err:
            sleep_time = min(backoff_factor * (2 ** attempt), 20.0)
            await stream_log(
                f"[Network Error] {type(req_err).__name__} on '{current_model}': {req_err}. "
                f"Retrying in {sleep_time:.0f}s..."
            )
            await asyncio.sleep(sleep_time)
            continue

    raise RuntimeError(
        f"All {len(candidates)} pool model(s) exhausted after {max_attempts} attempts."
    )



async def ask_llm(prompt: str = None, system: str = "You are a QA and Security testing expert.", temperature: float = 0.2, messages: list = None, model_type: str = "reasoning") -> tuple[str, str | None]:
    """
    Reasoning model call (poolside/laguna-m.1 by default).
    Used for complex tasks: audit, test plan generation, context analysis.
    Automatically falls back to the fast model on timeout.
    """
    try:
        if is_openrouter():
            # Build reasoning pool: env override as first choice, then remaining defaults
            env_model = get_reasoning_model()
            pool = [env_model] + [m for m in _REASONING_MODEL_POOL if m != env_model]
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            try:
                timeouts = get_timeout_settings()
                reasoning_timeout = timeouts.get("reasoning_llm_timeout", 60.0)
                return await asyncio.wait_for(
                    _call_openrouter(messages, pool[0], temperature, max_tokens=8192, use_reasoning=True, model_pool=pool),
                    timeout=reasoning_timeout
                )
            except asyncio.TimeoutError:
                from src.logger import stream_log
                timeouts = get_timeout_settings()
                reasoning_timeout = timeouts.get("reasoning_llm_timeout", 60.0)
                await stream_log(f"[Timeout] Reasoning model timed out after {reasoning_timeout}s. Falling back to fast model...")
                return await ask_llm_fast(messages=messages, temperature=temperature)
        else:
            # Non-OpenRouter path 
            global _last_successful_provider_idx, _provider_cooldowns, _total_tokens_consumed
            providers = get_provider_priority()
            if not providers:
                providers = ["mistral"]
            
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            timeouts = get_timeout_settings()
            non_or_timeout = timeouts.get("non_openrouter_llm_timeout", 120.0)
            concurrency_cfg = get_concurrency_settings()
            cooldown_secs = concurrency_cfg.get("provider_cooldown_secs", 62.0)
            
            max_attempts = 5
            backoff_factor = 2.0
            from src.logger import stream_log
            semaphore = _get_llm_semaphore()
            
            for attempt in range(max_attempts):
                # Dynamically resolve provider client and model at start of attempt
                # Skip any provider still in cooldown — pick the next available one
                now = time.time()
                base_idx = _last_successful_provider_idx % len(providers)
                current_provider_idx = base_idx
                for offset in range(len(providers)):
                    candidate_idx = (base_idx + offset) % len(providers)
                    if _provider_cooldowns.get(providers[candidate_idx], 0) <= now:
                        current_provider_idx = candidate_idx
                        break
                provider_name = providers[current_provider_idx]
                client, model_name = get_provider_client_and_model(provider_name, model_type)
                
                try:
                    async with semaphore:
                        # Re-resolve after semaphore wait in case another task failed over in the meantime
                        current_provider_idx = _last_successful_provider_idx % len(providers)
                        provider_name = providers[current_provider_idx]
                        client, model_name = get_provider_client_and_model(provider_name, model_type)
                        
                        if current_provider_idx == 0:
                            await _throttle_llm_request()
                            
                            # Re-resolve after throttling sleep in case failover happened during the sleep
                            current_provider_idx = _last_successful_provider_idx % len(providers)
                            if current_provider_idx != 0:
                                provider_name = providers[current_provider_idx]
                                client, model_name = get_provider_client_and_model(provider_name, model_type)
                        
                        response = await client.chat.completions.create(
                            model=model_name, messages=messages,
                            temperature=temperature, max_tokens=4096, timeout=non_or_timeout,
                            top_p=1.0
                        )
                    
                    if hasattr(response, "usage") and response.usage:
                        total_tokens = getattr(response.usage, "total_tokens", 0)
                        if total_tokens > 0:
                            _total_tokens_consumed += total_tokens
                            await stream_log(f"[Token Tracker] Model '{model_name}' consumed {total_tokens:,} tokens. Session total: {_total_tokens_consumed:,}")
                    _last_successful_provider_idx = current_provider_idx
                    return response.choices[0].message.content or "", None

                except Exception as e:
                    # Stamp the failing provider with a configurable cooldown
                    _provider_cooldowns[provider_name] = time.time() + cooldown_secs
                    await stream_log(
                        f"[LLM Failover] Request to '{provider_name}' failed ({e}). "
                        f"Cooling down '{provider_name}' for {cooldown_secs:.0f}s."
                    )

                    if len(providers) > 1:
                        # Scan all providers in priority order (starting after the failed one)
                        # to find the best available provider not currently in cooldown.
                        now = time.time()
                        next_provider = None
                        next_provider_idx = None
                        for offset in range(1, len(providers)):
                            candidate_idx = (current_provider_idx + offset) % len(providers)
                            candidate = providers[candidate_idx]
                            if _provider_cooldowns.get(candidate, 0) <= now:
                                next_provider = candidate
                                next_provider_idx = candidate_idx
                                break

                        # All providers are in cooldown — wait for the shortest remaining cooldown
                        if next_provider is None:
                            min_wait = min(
                                max(0.0, _provider_cooldowns.get(p, 0) - time.time())
                                for p in providers
                            )
                            best_idx = min(
                                range(len(providers)),
                                key=lambda i: _provider_cooldowns.get(providers[i], 0)
                            )
                            next_provider_idx = best_idx
                            next_provider = providers[best_idx]
                            await stream_log(
                                f"[Rate Limiter] All providers in cooldown. "
                                f"Waiting {min_wait:.1f}s before retrying '{next_provider}'..."
                            )
                            await asyncio.sleep(min_wait + 0.5)  # +0.5s buffer
                        else:
                            await stream_log(
                                f"[LLM Failover] Rotating to next available provider: '{next_provider}'..."
                            )

                        # Globally shift so other concurrent tasks also pick the new provider
                        _last_successful_provider_idx = next_provider_idx
                        try:
                            client, model_name = get_provider_client_and_model(next_provider, model_type)
                            current_provider_idx = next_provider_idx
                            provider_name = next_provider
                            continue  # retry immediately on the chosen provider
                        except Exception as failover_err:
                            await stream_log(f"[Failover Error] Failed to switch to {next_provider}: {failover_err}")

                    if attempt < max_attempts - 1:
                        sleep_time = backoff_factor * (2 ** attempt)
                        await stream_log(
                            f"[Rate Limit/API Error] Retrying on '{provider_name}' in {sleep_time:.1f}s (attempt {attempt+1}/{max_attempts})..."
                        )
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        raise e
    except asyncio.TimeoutError:
        raise
    except Exception as e:
        from src.logger import stream_log
        await stream_log(f"\n[LLM Error] Reasoning model failed: {e}")
        raise e


async def ask_llm_fast(prompt: str = None, system: str = "You are a QA and Security testing expert.", temperature: float = 0.1, messages: list = None) -> tuple[str, str | None]:
    """
    Fast model call — uses the FAST_MODEL_POOL for automatic round-robin rotation.
    On a 429 from any model, the pool immediately tries the next one (no wait).
    Used for: CSS selector identification, pass/fail verification, audit passes.
    """
    try:
        if is_openrouter():
            # Build pool: env override as first choice, then remaining defaults
            env_model = get_fast_model()
            pool = [env_model] + [m for m in _FAST_MODEL_POOL if m != env_model]
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            timeouts = get_timeout_settings()
            fast_timeout = timeouts.get("fast_llm_timeout", 60.0)
            return await asyncio.wait_for(
                _call_openrouter(messages, pool[0], temperature, max_tokens=4096, use_reasoning=False, model_pool=pool),
                timeout=fast_timeout
            )
        else:
            return await ask_llm(prompt=prompt, system=system, temperature=temperature, messages=messages, model_type="fast")
    except Exception as e:
        from src.logger import stream_log
        await stream_log(f"\n[LLM Fast Error] Fast model failed: {e}")
        raise e


async def ask_llm_json_with_healing(prompt: str, system: str = "You are a QA and Security testing expert.", temperature: float = 0.2, pydantic_model=None, max_retries: int = 3):
    """
    Calls the LLM and attempts to parse the result as JSON.
    If parsing or Pydantic validation fails, it appends the error to the message history and retries.
    """
    import json
    import re
    
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt}
    ]
    last_error = ""

    for attempt in range(max_retries):
        try:
            content, reasoning_details = await ask_llm(messages=messages, temperature=temperature)
        except Exception as api_err:
            last_error = f"API Error: {str(api_err)}"
            from src.logger import stream_log
            import random
            sleep_time = (2 ** attempt) + random.uniform(0, 2)
            await stream_log(f"[Self-Healing] Attempt {attempt + 1} failed due to API Error. Retrying in {sleep_time:.1f}s...")
            import asyncio
            await asyncio.sleep(sleep_time)
            continue # Retry without modifying history for network errors

        try:
            # Find the first '{' and decode exactly one JSON object from that position.
            # raw_decode() stops after the first valid object, ignoring any trailing text
            # or additional JSON blocks the model may have appended.
            start = content.find('{')
            if start == -1:
                raise ValueError("No JSON object found in response")
            data, _ = json.JSONDecoder().raw_decode(content, start)
            if pydantic_model:
                return pydantic_model(**data)
            return data
        except Exception as e:
            last_error = str(e)
            from src.logger import stream_log
            await stream_log(f"[Self-Healing] Attempt {attempt + 1} failed. JSON/Pydantic Error: {last_error}")
            
            # Record assistant turn preserving reasoning_details if present
            assistant_turn = {"role": "assistant", "content": content}
            if reasoning_details:
                assistant_turn["reasoning_details"] = reasoning_details
            
            feedback_content = f"[System Feedback] Your previous response failed to parse as valid JSON. Error: {last_error}\nPlease fix the formatting and try again. Respond ONLY with a valid JSON object."
            user_turn = {"role": "user", "content": feedback_content}
            
            messages.append(assistant_turn)
            messages.append(user_turn)
    
    raise ValueError(f"Failed to generate valid JSON after {max_retries} attempts. Last error: {last_error}")


async def ask_llm_fast_json_with_healing(prompt: str, system: str = "You are a QA and Security testing expert.", temperature: float = 0.1, pydantic_model=None, max_retries: int = 2):
    """
    Fast model variant of ask_llm_json_with_healing.
    Uses the Llama 3.3 70B fast model for selector identification and verification.
    Lower max_retries (2) since speed is prioritised over deep self-correction.
    """
    import json
    import re

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt}
    ]
    last_error = ""

    for attempt in range(max_retries):
        try:
            content, _ = await ask_llm_fast(messages=messages, temperature=temperature)
        except Exception as api_err:
            last_error = f"API Error: {str(api_err)}"
            from src.logger import stream_log
            import random
            sleep_time = (2 ** attempt) + random.uniform(0, 1)
            await stream_log(f"[Fast Self-Healing] Attempt {attempt + 1} failed (API). Retrying in {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
            continue

        try:
            # Find the first '{' and decode exactly one JSON object from that position.
            # raw_decode() stops after the first valid object, ignoring any trailing text
            # or additional JSON blocks the model may have appended.
            start = content.find('{')
            if start == -1:
                raise ValueError("No JSON object found in response")
            data, _ = json.JSONDecoder().raw_decode(content, start)
            if pydantic_model:
                return pydantic_model(**data)
            return data
        except Exception as e:
            last_error = str(e)
            from src.logger import stream_log
            await stream_log(f"[Fast Self-Healing] Attempt {attempt + 1} failed. JSON Error: {last_error}")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"[System Feedback] JSON parse failed: {last_error}\nRespond ONLY with a valid JSON object."})

    raise ValueError(f"Fast model: failed to generate valid JSON after {max_retries} attempts. Last error: {last_error}")


async def init_browser(url_or_html: str, is_html: bool = False):
    """
    Initializes a local Playwright browser (Chromium, headless).
    Returns the browser, context, and page objects.
    """
    global _playwright, _browser

    if _playwright is None:
        _playwright = await async_playwright().start()
    if _browser is None:
        _browser = await _playwright.chromium.launch(headless=True)
        
    context: BrowserContext = await _browser.new_context()
    page: Page = await context.new_page()

    # Block tracking/analytics scripts to speed up loading and prevent noise
    async def block_trackers(route):
        url = route.request.url.lower()
        blocked_domains = [
            "google-analytics.com", "googletagmanager.com", "clarity.ms",
            "hotjar.com", "mixpanel.com", "segment.com", "facebook.net/en_us/fbevents.js",
            "doubleclick.net", "sentry.io"
        ]
        if any(domain in url for domain in blocked_domains):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_trackers)

    # Native Playwright event listeners for error/network capture
    # Suppress native website console errors (e.g. Google Analytics CSP blocks) to reduce terminal noise
    # page.on("console", lambda msg: print(f"[Browser Console - {msg.type.upper()}] {msg.text}") if msg.type in ("error", "warning") else None)
    # Suppress harmless aborted request noise
    # page.on("requestfailed", lambda req: print(f"[Failed Request] {req.url} — {req.failure}"))

    # Auto-dismiss dialogs to prevent XSS payloads from blocking execution
    async def handle_dialog(dialog):
        print(f"[CRITICAL XSS] Unexpected dialog caught! Type: {dialog.type}, Message: {dialog.message}")
        await dialog.dismiss()

    page.on("dialog", lambda dialog: asyncio.create_task(handle_dialog(dialog)))

    if is_html:
        await page.set_content(url_or_html, wait_until="domcontentloaded")
    else:
        await page.goto(url_or_html, wait_until="networkidle")

    return page

async def close_browser():
    global _playwright, _browser
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
