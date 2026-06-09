import asyncio
import os
from dotenv import load_dotenv
from src.browser_manager import init_browser, close_browser
from src.auditor import perform_static_audit
from src.planner import generate_test_plan
from src.executor import execute_plan
from src.reporter import generate_report

# Load environment variables (e.g., OPENAI_API_KEY)
load_dotenv()

async def run_automation(target: str, is_html: bool = False):
    print(f"Starting automation for: {'Raw HTML' if is_html else target}")
    
    # 1. Initialize Stagehand & Playwright
    stagehand, page = await init_browser(target, is_html)
    
    audit_result = None
    try:
        # 2. Security: Static Audit if raw HTML
        if is_html:
            audit_result = await perform_static_audit(stagehand, target)
            
        # 3. Plan: Generate Test Plan using LLM
        plan = await generate_test_plan(stagehand)
        
        # 4. Execute: Run dynamic tests and active probes
        results = await execute_plan(stagehand, plan)
        
        # 5. Report: Aggregate and output
        generate_report(results, audit_result)
        
    finally:
        await close_browser(stagehand)

if __name__ == "__main__":
    # Prompt the user for the URL to test
    print("Welcome to Stagehand QA Automation")
    test_target = input("Please enter the website URL to test (e.g., https://example.com): ").strip()
    
    if not test_target:
        test_target = "https://example.com"
        print(f"No URL provided, defaulting to {test_target}")
        
    print(f"Testing {test_target}...")
    asyncio.run(run_automation(test_target, is_html=False))
