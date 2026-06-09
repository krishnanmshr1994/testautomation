import json
from playwright.async_api import Page
from pydantic import BaseModel, Field
from typing import List
from src.browser_manager import ask_llm
from src.logger import stream_log

class TestIntent(BaseModel):
    description: str = Field(..., description="Natural language action to perform")
    expected_outcome: str = Field(..., description="Expected result after the action")
    is_security_probe: bool = Field(False, description="True if this injects a security payload")

class TestPlan(BaseModel):
    intents: List[TestIntent]

async def analyze_for_required_context(page: Page) -> str | None:
    """
    Analyzes the DOM to determine if user context/credentials are required for effective testing.
    Returns a prompt string to ask the user, or None if no context is needed.
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
You are an AI analyzing a webpage to determine if user input is necessary before automated testing begins.

Page Elements:
{json.dumps(dom_summary, indent=2)}

Does this page require specific user credentials (like a login form) or highly specific user selections to be properly tested?
If no, return {{"needs_input": false, "prompt_message": null}}.
If yes, return {{"needs_input": true, "prompt_message": "..."}} where prompt_message is a clear, concise question asking the user for the required information (e.g. "We detected a login form. Please provide the username and password to use for testing, or press Enter to skip and use random data:").

Respond ONLY with a JSON object in this exact format.
"""
    response_text = await ask_llm(prompt)
    try:
        data = json.loads(response_text.strip("```json\n").strip("```").strip())
        if data.get("needs_input") and data.get("prompt_message"):
            return data["prompt_message"]
    except Exception as e:
        await stream_log(f"[Warning] Failed to parse context analysis response: {e}")
    
    return None

async def generate_test_plan(page: Page, extra_context: str = "") -> TestPlan:
    """
    Extracts the DOM elements from the page and asks the LLM to generate a test plan.
    """
    await stream_log("\n--- Discovering Page Elements ---")

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

    await stream_log(f"Found {len(dom_summary)} interactive elements. Generating test plan...")

    prompt = f"""
You are a QA and Security testing expert. Based on the following page elements extracted from a website, generate a comprehensive test plan.

Page Elements:
{json.dumps(dom_summary, indent=2)}

Generate a JSON test plan with a list of test intents. Each intent must have:
- description: A specific natural language instruction of what to do (e.g. "Type 'admin' into the username field")
- expected_outcome: What should happen after the action
- is_security_probe: true if this is injecting XSS/SQLi payloads, false otherwise

Include:
1. Happy path tests (valid inputs)
2. Negative tests (invalid/empty inputs)
3. Security probes: inject <script>alert(1)</script> into text fields, and ' OR 1=1-- into form fields
"""
    if extra_context:
        prompt += f"""
IMPORTANT USER CONTEXT/CREDENTIALS TO USE:
{extra_context}
"""

    prompt += """
Respond ONLY with a JSON object in this exact format:
{
  "intents": [
    {"description": "...", "expected_outcome": "...", "is_security_probe": false},
    ...
  ]
}
"""
    response = await ask_llm(prompt)

    # Parse the JSON from the LLM response
    import re
    try:
        match = re.search(r'\{.*\}', response, re.DOTALL)
        cleaned = match.group(0) if match else response
        data = json.loads(cleaned)
        plan = TestPlan(**data)
    except Exception as e:
        await stream_log(f"[Warning] Could not parse test plan JSON. Error: {e}\nRaw Response: {response[:200]}")
        plan = TestPlan(intents=[
            TestIntent(description="Observe the page", expected_outcome="Page loads successfully", is_security_probe=False)
        ])

    await stream_log(f"Generated {len(plan.intents)} test intents.")
    return plan
