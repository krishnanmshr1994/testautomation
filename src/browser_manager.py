import os
import asyncio
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from openai import AsyncOpenAI

# Global references
_playwright = None
_browser: Browser = None

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


async def ask_llm(prompt: str = None, system: str = "You are a QA and Security testing expert.", temperature: float = 0.2, messages: list = None) -> tuple[str, str | None]:
    """Helper to send a prompt or a list of messages to the LLM and get the content and reasoning details."""
    client = get_ai_client()
    try:
        if os.getenv("OPENROUTER_API_KEY"):
            default_model = "poolside/laguna-m.1:free"
        elif os.getenv("NVIDIA_API_KEY"):
            default_model = "meta/llama-3.3-70b-instruct"
        elif os.getenv("GEMINI_API_KEY"):
            default_model = "gemini-2.5-flash"
        elif os.getenv("GROQ_API_KEY"):
            default_model = "llama-3.3-70b-versatile"
        elif os.getenv("HF_TOKEN"):
            default_model = "Qwen/Qwen2.5-72B-Instruct"
        elif os.getenv("GITHUB_TOKEN"):
            default_model = "gpt-4o-mini"
        else:
            default_model = "meta/llama-3.3-70b-instruct"
            
        model_name = os.getenv("MODEL_NAME", default_model)

        if messages is None:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ]

        if os.getenv("OPENROUTER_API_KEY"):
            import httpx
            headers = {
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
                "top_p": 0.7,
                "max_tokens": 8192,
                "reasoning": {"enabled": True}
            }
            async with httpx.AsyncClient(timeout=120.0) as httpx_client:
                response = await httpx_client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                response_json = response.json()
            message_data = response_json["choices"][0]["message"]
            content = message_data.get("content") or ""
            reasoning_details = message_data.get("reasoning_details")
            return content, reasoning_details
        else:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                top_p=0.7,
                max_tokens=4096,
                timeout=120.0
            )
            content = response.choices[0].message.content or ""
            return content, None
    except Exception as e:
        from src.logger import stream_log
        await stream_log(f"\n[LLM Error] API request failed or timed out: {e}")
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
