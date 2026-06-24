import json
from playwright.async_api import Page
from pydantic import BaseModel, Field
from typing import List, Optional
from src.browser_manager import ask_llm
from src.logger import stream_log

class TestIntent(BaseModel):
    description: str = Field(..., description="Natural language action to perform")
    expected_outcome: str = Field(..., description="Expected result after the action")
    is_security_probe: bool = Field(False, description="True if this injects a security payload")
    attack_type: Optional[str] = Field(None, description="The category of attack (e.g. XSS, SQLi) if this is a security probe")
    press_enter_after_fill: bool = Field(False, description="Set to true ONLY if there is no submit button and Enter is required to submit.")

class TestPlan(BaseModel):
    intents: List[TestIntent]

async def analyze_for_required_context(page: Page) -> dict | None:
    """
    Analyzes the DOM to determine if user context is required for effective testing.
    Returns a dict with {prompt_message, field_label, placeholder} or None if no context needed.
    """
    await stream_log("\n--- Analyzing Page for Required Context ---")
    
    # Extract key DOM elements
    dom_summary = await page.evaluate("""() => {
        const elements = [];
        document.querySelectorAll('input, button, select, form').forEach(el => {
            elements.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || null,
                name: el.name || null,
                id: el.id || null,
                placeholder: el.placeholder || null,
                text: el.innerText?.trim().substring(0, 50) || null
            });
        });
        return elements;
    }""")

    from src.prompts import PLANNER_CONTEXT_PROMPT
    prompt = PLANNER_CONTEXT_PROMPT.format(
        dom_summary_json=json.dumps(dom_summary, indent=2)
    )
    from src.browser_manager import ask_llm_json_with_healing
    try:
        data = await ask_llm_json_with_healing(prompt, max_retries=2)
        if data.get("needs_input"):
            return {
                "prompt_message": data.get("prompt_message", ""),
                "field_label": data.get("field_label", "Required Input"),
                "placeholder": data.get("placeholder", "")
            }
    except Exception as e:
        await stream_log(f"[Warning] Failed to parse context analysis response: {e}")
    
    return None


