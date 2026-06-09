import asyncio
import os
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
from src.auditor import perform_static_audit
from src.reporter import generate_report
from src.planner import generate_test_plan, analyze_for_required_context
from src.executor import execute_plan

# Load environment variables (e.g., NVIDIA_API_KEY)
load_dotenv()

async def run_automation(target: str, is_html: bool = False):
    print(f"\nStarting automation for: {'Raw HTML' if is_html else target}")
    print("Launching headless browser...")

    page = await init_browser(target, is_html)

    audit_result = None
    results = []

    try:
        # 1. Security: Static Audit
        raw_html = await page.content()
        audit_result = await perform_static_audit(page, raw_html)

        # 1.5 Context Analysis: Check if the AI needs credentials to proceed
        extra_context = ""
        prompt_msg = await analyze_for_required_context(page)
        if prompt_msg:
            # Run blocking input() in a separate thread so we don't freeze the async event loop
            extra_context = await asyncio.to_thread(input, f"\n[AI Request] {prompt_msg}\n> ")
            extra_context = extra_context.strip()

        # 2. Plan: Generate Test Plan
        plan = await generate_test_plan(page, extra_context=extra_context)

        # 3. Execute: Run dynamic tests
        results = await execute_plan(page, plan)

        # 4. Report
        generate_report(results, audit_result, plan=plan)

    finally:
        await close_browser()

if __name__ == "__main__":
    print("=" * 50)
    print("  Welcome to Stagehand QA Automation")
    print("=" * 50)

    test_target = input("\nPlease enter the website URL to test (e.g., https://example.com): ").strip()

    if not test_target:
        test_target = "https://example.com"
        print(f"No URL provided, defaulting to {test_target}")

    print(f"\nTesting {test_target}...")
    asyncio.run(run_automation(test_target, is_html=False))
