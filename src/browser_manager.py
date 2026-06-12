import os
import asyncio
import httpx
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from openai import AsyncOpenAI

# Reasoning model: used for audit, planning, context analysis
# Set MODEL_NAME in .env to override
_REASONING_MODEL_DEFAULT = "poolside/laguna-m.1:free"

# Fast model: used for selector identification and verification in the execution loop
# Set FAST_MODEL_NAME in .env to override
_FAST_MODEL_DEFAULT = "meta-llama/llama-3.3-70b-instruct:free"

# Global references
_playwright = None
_browser: Browser = None

# Global LLM concurrency throttle.
# All OpenRouter calls (reasoning + fast, across all parallel pages and audit passes)
# must acquire this semaphore before firing. This prevents thundering-herd 429s.
# Value: max simultaneous in-flight API requests to OpenRouter.
_LLM_CONCURRENCY = 2
_llm_semaphore: asyncio.Semaphore | None = None

def _get_llm_semaphore() -> asyncio.Semaphore:
    """Lazily initialise the global semaphore inside the running event loop."""
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)
    return _llm_semaphore

def get_ai_client() -> AsyncOpenAI:
    if os.getenv("OPENROUTER_API_KEY"):
        return AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
    if os.getenv("NVIDIA_API_KEY"):
        return AsyncOpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.getenv("NVIDIA_API_KEY"),
        )
    if os.getenv("GEMINI_API_KEY"):
        return AsyncOpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.getenv("GEMINI_API_KEY"),
        )
    if os.getenv("GROQ_API_KEY"):
        return AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
        )
    if os.getenv("HF_TOKEN"):
        return AsyncOpenAI(
            base_url="https://api-inference.huggingface.co/v1/",
            api_key=os.getenv("HF_TOKEN"),
        )
    if os.getenv("GITHUB_TOKEN"):
        return AsyncOpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=os.getenv("GITHUB_TOKEN"),
        )
    # Fallback
    return AsyncOpenAI(
        base_url="https://api.openai.com/v1",
        api_key="dummy_key",
    )

async def distill_dom(page) -> str:
    """
    Executes a JavaScript minifier in the browser to strip out non-semantic
    elements (scripts, styles, svgs, classes) and returns a clean HTML skeleton.
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


async def _call_openrouter(messages: list, model: str, temperature: float, max_tokens: int, use_reasoning: bool) -> tuple[str, str | None]:
    """Internal: makes a direct httpx POST to OpenRouter. Shared by both reasoning and fast paths."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-OpenRouter-Title": "AI QA Security Automation"
    }
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": 0.7,
        "max_tokens": max_tokens,
    }
    if use_reasoning:
        payload["reasoning"] = {"enabled": True}

    timeout_per_request = 30.0 if use_reasoning else 15.0
    max_attempts = 5
    backoff_factor = 2.0
    
    from src.logger import stream_log
    semaphore = _get_llm_semaphore()
    
    for attempt in range(max_attempts):
        # Acquire global throttle before every attempt to cap concurrent API calls
        async with semaphore:
          try:
              async with httpx.AsyncClient(timeout=timeout_per_request) as client:
                  response = await client.post(
                      "https://openrouter.ai/api/v1/chat/completions",
                      headers=headers,
                      json=payload
                  )
              
              # Check for rate limit 429
              if response.status_code == 429:
                  retry_after = response.headers.get("Retry-After")
                  try:
                      sleep_time = float(retry_after) if retry_after else backoff_factor * (2 ** attempt)
                  except ValueError:
                      sleep_time = backoff_factor * (2 ** attempt)
                  
                  await stream_log(
                      f"[Rate Limit] OpenRouter 429 for model '{model}'. "
                      f"Retrying in {sleep_time:.1f}s (Attempt {attempt+1}/{max_attempts})..."
                  )
                  # Release semaphore while sleeping so other callers aren't blocked
                  await asyncio.sleep(sleep_time)
                  continue
              
              # Check for server errors
              if response.status_code in (502, 503, 504):
                  sleep_time = backoff_factor * (2 ** attempt)
                  await stream_log(
                      f"[Server Error] OpenRouter returned {response.status_code} for model '{model}'. "
                      f"Retrying in {sleep_time:.1f}s (Attempt {attempt+1}/{max_attempts})..."
                  )
                  await asyncio.sleep(sleep_time)
                  continue

              response.raise_for_status()
              data = response.json()
              
              # Check if OpenRouter returned an embedded error inside a 200 OK response
              if "choices" not in data or not data["choices"]:
                  if "error" in data:
                      err_msg = data["error"].get("message", "Unknown OpenRouter API error")
                      code = data["error"].get("code")
                      if code == 429 or "rate limit" in err_msg.lower() or "too many requests" in err_msg.lower():
                          sleep_time = backoff_factor * (2 ** attempt)
                          await stream_log(
                              f"[Rate Limit] OpenRouter inline error '{err_msg}'. "
                              f"Retrying in {sleep_time:.1f}s (Attempt {attempt+1}/{max_attempts})..."
                          )
                          await asyncio.sleep(sleep_time)
                          continue
                      raise ValueError(f"OpenRouter Error: {err_msg}")
                  raise ValueError(f"Unexpected OpenRouter response format: {data}")
              
              message_data = data["choices"][0]["message"]
              content = message_data.get("content") or ""
              reasoning_details = message_data.get("reasoning_details")
              return content, reasoning_details

          except httpx.RequestError as req_err:
              # Handle client-side network errors, connection timeouts, etc.
              sleep_time = backoff_factor * (2 ** attempt)
              await stream_log(
                  f"[Network Error] {type(req_err).__name__} for model '{model}': {req_err}. "
                  f"Retrying in {sleep_time:.1f}s (Attempt {attempt+1}/{max_attempts})..."
              )
              await asyncio.sleep(sleep_time)
              continue
            
    # If we get here, all attempts failed
    raise RuntimeError(f"Failed to call OpenRouter model '{model}' after {max_attempts} attempts.")


