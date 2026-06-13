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

    prompt = f"""
You are an AI analyzing a webpage to determine if specific user input is necessary before automated testing begins.

Page Elements:
{json.dumps(dom_summary, indent=2)}

Does this page require specific user input (like login credentials, payment info, OTP, address, etc.) to be properly tested?

If NO user input is needed, return:
{{"needs_input": false, "prompt_message": null, "field_label": null, "placeholder": null}}

If YES, return:
{{
  "needs_input": true,
  "prompt_message": "<A clear sentence explaining what was detected and why input is needed>",
  "field_label": "<Short label describing exactly what is needed, e.g. 'Login Credentials', 'Credit Card Details', 'Shipping Address', 'OTP / Verification Code'>",
  "placeholder": "<Example of what to type, e.g. 'username: admin, password: secret' or 'Card: 4111111111111111, CVV: 123, Exp: 12/26'>"
}}

Respond ONLY with a valid JSON object.
"""
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

    prompt = f"""
You are a QA and Security testing expert. Based on the following page elements extracted from a website, generate a comprehensive test plan.

Page Elements:
{json.dumps(dom_summary, indent=2)}

Generate a JSON test plan with a list of test intents. Each intent must have:
- description: A specific natural language instruction of what to do (e.g. "Type 'admin' into the username field")
- expected_outcome: What should happen after the action
- is_security_probe: true if this is a security injection test, false otherwise
- attack_type: If it IS a security probe, specify the attack class (e.g., "XSS", "SQLi", "SSRF", "SSTI", "LFI", "CommandInjection"). Otherwise, leave null.
- press_enter_after_fill: By default, you MUST set this to false. You are ONLY allowed to set it to true if you are 100% certain that the field is a standalone text input (like a search bar) AND there are absolutely zero actionable buttons (Save, Submit, Search, Go) available on the form. If you are unsure, set it to false.

CRITICAL: DO NOT generate passive security/audit test cases that do not target specific, interactive page elements (such as testing for missing HTTP security headers, clickjacking/X-Frame-Options, SSL certificates, cookies, or port scanning). These passive checks are already handled in a separate static audit phase. Every test intent you generate MUST interact with one of the extracted page elements (e.g., input fields, links, buttons) via click or fill.

Include the following types of tests based on the user's request:
"""
    if run_functional:
        prompt += "1. Happy path tests (valid inputs)\n2. Negative tests (invalid/empty inputs)\n"
    if run_probes:
        prompt += "3. Security probes: Map vulnerable-looking fields to an appropriate `attack_type`. DO NOT generate the specific payload string (e.g. don't write '<script>'). The executor will dynamically fetch payloads from a deep-scan dataset based on the `attack_type` you assign.\n"

    if extra_context:
        prompt += f"""
IMPORTANT — The user has provided the following context/credentials to use in tests:
{extra_context}
Use this information for the happy-path tests.
"""
    elif run_functional:
        prompt += """
IMPORTANT — No credentials or context were provided by the user.
You MUST still generate realistic, concrete test cases using sensible default values appropriate to the detected form type. Examples:
- Login forms: use username "testuser" and password "TestPass@123"
- Search fields: use query "test product"
- Credit card fields: use card "4111111111111111", CVV "123", Expiry "12/28"
- Email fields: use "tester@example.com"
- Address fields: use "123 Test Street, New York, NY 10001"
Choose defaults that make sense for the detected context.
"""

    prompt += """
Respond ONLY with a JSON object in this exact format:
{
  "intents": [
    {"description": "...", "expected_outcome": "...", "is_security_probe": false, "attack_type": null, "press_enter_after_fill": false},
    {"description": "Inject XSS into search field", "expected_outcome": "Application blocks or sanitizes the payload", "is_security_probe": true, "attack_type": "XSS", "press_enter_after_fill": true}
  ]
}
"""
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
