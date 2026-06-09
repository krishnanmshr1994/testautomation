import os
import asyncio
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from openai import AsyncOpenAI

# Global references
_playwright = None
_browser: Browser = None

# Shared AI client using NVIDIA endpoint
def get_ai_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=os.getenv("NVIDIA_API_KEY"),
    )

async def ask_llm(prompt: str, system: str = "You are a QA and Security testing expert.") -> str:
    """Helper to send a prompt to the LLM and get a text response."""
    client = get_ai_client()
    try:
        response = await client.chat.completions.create(
            model=os.getenv("MODEL_NAME", "deepseek-ai/deepseek-v4-pro"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            top_p=0.95,
            max_tokens=8192,
            extra_body={"chat_template_kwargs": {"thinking": False}},
            timeout=120.0
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"\n[LLM Error] API request failed or timed out: {e}")
        return "{}"

async def init_browser(url_or_html: str, is_html: bool = False):
    """
    Initializes a local Playwright browser (Chromium, headless).
    Returns the browser, context, and page objects.
    """
    global _playwright, _browser

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)
    context: BrowserContext = await _browser.new_context()
    page: Page = await context.new_page()

    # Native Playwright event listeners for error/network capture
    page.on("console", lambda msg: print(f"[Browser Console - {msg.type.upper()}] {msg.text}") if msg.type in ("error", "warning") else None)
    page.on("requestfailed", lambda req: print(f"[Failed Request] {req.url} — {req.failure}"))

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
