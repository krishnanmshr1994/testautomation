import json
from playwright.async_api import Page
from src.planner import TestPlan
from src.browser_manager import ask_llm, distill_dom

async def execute_plan(page: Page, plan: TestPlan) -> list:
    """
    Iterates through the test plan.
    Uses LLM to identify selectors, then Playwright to execute actions.
    """
    results = []

    for idx, intent in enumerate(plan.intents):
        print(f"\n--- Step {idx + 1}/{len(plan.intents)}: {intent.description} ---")

        step_result = {
            "intent": intent.model_dump(),
            "action_success": False,
            "verification_success": False,
            "error": None,
            "details": ""
        }

        try:
            # Ask LLM to identify the CSS selector for the intended action
            dom_snapshot = await distill_dom(page)
            selector_prompt = f"""
Given this distilled HTML snippet:
{dom_snapshot}

For the following action: "{intent.description}"

Respond ONLY with a valid JSON object in this exact format (no explanation):
{{"selector": "<css-selector>", "action": "click|fill|press", "value": "<optional value to type>"}}

If you cannot identify the element, respond with: {{"selector": null, "action": null, "value": null}}
"""
            raw = await ask_llm(selector_prompt, system="You are a Playwright automation expert. Respond only with JSON.")
            cleaned = raw.strip().strip("```json").strip("```").strip()
            action_data = json.loads(cleaned)

            if not action_data.get("selector"):
                step_result["error"] = "LLM could not identify element selector."
                step_result["details"] = "No matching selector found."
                results.append(step_result)
                continue

            selector = action_data["selector"]
            action = action_data.get("action", "click")
            value = action_data.get("value", "")

            # Execute the action with Playwright
            element = await page.query_selector(selector)
            if not element:
                step_result["error"] = f"Selector '{selector}' not found on page."
                results.append(step_result)
                continue

            if action == "fill" and value:
                await element.fill(str(value))
            elif action == "press":
                await element.press(str(value) if value else "Enter")
            else:
                await element.click()

            step_result["action_success"] = True

            # Verify outcome: ask LLM to check if the expected outcome occurred
            page_text = await distill_dom(page)
            verify_prompt = f"""
After performing: "{intent.description}"

The distilled page HTML now shows:
{page_text}

Expected outcome: "{intent.expected_outcome}"

Did the expected outcome occur? Respond ONLY with JSON:
{{"success": true/false, "details": "Brief explanation"}}
"""
            raw_verify = await ask_llm(verify_prompt, system="You are a QA verification expert. Respond only with JSON.")
            cleaned_verify = raw_verify.strip().strip("```json").strip("```").strip()
            verify_data = json.loads(cleaned_verify)
            step_result["verification_success"] = verify_data.get("success", False)
            step_result["details"] = verify_data.get("details", "")

            status = "✅ Passed" if step_result["verification_success"] else "❌ Failed"
            print(f"{status}: {step_result['details']}")

        except Exception as e:
            print(f"Error: {str(e)}")
            step_result["error"] = str(e)

        results.append(step_result)

    return results
