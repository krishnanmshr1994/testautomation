import os
import asyncio
from stagehand import Stagehand, StagehandConfig

async def init_browser(url_or_html: str, is_html: bool = False):
    """
    Initializes the Stagehand client and navigates to the given URL or sets the HTML content.
    Returns the stagehand instance and the page object.
    """
    # Assuming local execution or playwright backend based on environment
    env_mode = os.getenv("STAGEHAND_ENV", "LOCAL")
    
    config = StagehandConfig(
        env=env_mode,
        model_name=os.getenv("MODEL_NAME", "deepseek-ai/deepseek-v4-pro"),
        model_client_options={
            "baseURL": "https://integrate.api.nvidia.com/v1",
            "apiKey": os.getenv("NVIDIA_API_KEY")
        }
    )
    
    stagehand = Stagehand(config=config)
    await stagehand.init()
    
    page = stagehand.page
    
    # Setup native Playwright event listeners for logging
    page.on("console", lambda msg: print(f"Browser Console [{msg.type}]: {msg.text}"))
    page.on("requestfailed", lambda req: print(f"Failed Request: {req.url} - {req.failure}"))
    
    # Auto-dismiss dialogs to prevent blocking execution during XSS testing
    async def handle_dialog(dialog):
        print(f"CRITICAL: Unexpected Dialog caught (Type: {dialog.type}). Message: {dialog.message}")
        await dialog.dismiss()
        
    page.on("dialog", lambda dialog: asyncio.create_task(handle_dialog(dialog)))
    
    if is_html:
        await page.set_content(url_or_html)
    else:
        await page.goto(url_or_html, wait_until="networkidle")
        
    return stagehand, page

async def close_browser(stagehand: Stagehand):
    await stagehand.close()
