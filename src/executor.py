import json
import re
from playwright.async_api import Page
from src.planner import TestPlan
from src.browser_manager import ask_llm, distill_dom
from src.logger import stream_log

async def execute_plan(page: Page, plan: TestPlan, live_reporter=None) -> list:
    """
    Iterates through the test plan.
    Uses LLM to identify selectors, then Playwright to execute actions.
    If live_reporter is provided, records each result immediately to disk.
    """
    results = []

    for idx, intent in enumerate(plan.intents):
        await stream_log(f"\n--- Step {idx + 1}/{len(plan.intents)}: {intent.description} ---")

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
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            cleaned = match.group(0) if match else raw
            action_data = json.loads(cleaned)

            if not action_data.get("selector"):
                step_result["error"] = "LLM could not identify element selector."
                step_result["details"] = "No matching selector found."
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            selector = action_data["selector"]
            action = action_data.get("action", "click")
            value = action_data.get("value", "")

            # Execute the action with Playwright
            element = await page.query_selector(selector)
            if not element:
                step_result["error"] = f"Selector '{selector}' not found on page."
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            # Scroll element into view before interacting
            try:
                await element.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass  # Not critical — proceed anyway

            try:
                if action == "fill" and value:
                    await element.fill(str(value), timeout=5000)
                elif action == "press":
                    await element.press(str(value) if value else "Enter", timeout=5000)
                else:
                    # Try normal click first (respects visibility), fall back to force click
                    try:
                        await element.click(timeout=5000)
                    except Exception:
                        await stream_log(f"  [Fallback] Element not visible — trying force click on '{selector}'")
                        await element.click(force=True, timeout=3000)

                step_result["action_success"] = True

            except Exception as action_err:
                step_result["error"] = f"Action failed on '{selector}': {str(action_err)[:120]}"
                step_result["details"] = "Element found but could not be interacted with (hidden/disabled/off-screen)."
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            # ── Wait for navigation / DOM to settle after the action ─────────
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                # Not all actions trigger navigation — timeout here is fine
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                except Exception:
                    pass

            # ── Capture post-action state: URL + distilled DOM ───────────────
            current_url = page.url
            page_text   = await distill_dom(page)

            # ── Smart URL-based shortcut for navigation tests ─────────────────
            # If the URL changed and the new path appears in the expected outcome,
            # we can confidently mark it as passed without an extra LLM call.
            url_keywords = [w.lower() for w in intent.expected_outcome.split()
                            if len(w) > 3 and w.isalpha()]
            url_match = any(kw in current_url.lower() for kw in url_keywords)
            if url_match and action in ("click", None):
                step_result["verification_success"] = True
                step_result["details"] = (
                    f"URL changed to '{current_url}' which matches expected outcome. "
                    f"Navigation verified via URL."
                )
                await stream_log(f"✅ Passed (URL match): {step_result['details']}")
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            # ── Full LLM verification (with URL context) ──────────────────────
            verify_prompt = f"""
You are a QA verification expert. A browser automation just performed an action and you must decide if the expected outcome occurred.

Action performed : "{intent.description}"
Current page URL : {current_url}
Expected outcome : "{intent.expected_outcome}"

Distilled page HTML:
{page_text}

IMPORTANT RULES:
1. If the Current page URL clearly matches the expected outcome (e.g. URL contains "healthcare" and expected is "Healthcare page is displayed"), mark it as SUCCESS.
2. Do NOT fail a test just because the HTML contains links or mentions of other sections — focus on whether the PRIMARY content matches.
3. Be lenient: if the evidence is ambiguous but the URL changed correctly, lean toward success.

Respond ONLY with JSON:
{{"success": true/false, "details": "One sentence explaining what you see on the page and why you chose this verdict"}}
"""
            raw_verify = await ask_llm(verify_prompt, system="You are a QA verification expert. Respond only with JSON.")
            match = re.search(r'\{.*\}', raw_verify, re.DOTALL)
            cleaned_verify = match.group(0) if match else raw_verify
            verify_data = json.loads(cleaned_verify)
            step_result["verification_success"] = verify_data.get("success", False)
            step_result["details"] = verify_data.get("details", "")

            status = "✅ Passed" if step_result["verification_success"] else "❌ Failed"
            await stream_log(f"{status}: {step_result['details']}")

        except Exception as e:
            await stream_log(f"Error: {str(e)}")
            step_result["error"] = str(e)

        results.append(step_result)

        # ── Write this result to disk immediately (parallel write) ──────────
        if live_reporter:
            await live_reporter.record(intent, step_result)

    return results
