import asyncio
import sys
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
from src.settings_loader import get_concurrency_settings, get_timeout_settings
from src.auditor import perform_static_audit
from src.reporter import LiveReporter
from src.planner import generate_test_plan, analyze_for_required_context
from src.executor import execute_plan
from src.logger import stream_log

load_dotenv()

# Special SSE signal prefix — frontend watches for this
NEEDS_INPUT_PREFIX = "NEEDS_INPUT:"


async def crawl_internal_links(base_url: str, page, max_pages: int) -> list:
    if max_pages == 1:
        return [base_url]
    await stream_log(f"[Crawler] Spidering {base_url} for internal links...")
    links = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a'))
            .map(a => a.href)
            .filter(href => href && href.startsWith('http'));
    }""")
    from urllib.parse import urlparse
    base_domain = urlparse(base_url).netloc
    unique_links = {base_url}
    for link in links:
        if max_pages > 0 and len(unique_links) >= max_pages: break
        if urlparse(link).netloc == base_domain:
            unique_links.add(link)
    urls_to_test = list(unique_links)
    await stream_log(f"[Crawler] Found {len(urls_to_test)} pages: {', '.join(urls_to_test)}")
    return urls_to_test


async def run_single_page(url: str,
                          is_html: bool,
                          extra_context: str,
                          custom_tests_raw: str,
                          run_audit: bool,
                          run_functional: bool,
                          run_probes: bool,
                          context_queue: asyncio.Queue,
                          semaphore: asyncio.Semaphore,
                          run_dir: str,
                          global_tested_elements: set,
                          global_fuzzed_targets: set,
                          page_index: int = 1,
                          total_pages: int = 1,
                          auth_state: dict = None) -> dict:
    async with semaphore:
        await stream_log(f"\n[{url}] Starting automation (Page {page_index}/{total_pages})")

        try:
            page = await init_browser(url, is_html, storage_state=auth_state)
            if not page:
                await stream_log(f"[{url}] Failed to load page.")
                return {}
            # ── 2. Static Audit ────────────────────────────────────────────────────
            if run_audit:
                audit_result = await perform_static_audit(page)
            else:
                await stream_log(f"[{url}] Skipping Static Security Audit")
                audit_result = None

            # ── 2b. Context Analysis ──────────────────────────────────────────────
            # Context is resolved globally in the discovery phase.
            if extra_context:
                await stream_log(f"[{url}] User Context Applied: {extra_context}")

            # ── 3. Generate Test Plan ──────────────────────────────────────────────
            test_plan = await generate_test_plan(page, extra_context, custom_tests_raw, run_functional, run_probes, global_tested_elements)

            # ── 4. Initialize Live Reporter ───────────────────────────────────────
            live_reporter = LiveReporter(url, output_dir=run_dir)
            await live_reporter.initialize(audit_result, test_plan)

            # ── 5. Execute ────────────────────────────────────────────────────────
            await execute_plan(page, test_plan, live_reporter=live_reporter, global_fuzzed_targets=global_fuzzed_targets)

            # ── 6. Finalize ───────────────────────────────────────────────────────
            return await live_reporter.finalize()

        except Exception as e:
            await stream_log(f"[{url}] Critical error: {str(e)}")
            return {}
        finally:
            if 'page' in locals() and page:
                await page.context.close()


async def run_automation(target: str,
                         is_html: bool = False,
                         extra_context: str = "",
                         custom_tests_raw: str = "",
                         run_audit: bool = True,
                         run_functional: bool = True,
                         run_probes: bool = True,
                         max_pages: int = 1,
                         context_queue: asyncio.Queue = None) -> dict:
    # Setup Global Run Directory
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import os
    import json
    
    nyc_tz = ZoneInfo("America/New_York")
    timestamp = datetime.now(nyc_tz).strftime("%Y%m%d_%H%M%S")
    clean_domain = re.sub(r'[^a-zA-Z0-9]', '_', target.replace('https://', '').replace('http://', ''))[:30]
    run_dir = os.path.join("reports", f"{timestamp}_{clean_domain}")
    os.makedirs(run_dir, exist_ok=True)
    
    # Discovery Phase
    await stream_log(f"\n--- Initializing Discovery for {target} ---")
    discovery_page = await init_browser(target, is_html)
    if not discovery_page:
        return {}
        
    # ── Context Analysis (Global for the domain) ──────────────────────────────────
    login_was_detected    = False
    test_login_page_first = False

    if not extra_context:
        context_info = await analyze_for_required_context(discovery_page)
        if context_info and context_queue is not None:
            login_was_detected = True
            timeouts = get_timeout_settings()
            context_timeout = timeouts.get("context_input_timeout", 120.0)

            # ── Step 1: Ask for credentials ──────────────────────────────────
            signal = f"{NEEDS_INPUT_PREFIX}{context_info['prompt_message']}|{context_info['field_label']}|{context_info['placeholder']}"
            await stream_log(signal)
            await stream_log(f"[Base] Waiting for user to provide credentials...")
            try:
                extra_context = await asyncio.wait_for(context_queue.get(), timeout=context_timeout)
            except asyncio.TimeoutError:
                await stream_log(f"[Base] No credentials received within timeout. Using defaults.")
                extra_context = ""
                login_was_detected = False
            await stream_log("CONTEXT_RECEIVED")  # dismiss the credential box

            # ── Step 2: Ask whether to test the login page first ─────────────
            if extra_context and login_was_detected:
                await stream_log(
                    "NEEDS_YESNO:A login page was detected. Do you want to test it for "
                    "vulnerabilities and functional issues before logging in?"
                    "|Yes, test it|No, skip it"
                )
                try:
                    answer = await asyncio.wait_for(context_queue.get(), timeout=context_timeout)
                    answer_clean = answer.strip().lower()
                    test_login_page_first = (answer_clean == "yes")
                    await stream_log(f"[Base] Login page test decision: {'YES — will test login page first' if test_login_page_first else 'NO — skipping login page tests'}")
                except asyncio.TimeoutError:
                    test_login_page_first = False
                    await stream_log("[Base] No answer received within timeout — skipping login page test.")
                await stream_log("CONTEXT_RECEIVED")  # dismiss the yes/no box

        elif context_info:
            await stream_log(f"[Base] Context detected: {context_info['field_label']} — using defaults.")

    # ── Step 3a: Optionally test the login page ───────────────────────────────
    login_page_result = {}
    if login_was_detected and test_login_page_first:
        await stream_log("\n--- Testing Login Page (user opted in) ---")
        from src.auditor import perform_static_audit as _static_audit
        from src.reporter import LiveReporter as _LiveReporter
        # Re-navigate to the login page before testing it (in case we drifted)
        try:
            if discovery_page.url != target:
                await discovery_page.goto(target, wait_until="networkidle")
        except Exception:
            pass
        login_audit = await _static_audit(discovery_page) if run_audit else None
        login_test_plan = await generate_test_plan(
            discovery_page, "",  # no credentials — test the raw login form
            custom_tests_raw, run_functional, run_probes, set()
        )
        login_reporter = _LiveReporter(target, output_dir=run_dir)
        await login_reporter.initialize(login_audit, login_test_plan)
        await execute_plan(discovery_page, login_test_plan, live_reporter=login_reporter)
        login_page_result = await login_reporter.finalize()
        await stream_log("\n--- Login Page Test Complete — now proceeding to login ---")
    else:
        if login_was_detected:
            await stream_log("[Base] Skipping login page tests — proceeding directly to login.")

    # ── Step 3b: Perform the actual login ────────────────────────────────────
    auth_state = None
    if login_was_detected and extra_context:
        from src.planner import perform_login
        # Always re-navigate to the login page before attempting login
        # (login page tests or other drift could have changed the page)
        try:
            current_url = discovery_page.url
            if current_url != target:
                await stream_log(f"[Login] Re-navigating to login page: {target}")
                await discovery_page.goto(target, wait_until="networkidle")
        except Exception as e:
            await stream_log(f"[Login] Warning: could not re-navigate to login page: {e}")

        login_ok = await perform_login(discovery_page, extra_context)
        if login_ok:
            post_login_url = discovery_page.url
            await stream_log(f"[Login] ✅ Authenticated. Now on: {post_login_url}")
            
            # Extract session cookies and local storage to share with parallel pages
            try:
                auth_state = await discovery_page.context.storage_state()
                await stream_log(f"[Login] Successfully captured session state.")
            except Exception as e:
                await stream_log(f"[Login] Warning: could not capture session state: {e}")

            # If the login redirected somewhere other than the target, navigate to target
            try:
                if post_login_url != target:
                    await stream_log(f"[Login] Navigating to originally requested URL: {target}")
                    await discovery_page.goto(target, wait_until="networkidle")
                    await stream_log(f"[Login] Now on target page: {discovery_page.url}")
            except Exception as e:
                await stream_log(f"[Login] Warning: could not navigate to target after login: {e}")
        else:
            await stream_log("[Login] ⚠️ Login failed or could not be confirmed. Proceeding anyway.")

    urls_to_test = await crawl_internal_links(target, discovery_page, max_pages)
    await discovery_page.context.close()

    # Parallel Execution Phase
    # Free-tier OpenRouter models have low rate limits (requests/minute).
    # We use a model pool rotation in browser_manager to automatically failover on 429s.
    # This allows us to run multiple pages in parallel without 429 blocking.
    concurrency_cfg = get_concurrency_settings()
    max_concurrency = concurrency_cfg.get("max_page_concurrency", 3)
    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = []
    
    total_pages = len(urls_to_test)
    global_tested_elements = set()
    global_fuzzed_targets = set()
    
    for idx, url in enumerate(urls_to_test):
        tasks.append(run_single_page(
            url, is_html, extra_context, custom_tests_raw, 
            run_audit, run_functional, run_probes, 
            context_queue, semaphore, run_dir,
            global_tested_elements, global_fuzzed_targets,
            page_index=idx + 1, total_pages=total_pages,
            auth_state=auth_state
        ))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Prepend login-page result (if it was tested) so it's included in aggregation
    all_results = ([login_page_result] if login_page_result else []) + list(results)

    # ── Aggregate All Sub-Reports into a Single Master Report ────────────────
    await stream_log(f"\n[Aggregator] Compiling master report from {len(all_results)} page(s)...")

    
    master_report = {
        "target_url": target,
        "generated_at": datetime.now(nyc_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_pages_scanned": len(all_results),
            "total_actions": sum(r.get("summary", {}).get("total_actions", 0) for r in all_results if r and isinstance(r, dict)),
            "successful_actions": sum(r.get("summary", {}).get("successful_actions", 0) for r in all_results if r and isinstance(r, dict)),
            "failed_actions": sum(r.get("summary", {}).get("failed_actions", 0) for r in all_results if r and isinstance(r, dict)),
            "vulnerabilities_found": sum(r.get("summary", {}).get("vulnerabilities_found", 0) for r in all_results if r and isinstance(r, dict))
        },
        "static_audit": { "vulnerabilities": [] },
        "execution_results": []
    }
    
    seen_vulns = set()
    deduped_vulnerabilities = []
    
    for r in all_results:
        if isinstance(r, Exception) or not r: 
            continue
        if r.get("static_audit") and r["static_audit"].get("vulnerabilities"):
            for v in r["static_audit"]["vulnerabilities"]:
                import re
                evidence_norm = re.sub(r'\s+', ' ', str(v.get("evidence", ""))).strip().lower()
                fingerprint = (
                    str(v.get("title", "")).strip().lower(),
                    str(v.get("owasp_category", "")).strip().lower(),
                    str(v.get("cwe_id", "")).strip().lower(),
                    str(v.get("severity", "")).strip().lower(),
                    evidence_norm
                )
                if fingerprint not in seen_vulns:
                    seen_vulns.add(fingerprint)
                    v["steps_to_reproduce"] = f"1. Navigate to {target}\\n2. Open browser Developer Tools or 'View Page Source'\\n3. Locate the insecure element/evidence: {v.get('evidence', '')}"
                    deduped_vulnerabilities.append(v)
        if r.get("execution_results"):
            master_report["execution_results"].extend(r["execution_results"])
            
    master_report["static_audit"]["vulnerabilities"] = deduped_vulnerabilities
    master_report["summary"]["vulnerabilities_found"] = len(deduped_vulnerabilities)
            
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(master_report, f, indent=2)
        
    # ── Combine all test_cases_report_*.txt files into one and delete them ──
    master_txt_path = os.path.join(run_dir, "test_cases_report.txt")
    import glob
    txt_files = glob.glob(os.path.join(run_dir, "test_cases_report_*.txt"))
    with open(master_txt_path, "w", encoding="utf-8") as outfile:
        outfile.write("=" * 70 + "\n")
        outfile.write(f"  MASTER QA & SECURITY TEST CASES REPORT\n")
        outfile.write(f"  Target: {target}\n")
        outfile.write("=" * 70 + "\n\n")
        for fpath in txt_files:
            try:
                with open(fpath, "r", encoding="utf-8") as infile:
                    outfile.write(infile.read() + "\n\n")
                os.remove(fpath) # Clean up individual file
            except Exception:
                pass
    
    await close_browser()
    return master_report

if __name__ == "__main__":
    print("=" * 50)
    print("  AI QA & Security Automation Agent")
    print("=" * 50)
    if len(sys.argv) > 1:
        test_target = sys.argv[1].strip()
    else:
        test_target = input("\nEnter the website URL to test: ").strip()
    if not test_target:
        sys.exit(1)
    asyncio.run(run_automation(test_target, is_html=False))