async def perform_login(page: Page, credentials_text: str) -> bool:
    """
    Attempts to perform a login on the current page using the provided credentials.
    Parses username/password from free-form text (e.g. "username: admin, password: secret"),
    uses the LLM to identify the correct selectors, fills the form, and submits it.
    Returns True if the URL changed after submission (indicating successful navigation),
    False otherwise.
    """
    import re
    await stream_log("[Login] Parsing credentials and identifying login form fields...")

    # ── Parse credentials from free-form text ─────────────────────────────────
    def extract_field(text: str, *keys) -> str | None:
        for key in keys:
            match = re.search(rf'(?i){re.escape(key)}\s*[:\-=]\s*([^\s,|;]+)', text)
            if match:
                return match.group(1).strip()
        return None

    username = extract_field(credentials_text, "username", "user", "email", "login", "u")
    password = extract_field(credentials_text, "password", "pass", "pwd", "p")

    if not username and not password:
        await stream_log("[Login] Could not parse credentials from input. Skipping login.")
        return False

    await stream_log(f"[Login] Parsed → username: {username or '(none)'}, password: {'*' * len(password) if password else '(none)'}")

    # ── Get the current DOM to find login field selectors ────────────────────
    from src.browser_manager import distill_dom, ask_llm_fast_json_with_healing

    dom_snapshot = await distill_dom(page)
    login_selector_prompt = f"""Given this HTML:
{dom_snapshot}

Identify the CSS selectors for the username/email field, password field, and submit/login button.
Respond ONLY with JSON:
{{
  "username_selector": "<css-selector or null>",
  "password_selector": "<css-selector or null>",
  "submit_selector":   "<css-selector or null>"
}}
If a field doesn't exist, use null."""

    try:
        selectors = await ask_llm_fast_json_with_healing(
            login_selector_prompt,
            system="You are a Playwright automation expert. Respond only with JSON.",
            max_retries=2
        )
    except Exception as e:
        await stream_log(f"[Login] Could not identify login selectors: {e}")
        return False

    username_sel = selectors.get("username_selector")
    password_sel = selectors.get("password_selector")
    submit_sel   = selectors.get("submit_selector")

    await stream_log(f"[Login] Selectors → user: {username_sel}, pass: {password_sel}, btn: {submit_sel}")

    pre_login_url = page.url

    # ── Fill fields and submit ────────────────────────────────────────────────
    from src.settings_loader import get_timeout_settings
    timeouts = get_timeout_settings()
    nav_timeout = timeouts.get("page_navigation", 10000)

    try:
        if username and username_sel:
            # Use press_sequentially instead of fill. 
            # Complex apps (Salesforce, React) often rely on keyup/keydown events to update internal state.
            # Instant fill can bypass these, causing client-side validation to block submission.
            await page.locator(username_sel).press_sequentially(username, delay=50, timeout=5000)
            
        if password and password_sel:
            await page.locator(password_sel).press_sequentially(password, delay=50, timeout=5000)

        # Small pause before submitting to mimic human behavior
        await asyncio.sleep(0.5)

        if submit_sel:
            await page.click(submit_sel, timeout=5000)
        else:
            # No button found — press Enter on the password field
            if password_sel:
                await page.press(password_sel, "Enter")

        await page.wait_for_load_state("networkidle", timeout=nav_timeout)
    except Exception as e:
        await stream_log(f"[Login] Error during form submission: {e}")
        return False

    post_login_url = page.url
    
    # Check if we are still on the login page (e.g. invalid credentials or anti-bot challenge)
    try:
        # If the password field is still visible, the login definitely failed
        if password_sel and await page.is_visible(password_sel, timeout=2000):
            await stream_log(f"[Login] ❌ Login failed! The login form is still present on the page. Check credentials or Salesforce IP restrictions.")
            return False
    except Exception:
        pass

    if post_login_url != pre_login_url:
        await stream_log(f"[Login] ✅ Login successful — navigated away from login form to: {post_login_url}")
        return True
    else:
        await stream_log(f"[Login] ⚠️ URL unchanged after submission — login may have failed.")
        return False

CUSTOM_TEST_FORMAT = """
Test: <what to do in plain English>
Expected: <what should happen>
"""

def parse_custom_tests(raw: str) -> list[TestIntent]:
    """
    Parses user-defined test cases from plain text.

    Accepted format (one or more blocks):
        Test: Click the login button
        Expected: The login form is displayed

    Lines starting with # are treated as comments and ignored.
    """
    intents = []
    description = None
    expected = None

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("test:"):
            description = line[5:].strip()
        elif line.lower().startswith("expected:"):
            expected = line[9:].strip()

        if description and expected:
            intents.append(TestIntent(
                description=description,
                expected_outcome=expected,
                is_security_probe=False
            ))
            description = None
            expected = None

    return intents


