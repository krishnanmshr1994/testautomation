import asyncio
import os
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
from src.auditor import perform_static_audit
from src.planner import generate_test_plan
from src.executor import execute_plan
from src.reporter import generate_report

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

        # 2. Plan: Generate Test Plan
        plan = await generate_test_plan(page)

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
