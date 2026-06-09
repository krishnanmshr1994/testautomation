import json
from playwright.async_api import Page
from pydantic import BaseModel, Field
from typing import List
from src.browser_manager import ask_llm

class TestIntent(BaseModel):
    description: str = Field(..., description="Natural language action to perform")
    expected_outcome: str = Field(..., description="Expected result after the action")
    is_security_probe: bool = Field(False, description="True if this injects a security payload")

class TestPlan(BaseModel):
    intents: List[TestIntent]

async def generate_test_plan(page: Page, extra_context: str = "") -> TestPlan:
    """
    Extracts the DOM elements from the page and asks the LLM to generate a test plan.
    """
    print("\n--- Discovering Page Elements ---")

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

    print(f"Found {len(dom_summary)} interactive elements. Generating test plan...")

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
    try:
        # Strip markdown code fences if present
        cleaned = response.strip().strip("```json").strip("```").strip()
        data = json.loads(cleaned)
        plan = TestPlan(**data)
    except Exception as e:
        print(f"Warning: Could not parse test plan JSON, using fallback. Error: {e}")
        plan = TestPlan(intents=[
            TestIntent(description="Observe the page", expected_outcome="Page loads successfully", is_security_probe=False)
        ])

    print(f"Generated {len(plan.intents)} test intents.")
    return plan
