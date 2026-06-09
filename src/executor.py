import json
import re
from playwright.async_api import Page
from src.planner import TestPlan
from src.browser_manager import ask_llm, distill_dom
from src.logger import stream_log

import os

async def execute_plan(page: Page, plan: TestPlan, live_reporter=None) -> list:
    """
    Iterates through the test plan.
    Uses LLM to identify selectors, then Playwright to execute actions.
    If live_reporter is provided, records each result immediately to disk.
    """
    results = []
    
    # Load payloads for deep scanning
    payloads_data = {}
    payloads_path = os.path.join(os.path.dirname(__file__), "data", "payloads.json")
    if os.path.exists(payloads_path):
        with open(payloads_path, "r", encoding="utf-8") as f:
            payloads_data = json.load(f)

    for idx, intent in enumerate(plan.intents):
        await stream_log(f"\n--- Step {idx + 1}/{len(plan.intents)}: {intent.description} ---")

        # Ask LLM to identify the CSS selector for the intended action
        dom_snapshot = await distill_dom(page)
        selector_prompt = f"""
Given this distilled HTML snippet:
{dom_snapshot}

For the following action: "{intent.description}"

Respond ONLY with a valid JSON object in this exact format (no explanation):
{{"selector": "<css-selector>", "action": "click|fill|press"}}

If you cannot identify the element, respond with: {{"selector": null, "action": null}}
"""
        from src.browser_manager import ask_llm_json_with_healing
        try:
            action_data = await ask_llm_json_with_healing(
                selector_prompt,
                system="You are a Playwright automation expert. Respond only with JSON.",
                max_retries=2
            )
        except Exception:
            action_data = {}

        if not action_data.get("selector"):
            step_result = {
                "intent": intent.model_dump(),
                "action_success": False,
                "verification_success": False,
                "error": "LLM could not identify element selector.",
                "details": "No matching selector found."
            }
            results.append(step_result)
            if live_reporter:
                await live_reporter.record(intent, step_result)
            continue

        selector = action_data["selector"]
        action = action_data.get("action", "click")

        # Determine payloads to test
        test_payloads = [None] # Default to None (no payload or value specified)
        if intent.is_security_probe and getattr(intent, "attack_type", None):
            test_payloads = payloads_data.get(intent.attack_type, ["<script>alert(1)</script>"])
            await stream_log(f"[Deep Scan] Testing {len(test_payloads)} payloads for {intent.attack_type} on '{selector}'")
        elif action in ("fill", "press"):
            # For functional tests, use a default value if one isn't explicitly extracted
            test_payloads = ["test_value"]

        # Fuzz the field
        for p_idx, payload in enumerate(test_payloads):
            step_result = {
                "intent": intent.model_dump(),
                "action_success": False,
                "verification_success": False,
                "error": None,
                "details": ""
            }
            
            if len(test_payloads) > 1:
                await stream_log(f"  → Fuzzing Payload {p_idx + 1}/{len(test_payloads)}: {str(payload)[:30]}...")

            # Execute the action with Playwright
            element = await page.query_selector(selector)
            if not element:
                step_result["error"] = f"Selector '{selector}' not found on page."
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                break # Stop testing payloads if selector doesn't exist

            # Scroll element into view before interacting
            try:
                await element.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass  # Not critical — proceed anyway

            try:
                if action == "fill" and payload:
                    await element.fill(str(payload), timeout=5000)
                    await element.press("Enter", timeout=2000) # Auto-submit for fuzzing
                elif action == "press":
                    await element.press(str(payload) if payload else "Enter", timeout=5000)
                else:
                    # Try normal click first (respects visibility), fall back to force click
                    try:
                        await element.click(timeout=5000)
                    except Exception:
                        await element.click(force=True, timeout=3000)

                step_result["action_success"] = True

            except Exception as action_err:
                step_result["error"] = f"Action failed on '{selector}': {str(action_err)[:120]}"
                step_result["details"] = "Element found but could not be interacted with."
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            # Wait for navigation / DOM to settle after the action
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                except Exception:
                    pass

            # Capture post-action state: URL + distilled DOM
            current_url = page.url
            page_text = await distill_dom(page)

            # Smart URL-based shortcut for navigation tests
            url_keywords = [w.lower() for w in intent.expected_outcome.split() if len(w) > 3 and w.isalpha()]
            url_match = any(kw in current_url.lower() for kw in url_keywords)
            
            fast_fail_security = False
            if intent.is_security_probe and getattr(intent, "attack_type", None) and payload:
                fast_fail_security = True
                if intent.attack_type == "XSS":
                    if payload in page_text:
                        step_result["verification_success"] = False
                        step_result["details"] = f"VULNERABLE: XSS payload reflected un-encoded."
                    else:
                        step_result["verification_success"] = True
                        step_result["details"] = "SECURE: XSS payload blocked or sanitized."
                elif intent.attack_type in ("SQLi", "CommandInjection", "SSTI", "LFI", "SSRF"):
                    error_signatures = ["sql syntax", "mysql_fetch", "syntax error", "traceback", "internal server error", "root:x:0:0"]
                    if any(sig in page_text.lower() for sig in error_signatures):
                        step_result["verification_success"] = False
                        step_result["details"] = f"VULNERABLE: Found error signature indicating {intent.attack_type}."
                    else:
                        step_result["verification_success"] = True
                        step_result["details"] = f"SECURE: No {intent.attack_type} indicators detected."
                else:
                    fast_fail_security = False # Fallback to LLM for unknown

            if fast_fail_security:
                pass # Already handled deterministically
            elif url_match and action in ("click", None):
                step_result["verification_success"] = True
                step_result["details"] = f"URL changed to '{current_url}' which matches expected outcome."
            else:
                # Full LLM verification (with URL context)
                verify_prompt = f"""
You are a QA verification expert. A browser automation just performed an action and you must decide if the expected outcome occurred.

Action performed : "{intent.description}"
Current page URL : {current_url}
Expected outcome : "{intent.expected_outcome}"

Distilled page HTML:
{page_text}

IMPORTANT RULES:
1. If the Current page URL clearly matches the expected outcome, mark it as SUCCESS.
2. For security probes (like XSS/SQLi), if the payload is reflected unsanitized or causes an error trace, mark it as FAILED (vulnerable). If it is blocked or sanitized, mark it as SUCCESS (secure).
3. Respond ONLY with JSON: {{"success": true/false, "details": "One sentence explaining verdict"}}
"""
                from src.browser_manager import ask_llm_json_with_healing
                try:
                    verify_data = await ask_llm_json_with_healing(
                        verify_prompt,
                        system="You are a QA verification expert. Respond only with JSON.",
                        temperature=0.0,
                        max_retries=2
                    )
                    step_result["verification_success"] = verify_data.get("success", False)
                    step_result["details"] = verify_data.get("details", "")
                except Exception:
                    step_result["verification_success"] = False
                    step_result["details"] = "Verification parse error."

            status = "✅ Passed" if step_result["verification_success"] else "❌ Failed"
            if len(test_payloads) == 1:
                await stream_log(f"{status}: {step_result['details']}")
            else:
                await stream_log(f"    {status}: {step_result['details']}")

            results.append(step_result)

            if live_reporter:
                await live_reporter.record(intent, step_result)

    return results
