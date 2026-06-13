import json
import re
import urllib.parse
import requests
from playwright.async_api import Page
from src.planner import TestPlan
from src.browser_manager import ask_llm, distill_dom, ask_llm_fast_json_with_healing
from src.logger import stream_log

import os

async def execute_plan(page: Page, plan: TestPlan, live_reporter=None, global_fuzzed_targets: set = None) -> list:
    """
    Iterates through the test plan.
    Uses LLM to identify selectors, then Playwright to execute actions.
    If live_reporter is provided, records each result immediately to disk.
    """
    from src.settings_loader import get_timeout_settings
    timeouts = get_timeout_settings()
    results = []
    
    # Load payloads for deep scanning
    payloads_data = {}
    payloads_path = os.path.join(os.path.dirname(__file__), "data", "payloads.json")
    if os.path.exists(payloads_path):
        with open(payloads_path, "r", encoding="utf-8") as f:
            payloads_data = json.load(f)

    # Capture the starting URL to reset state between independent tests
    base_url = page.url

    # --- DOM Snapshot Cache ---
    # Distill the DOM once per page state, reuse across steps on the same URL.
    # Only re-distill after a page navigation (URL change).
    _cached_url: str = ""
    _cached_dom: str = ""

    async def get_dom_snapshot() -> str:
        nonlocal _cached_url, _cached_dom
        current_url = page.url
        if current_url != _cached_url:
            _cached_dom = await distill_dom(page)
            _cached_url = current_url
        return _cached_dom

    for idx, intent in enumerate(plan.intents):
        await stream_log(f"\n--- Step {idx + 1}/{len(plan.intents)}: {intent.description} ---")

        # Always reset to a perfectly clean DOM state between tests
        # This prevents inputs filled in Step N-1 from leaking into Step N
        if idx > 0:
            try:
                await stream_log(f"  [State Reset] Reloading clean page state...")
                await page.goto(base_url, wait_until="domcontentloaded", timeout=timeouts.get("page_navigation", 10000))
            except Exception:
                pass

        from src.prompts import EXECUTOR_SELECTOR_PROMPT, EXECUTOR_VERIFY_PROMPT
        
        max_selector_retries = 2
        element = None
        selector = None
        action = None
        action_data = {}
        previous_error_context = ""
        
        for attempt in range(max_selector_retries + 1):
            dom_snapshot = await get_dom_snapshot()
            prompt = EXECUTOR_SELECTOR_PROMPT.format(
                dom_snapshot=dom_snapshot,
                intent_description=intent.description,
                previous_error_context=previous_error_context
            )
            try:
                action_data = await ask_llm_fast_json_with_healing(
                    prompt,
                    system="You are a Playwright automation expert. Respond only with JSON.",
                    max_retries=2
                )
            except Exception as e:
                action_data = {"selector": None, "error": str(e)}

            selector = action_data.get("selector")
            action = action_data.get("action", "click")

            if not selector:
                if attempt < max_selector_retries:
                    previous_error_context = "PREVIOUS ERROR: You failed to provide a valid selector. Please try a different approach."
                    continue
                else:
                    break

            element = await page.query_selector(selector)
            if not element:
                if attempt < max_selector_retries:
                    previous_error_context = f"PREVIOUS ERROR: The selector '{selector}' was NOT found on the page. Please provide a completely different alternative selector."
                    # Invalidate dom cache just in case
                    _cached_url = ""
                    continue
                else:
                    break
            else:
                break

        if not selector:
            err_msg = action_data.get("error") or "LLM could not identify element selector."
            await stream_log(f"❌ Skipped: {err_msg}")
            step_result = {
                "intent": intent.model_dump(),
                "action_success": False,
                "verification_success": False,
                "error": err_msg,
                "details": "No matching selector found.",
                "action_details": "Failed to identify selector"
            }
            results.append(step_result)
            if live_reporter:
                await live_reporter.record(intent, step_result)
            continue

        # Skip already fuzzed targets (Leak 3 Fix)
        if intent.is_security_probe and getattr(intent, "attack_type", None):
            fuzz_key = f"{intent.attack_type}:{selector}"
            if global_fuzzed_targets is not None:
                if fuzz_key in global_fuzzed_targets:
                    await stream_log(f"  --- Skipping: Already fuzzed {fuzz_key} ---")
                    continue
                global_fuzzed_targets.add(fuzz_key)

        # Determine payloads to test
        test_payloads = [None] # Default to None (no payload or value specified)
        if intent.is_security_probe and getattr(intent, "attack_type", None):
            test_payloads = payloads_data.get(intent.attack_type, ["<script>alert(1)</script>"])
            await stream_log(f"[Deep Scan] Testing {len(test_payloads)} payloads for {intent.attack_type} on '{selector}'")
        elif action == "fill":
            # For functional tests, use a default value if one isn't explicitly extracted
            test_payloads = ["test_value"]

        # Fuzz the field
        for p_idx, payload in enumerate(test_payloads):
            repro_code = f"await page.goto('{page.url}')\\n"
            if action == "fill":
                repro_code += f"await page.fill(\\"{selector}\\", {repr(str(payload)) if payload else '\"\"'})\\n"
                if intent.is_security_probe or getattr(intent, "press_enter_after_fill", False):
                    repro_code += f"await page.press(\\"{selector}\\", \\"Enter\\")\\n"
            elif action == "press":
                repro_code += f"await page.press(\\"{selector}\\", {repr(str(payload)) if payload else '\\"Enter\\"'})\\n"
            else:
                repro_code += f"await page.click(\\"{selector}\\")\\n"

            step_result = {
                "intent": intent.model_dump(),
                "action_success": False,
                "verification_success": False,
                "error": None,
                "details": "",
                "action_details": f"Playwright Action: {action.upper()} on selector '{selector}'" + (f" with payload '{payload}'" if payload else ""),
                "playwright_repro": repro_code
            }
            
            previous_url = page.url
            
            if len(test_payloads) > 1:
                await stream_log(f"  → Fuzzing Payload {p_idx + 1}/{len(test_payloads)}: {str(payload)[:30]}...")

            # Execute the action with Playwright
            element = await page.query_selector(selector)
            if not element:
                step_result["error"] = f"Selector '{selector}' not found on page."
                await stream_log(f"❌ Failed: Selector '{selector}' not found on page.")
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                break # Stop testing payloads if selector doesn't exist

            # Scroll element into view before interacting
            try:
                await element.scroll_into_view_if_needed(timeout=timeouts.get("element_scroll", 3000))
            except Exception:
                pass  # Not critical — proceed anyway

            # Check if the element is actually fillable (input, textarea, select, or contenteditable)
            tag_name = ""
            is_contenteditable = False
            try:
                tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
                is_contenteditable = await element.evaluate("el => el.isContentEditable || el.getAttribute('contenteditable') !== null")
            except Exception:
                pass

            # Prevent links from opening in new tabs (strip target="_blank")
            try:
                await element.evaluate("el => { if(el.tagName.toLowerCase() === 'a' && el.target === '_blank') el.removeAttribute('target'); }")
            except Exception:
                pass

            is_fillable = tag_name in ("input", "textarea", "select") or is_contenteditable

            try:
                if action == "fill" and payload:
                    if not is_fillable:
                        # Fallback: if LLM asked to fill a link/button, click it instead
                        await stream_log(f"  [Warning] Attempted to fill non-fillable element <{tag_name}>. Falling back to click.")
                        try:
                            await element.click(timeout=timeouts.get("element_click", 10000))
                        except Exception:
                            try:
                                await element.click(force=True, timeout=timeouts.get("element_force_click", 5000))
                            except Exception:
                                try:
                                    await element.dispatch_event("click")
                                except Exception:
                                    await element.evaluate("el => el.click()")
                    else:
                        try:
                            await element.fill(str(payload), timeout=timeouts.get("element_fill", 10000))
                        except Exception:
                            # Fallback: direct JS evaluation
                            await element.evaluate(f"el => {{ el.value = {json.dumps(str(payload))}; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                        if intent.is_security_probe or getattr(intent, "press_enter_after_fill", False):
                            try:
                                await element.press("Enter", timeout=timeouts.get("element_press", 5000))
                            except Exception:
                                pass
                elif action == "press":
                    await element.press(str(payload) if payload else "Enter", timeout=timeouts.get("element_fill", 10000))
                else:
                    # Try normal click first (respects visibility), fall back to force click, and finally JS click
                    try:
                        await element.click(timeout=timeouts.get("element_click", 10000))
                    except Exception:
                        try:
                            await element.click(force=True, timeout=timeouts.get("element_force_click", 5000))
                        except Exception:
                            try:
                                await element.dispatch_event("click")
                            except Exception:
                                await element.evaluate("el => el.click()")

                step_result["action_success"] = True

            except Exception as action_err:
                step_result["error"] = f"Action failed on '{selector}': {str(action_err)[:120]}"
                step_result["details"] = "Element found but could not be interacted with."
                await stream_log(f"❌ Failed: Action failed on '{selector}': {str(action_err)[:120]}")
                results.append(step_result)
                if live_reporter:
                    await live_reporter.record(intent, step_result)
                continue

            # Wait for navigation / DOM to settle after the action
            try:
                await page.wait_for_load_state("networkidle", timeout=timeouts.get("page_load_state_network_idle", 10000))
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=timeouts.get("page_load_state_dom_loaded", 5000))
                except Exception:
                    pass

            # Capture post-action state: URL + distilled DOM
            current_url = page.url
            page_text = await distill_dom(page)

            # Smart URL-based shortcut for navigation tests (Leak 2 Fix + Robust Redirects)
            url_match = False
            expected_url_pattern = re.search(r'(https?://[^\s]+|ftp://[^\s]+|rsync://[^\s]+|/[a-zA-Z0-9_\-\./]+)', intent.expected_outcome)
            if expected_url_pattern:
                expected_url = expected_url_pattern.group(0).strip("'\".),")
                parsed_url = urllib.parse.urlparse(expected_url)
                
                if parsed_url.scheme in ("ftp", "rsync"):
                    url_match = True
                    step_result["details"] = f"Custom protocol '{parsed_url.scheme}' successfully invoked."
                else:
                    if expected_url in current_url and "error" not in current_url.lower():
                        url_match = True
                    elif expected_url.startswith("http"):
                        # Resolve server-side redirects
                        try:
                            res = requests.head(expected_url, allow_redirects=True, timeout=5)
                            if res.url in current_url or current_url in res.url:
                                url_match = True
                        except Exception:
                            pass
            
            if not url_match and not step_result.get("details"):
                url_keywords = [w.lower() for w in intent.expected_outcome.split() if len(w) > 3 and w.isalpha()]
                url_match = any(kw in current_url.lower() for kw in url_keywords) and len(url_keywords) > 0
            
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
                from src.prompts import EXECUTOR_VERIFY_PROMPT, REFLECTOR_VERIFY_PROMPT
                verify_prompt = EXECUTOR_VERIFY_PROMPT.format(
                    intent_description=intent.description,
                    previous_url=previous_url,
                    current_url=current_url,
                    expected_outcome=intent.expected_outcome,
                    page_text=page_text
                )
                try:
                    verify_data = await ask_llm_fast_json_with_healing(
                        verify_prompt,
                        system="You are a fast QA verification classifier. Respond ONLY with JSON.",
                        temperature=0.0,
                        max_retries=2
                    )
                    cls = verify_data.get("classification", "UNEXPECTED_FAILURE")
                    details = verify_data.get("details", "")
                    
                    if cls in ("SUCCESS_MATCH", "EMPTY_STATE", "AUTH_WALL", "LOGICAL_REDIRECT", "SECURITY_BLOCKED"):
                        step_result["verification_success"] = True
                        step_result["details"] = details
                    elif cls in ("UNEXPECTED_FAILURE", "APP_ERROR"):
                        # Reflector Escalation (Reasoning Model)
                        await stream_log(f"  [Reflector] Escalating classification '{cls}' to reasoning model...")
                        reflector_prompt = REFLECTOR_VERIFY_PROMPT.format(
                            intent_description=intent.description,
                            previous_url=previous_url,
                            current_url=current_url,
                            expected_outcome=intent.expected_outcome,
                            fast_classification=cls,
                            fast_details=details,
                            page_text=page_text
                        )
                        from src.browser_manager import ask_llm_json_with_healing
                        reflector_data = await ask_llm_json_with_healing(
                            reflector_prompt,
                            system="You are a Senior QA Architect. Respond ONLY with JSON.",
                            temperature=0.1,
                            max_retries=2
                        )
                        step_result["verification_success"] = reflector_data.get("success", False)
                        step_result["details"] = f"(Reflector) {reflector_data.get('details', details)}"
                    else:
                        step_result["verification_success"] = False
                        step_result["details"] = details

                except Exception as e:
                    step_result["verification_success"] = False
                    step_result["details"] = f"Verification parse error: {e}"

            status = "✅ Passed" if step_result["verification_success"] else "❌ Failed"
            if len(test_payloads) == 1:
                await stream_log(f"{status}: {step_result['details']}")
            else:
                await stream_log(f"    {status}: {step_result['details']}")

            results.append(step_result)

            if live_reporter:
                await live_reporter.record(intent, step_result)

    return results