async def generate_test_plan(page: Page,
                             extra_context: str = "",
                             custom_tests_raw: str = "",
                             run_functional: bool = True,
                             run_probes: bool = True,
                             global_tested_elements: set = None) -> TestPlan:
    """
    Extracts the DOM elements from the page and asks the LLM to generate a test plan.
    If custom_tests_raw is provided, those intents are prepended to the AI-generated ones.
    Respects run_functional and run_probes flags.
    """
    await stream_log("\n--- Discovering Page Elements ---")

    # Parse user-defined test cases first
    custom_intents = []
    if custom_tests_raw and custom_tests_raw.strip():
        custom_intents = parse_custom_tests(custom_tests_raw)
        await stream_log(f"[Custom Tests] {len(custom_intents)} user-defined test case(s) added.")

    if not run_functional and not run_probes:
        await stream_log("--- Skipping AI Test Generation (Disabled by User) ---")
        return TestPlan(intents=custom_intents)

    # Extract key DOM elements using Playwright
    dom_summary = await page.evaluate("""() => {
        const elements = [];
        document.querySelectorAll('input, button, a, select, textarea, form').forEach(el => {
            elements.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || null,
                name: el.name || null,
                id: el.id || null,
                placeholder: el.placeholder || null,
                text: el.innerText?.trim().substring(0, 50) || null,
                href: el.href || null
            });
        });
        return elements;
    }""")

    total_found = len(dom_summary)

    # De-duplicate and filter blanks, then cap at 80 elements to prevent token overflow
    # on large pages (300+ elements). Priority: inputs/selects/textareas > buttons > links > forms
    MAX_ELEMENTS = 80
    def element_priority(el):
        tag = el.get('tag', '')
        if tag in ('input', 'select', 'textarea'): return 0
        if tag == 'button': return 1
        if tag == 'a': return 2
        return 3

    seen = set()
    deduped = []
    for el in sorted(dom_summary, key=element_priority):
        key = (el.get('tag'), el.get('type'), el.get('name'), el.get('id'), el.get('text'), el.get('href'))
        
        if global_tested_elements is not None and key in global_tested_elements:
            continue
            
        if key not in seen and any(v for v in el.values() if v):
            seen.add(key)
            if global_tested_elements is not None:
                global_tested_elements.add(key)
            deduped.append(el)

    if len(deduped) > MAX_ELEMENTS:
        await stream_log(f"Found {total_found} interactive elements (capped to {MAX_ELEMENTS} most relevant). Generating test plan...")
        dom_summary = deduped[:MAX_ELEMENTS]
    else:
        await stream_log(f"Found {total_found} interactive elements. Generating test plan...")
        dom_summary = deduped

    tests_to_run = ""
    if run_functional:
        tests_to_run += "1. Happy path tests (valid inputs)\n2. Negative tests (invalid/empty inputs)\n"
    if run_probes:
        tests_to_run += "3. Security probes: Map vulnerable-looking fields to an appropriate `attack_type`. DO NOT generate the specific payload string (e.g. don't write '<script>'). The executor will dynamically fetch payloads from a deep-scan dataset based on the `attack_type` you assign.\n"

    extra_context_instruction = ""
    if extra_context:
        extra_context_instruction = f"""
IMPORTANT — The user has provided the following context/credentials to use in tests:
{extra_context}
Use this information for the happy-path tests.
"""
    elif run_functional:
        extra_context_instruction = """
IMPORTANT — No credentials or context were provided by the user.
You MUST still generate realistic, concrete test cases using sensible default values appropriate to the detected form type. Examples:
- Login forms: use username "testuser" and password "TestPass@123"
- Search fields: use query "test product"
- Credit card fields: use card "4111111111111111", CVV "123", Expiry "12/28"
- Email fields: use "tester@example.com"
- Address fields: use "123 Test Street, New York, NY 10001"
Choose defaults that make sense for the detected context.
"""

    from src.prompts import PLANNER_PLAN_PROMPT_BASE
    prompt = PLANNER_PLAN_PROMPT_BASE.format(
        dom_summary_json=json.dumps(dom_summary, indent=2),
        tests_to_run=tests_to_run,
        extra_context_instruction=extra_context_instruction
    )
    from src.browser_manager import ask_llm_json_with_healing
    try:
        ai_plan = await ask_llm_json_with_healing(
            prompt,
            pydantic_model=TestPlan,
            max_retries=3
        )
    except Exception as e:
        await stream_log(f"[Warning] Could not parse test plan JSON after retries. Error: {e}")
        ai_plan = TestPlan(intents=[])

    # Merge: user-defined tests FIRST, then AI-generated
    merged_intents = custom_intents + ai_plan.intents
    plan = TestPlan(intents=merged_intents)

    await stream_log(f"Generated {len(plan.intents)} test intents "
                     f"({len(custom_intents)} custom + {len(ai_plan.intents)} AI-generated).")
    return plan
