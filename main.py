import asyncio
import os
import sys
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
from src.auditor import perform_static_audit
from src.reporter import generate_report
from src.planner import generate_test_plan, analyze_for_required_context
from src.executor import execute_plan
from src.logger import stream_log

# Load environment variables (e.g., NVIDIA_API_KEY)
load_dotenv()

async def run_automation(target: str, is_html: bool = False, extra_context: str = "") -> dict:
    await stream_log(f"Starting automation for: {target}")
    page = await init_browser(target, is_html)
    
    if not page:
        await stream_log("Failed to load page. Exiting.")
        return {}

    results = []

    try:
        # 1. Plan: Static Security Audit
        audit_result = await perform_static_audit(page)

        # 1.5 Context Analysis: We skip the interactive prompt in the web UI.
        # We just use the extra_context passed from the frontend form.
        if extra_context:
            await stream_log(f"[User Context Applied] {extra_context}")

        # 2. Plan: Generate intent-based test cases based on page DOM
        test_plan = await generate_test_plan(page, extra_context)
        
        # 3. Execute: Perform actions and verify
        results = await execute_plan(page, test_plan)

        # 4. Report: Save results
        report_data = await generate_report(target, results, audit_result, test_plan)
        return report_data

    except Exception as e:
        await stream_log(f"Critical error during automation: {str(e)}")
        return {}
    finally:
        await close_browser()

if __name__ == "__main__":
    print("=" * 50)
    print("  Welcome to Stagehand QA Automation")
    print("=" * 50)

    test_target = input("\nPlease enter the website URL to test (e.g., https://example.com): ").strip()
    if not test_target:
        print("No URL provided. Exiting.")
        sys.exit(1)
        
    print(f"\nTesting {test_target}...\n")
    asyncio.run(run_automation(test_target, is_html=False))
