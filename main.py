import asyncio
import sys
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
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
                          run_dir: str) -> dict:
    async with semaphore:
        await stream_log(f"\n[{url}] Starting automation")

        try:
            page = await init_browser(url, is_html)
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
            test_plan = await generate_test_plan(page, extra_context, custom_tests_raw, run_functional, run_probes)

            # ── 4. Initialize Live Reporter ───────────────────────────────────────
            live_reporter = LiveReporter(url, output_dir=run_dir)
            await live_reporter.initialize(audit_result, test_plan)

            # ── 5. Execute ────────────────────────────────────────────────────────
            await execute_plan(page, test_plan, live_reporter=live_reporter)

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
    if not extra_context:
        context_info = await analyze_for_required_context(discovery_page)
        if context_info and context_queue is not None:
            signal = f"{NEEDS_INPUT_PREFIX}{context_info['prompt_message']}|{context_info['field_label']}|{context_info['placeholder']}"
            await stream_log(signal)
            await stream_log(f"[Base] Waiting for user input for context...")
            try:
                extra_context = await asyncio.wait_for(context_queue.get(), timeout=120)
            except asyncio.TimeoutError:
                await stream_log(f"[Base] No context received. Using defaults.")
                extra_context = ""
        elif context_info:
            await stream_log(f"[Base] Context detected: {context_info['field_label']} — using defaults.")

    urls_to_test = await crawl_internal_links(target, discovery_page, max_pages)
    await discovery_page.context.close()

    # Parallel Execution Phase
    # Free-tier OpenRouter models have low rate limits (requests/minute).
    # We use a model pool rotation in browser_manager to automatically failover on 429s.
    # This allows us to run multiple pages in parallel without 429 blocking.
    max_concurrency = 3
    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = []
    
    for url in urls_to_test:
        tasks.append(run_single_page(
            url, is_html, extra_context, custom_tests_raw, 
            run_audit, run_functional, run_probes, 
            context_queue, semaphore, run_dir
        ))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # ── Aggregate All Sub-Reports into a Single Master Report ────────────────
    await stream_log(f"\n[Aggregator] Compiling master report from {len(results)} page(s)...")
    
    master_report = {
        "target_url": target,
        "generated_at": datetime.now(nyc_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_actions": sum(r.get("summary", {}).get("total_actions", 0) for r in results if r),
            "successful_actions": sum(r.get("summary", {}).get("successful_actions", 0) for r in results if r),
            "failed_actions": sum(r.get("summary", {}).get("failed_actions", 0) for r in results if r),
            "vulnerabilities_found": sum(r.get("summary", {}).get("vulnerabilities_found", 0) for r in results if r)
        },
        "static_audit": { "vulnerabilities": [] },
        "execution_results": []
    }
    
    for r in results:
        if isinstance(r, Exception) or not r: 
            continue
        if r.get("static_audit") and r["static_audit"].get("vulnerabilities"):
            master_report["static_audit"]["vulnerabilities"].extend(r["static_audit"]["vulnerabilities"])
        if r.get("execution_results"):
            master_report["execution_results"].extend(r["execution_results"])
            
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