async def ask_llm(prompt: str = None, system: str = "You are a QA and Security testing expert.", temperature: float = 0.2, messages: list = None) -> tuple[str, str | None]:
    """
    Reasoning model call (poolside/laguna-m.1 by default).
    Used for complex tasks: audit, test plan generation, context analysis.
    Automatically falls back to the fast model on timeout.
    """
    try:
        if os.getenv("OPENROUTER_API_KEY"):
            model_name = os.getenv("MODEL_NAME", _REASONING_MODEL_DEFAULT)
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            try:
                # 45-second hard cap on reasoning model
                return await asyncio.wait_for(
                    _call_openrouter(messages, model_name, temperature, max_tokens=8192, use_reasoning=True),
                    timeout=45.0
                )
            except asyncio.TimeoutError:
                from src.logger import stream_log
                await stream_log(f"[Timeout] Reasoning model timed out after 45s. Falling back to fast model...")
                return await ask_llm_fast(messages=messages, temperature=temperature)
        else:
            # Non-OpenRouter path (Gemini, Groq, etc.)
            client = get_ai_client()
            if os.getenv("NVIDIA_API_KEY"):    default_model = "meta/llama-3.3-70b-instruct"
            elif os.getenv("GEMINI_API_KEY"): default_model = "gemini-2.5-flash"
            elif os.getenv("GROQ_API_KEY"):   default_model = "llama-3.3-70b-versatile"
            elif os.getenv("HF_TOKEN"):        default_model = "Qwen/Qwen2.5-72B-Instruct"
            elif os.getenv("GITHUB_TOKEN"):    default_model = "gpt-4o-mini"
            else:                              default_model = "meta/llama-3.3-70b-instruct"
            model_name = os.getenv("MODEL_NAME", default_model)
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            response = await client.chat.completions.create(
                model=model_name, messages=messages,
                temperature=temperature, top_p=0.7, max_tokens=4096, timeout=120.0
            )
            return response.choices[0].message.content or "", None
    except asyncio.TimeoutError:
        raise
    except Exception as e:
        from src.logger import stream_log
        await stream_log(f"\n[LLM Error] Reasoning model failed: {e}")
        raise e


async def ask_llm_fast(prompt: str = None, system: str = "You are a QA and Security testing expert.", temperature: float = 0.1, messages: list = None) -> tuple[str, str | None]:
    """
    Fast model call (meta-llama/llama-3.3-70b-instruct by default).
    Used for simple tasks: CSS selector identification, pass/fail verification.
    No reasoning — optimised for speed (1-5s response time).
    """
    try:
        if os.getenv("OPENROUTER_API_KEY"):
            model_name = os.getenv("FAST_MODEL_NAME", _FAST_MODEL_DEFAULT)
            if messages is None:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            return await asyncio.wait_for(
                _call_openrouter(messages, model_name, temperature, max_tokens=1024, use_reasoning=False),
                timeout=30.0
            )
        else:
            # Fallback to the standard reasoning model path if no OpenRouter key
            return await ask_llm(prompt=prompt, system=system, temperature=temperature, messages=messages)
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
            match = re.search(r'\{.*\}', content, re.DOTALL)
            cleaned = match.group(0) if match else content
            data = json.loads(cleaned)
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
            match = re.search(r'\{.*\}', content, re.DOTALL)
            cleaned = match.group(0) if match else content
            data = json.loads(cleaned)
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
