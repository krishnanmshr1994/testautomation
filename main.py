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


async def run_automation(target: str,
                         is_html: bool = False,
                         extra_context: str = "",
                         context_queue: asyncio.Queue = None) -> dict:
    """
    Single browser session:
      1. Open browser
      2. Static security audit
      3. Analyze for required context → if needed, pause and wait for user input
      4. Generate test plan
      5. Execute (writing results live)
      6. Finalize report
    """
    await stream_log(f"Starting automation for: {target}")
    page = await init_browser(target, is_html)

    if not page:
        await stream_log("Failed to load page. Exiting.")
        return {}

    try:
        # ── 1. Static Security Audit ───────────────────────────────────────────
        audit_result = await perform_static_audit(page)

        # ── 2. Context Analysis (same browser session — no double open!) ───────
        if not extra_context:
            context_info = await analyze_for_required_context(page)
            if context_info and context_queue is not None:
                # Signal the frontend to show the context prompt
                signal = (
                    f"{NEEDS_INPUT_PREFIX}"
                    f"{context_info['prompt_message']}|"
                    f"{context_info['field_label']}|"
                    f"{context_info['placeholder']}"
                )
                await stream_log(signal)

                # Pause — wait for user to submit context (or skip with blank)
                await stream_log("[Waiting for user input... submit the form or leave blank to continue with defaults]")
                try:
                    extra_context = await asyncio.wait_for(context_queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    await stream_log("[Timeout] No context received. Using defaults.")
                    extra_context = ""
            elif context_info:
                # CLI mode — skip interactive prompt, use defaults
                await stream_log(f"[Context detected] {context_info['field_label']} — using defaults.")

        if extra_context:
            await stream_log(f"[User Context Applied] {extra_context}")

        # ── 3. Generate Test Plan ──────────────────────────────────────────────
        test_plan = await generate_test_plan(page, extra_context)

        # ── 4. Initialize Live Reporter (writes header + audit to disk NOW) ────
        live_reporter = LiveReporter(target)
        await live_reporter.initialize(audit_result, test_plan)

        # ── 5. Execute (each result written to disk immediately after) ─────────
        await execute_plan(page, test_plan, live_reporter=live_reporter)

        # ── 6. Finalize (summary + JSON) ──────────────────────────────────────
        report_data = await live_reporter.finalize()
        return report_data

    except Exception as e:
        await stream_log(f"Critical error during automation: {str(e)}")
        return {}
    finally:
        await close_browser()


# ── CLI entry-point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  AI QA & Security Automation Agent")
    print("=" * 50)
    test_target = input("\nEnter the website URL to test: ").strip()
    if not test_target:
        print("No URL provided. Exiting.")
        sys.exit(1)
    print(f"\nTesting {test_target}...\n")
    asyncio.run(run_automation(test_target, is_html=False))
