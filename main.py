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
                          semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        await stream_log(f"\n[{url}] Starting automation")
        page = await init_browser(url, is_html)
        if not page:
            await stream_log(f"[{url}] Failed to load page.")
            return {}

        try:
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
            live_reporter = LiveReporter(url)
            await live_reporter.initialize(audit_result, test_plan)

            # ── 5. Execute ────────────────────────────────────────────────────────
            await execute_plan(page, test_plan, live_reporter=live_reporter)

            # ── 6. Finalize ───────────────────────────────────────────────────────
            return await live_reporter.finalize()

        except Exception as e:
            await stream_log(f"[{url}] Critical error: {str(e)}")
            return {}
        finally:
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
    semaphore = asyncio.Semaphore(2) # Throttle LLM and browser contexts
    tasks = []
    
    for url in urls_to_test:
        tasks.append(run_single_page(
            url, is_html, extra_context, custom_tests_raw, 
            run_audit, run_functional, run_probes, 
            context_queue, semaphore
        ))
        
    results = await asyncio.gather(*tasks)
    
    await close_browser()
    return {"pages_tested": len(results), "reports": results}

if __name__ == "__main__":
    print("=" * 50)
    print("  AI QA & Security Automation Agent")
    print("=" * 50)
    test_target = input("\nEnter the website URL to test: ").strip()
    if not test_target:
        sys.exit(1)
    asyncio.run(run_automation(test_target, is_html=False))
