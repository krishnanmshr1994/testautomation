import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import re
from src.planner import TestPlan
from src.logger import stream_log

async def generate_report(target_url: str, results: list, audit_result, plan: TestPlan = None, base_output_dir: str = "reports"):
    """
    Generates:
    1. test_cases_report.txt  - Each test case with its PASS/FAIL result
    2. report.json            - Full structured JSON output
    """
    # Create dynamic folder name: YYYYMMDD_HHMMSS_domain
    nyc_tz = ZoneInfo("America/New_York")
    timestamp = datetime.now(nyc_tz).strftime("%Y%m%d_%H%M%S")
    clean_domain = re.sub(r'[^a-zA-Z0-9]', '_', target_url.replace('https://', '').replace('http://', ''))[:30]
    output_dir = os.path.join(base_output_dir, f"{timestamp}_{clean_domain}")
    os.makedirs(output_dir, exist_ok=True)
    
    display_timestamp = datetime.now(nyc_tz).strftime("%Y-%m-%d %H:%M:%S")

    # ─────────────────────────────────────────────────────────────
    # 1. TEST CASES REPORT (main deliverable the user asked for)
    # ─────────────────────────────────────────────────────────────
    tc_path = os.path.join(output_dir, "test_cases_report.txt")
    with open(tc_path, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"  QA & SECURITY TEST CASES REPORT FOR: {target_url}\n")
        f.write(f"  Generated: {display_timestamp}\n")
        f.write("=" * 70 + "\n\n")

        # --- SECTION 1: Static Security Audit ---
        f.write("SECTION 1: STATIC SECURITY AUDIT\n")
        f.write("  (LLM reads the raw HTML for obvious security flaws)\n")
        f.write("-" * 70 + "\n")
        if audit_result and audit_result.vulnerabilities:
            for idx, v in enumerate(audit_result.vulnerabilities, 1):
                f.write(f"  SA-{idx:03d} [{v.severity.upper()}] {v.title}\n")
                f.write(f"         Description : {v.description}\n")
                f.write(f"         Result      : VULNERABILITY FOUND\n\n")
        else:
            f.write("  No static vulnerabilities found.\n\n")

        # --- SECTION 2: Functional & Security Test Cases with Results ---
        if plan and results:
            functional = [(plan.intents[i], results[i]) for i in range(min(len(plan.intents), len(results))) if not plan.intents[i].is_security_probe]
            security = [(plan.intents[i], results[i]) for i in range(min(len(plan.intents), len(results))) if plan.intents[i].is_security_probe]

            f.write("\nSECTION 2: FUNCTIONAL TEST CASES\n")
            f.write("  (Happy path + negative/boundary tests)\n")
            f.write("-" * 70 + "\n")
            if functional:
                for idx, (intent, result) in enumerate(functional, 1):
                    status = "PASS" if result.get("verification_success") else ("FAIL" if result.get("action_success") else "ERROR")
                    f.write(f"\n  TC-{idx:03d} [{status}]\n")
                    f.write(f"         Test        : {intent.description}\n")
                    f.write(f"         Expected    : {intent.expected_outcome}\n")
                    if result.get("details"):
                        f.write(f"         Actual      : {result['details']}\n")
                    if result.get("error"):
                        f.write(f"         Error       : {result['error']}\n")
            else:
                f.write("  No functional test cases were generated.\n")

            f.write("\n\nSECTION 3: SECURITY PROBE TEST CASES\n")
            f.write("  (XSS and SQLi payload injection)\n")
            f.write("-" * 70 + "\n")
            if security:
                for idx, (intent, result) in enumerate(security, 1):
                    # For security probes: PASS means the attack DID NOT succeed (no alert fired)
                    status = "SAFE" if result.get("verification_success") else ("VULNERABLE" if result.get("action_success") else "ERROR")
                    f.write(f"\n  SEC-{idx:03d} [{status}]\n")
                    f.write(f"         Probe       : {intent.description}\n")
                    f.write(f"         Expected    : {intent.expected_outcome}\n")
                    if result.get("details"):
                        f.write(f"         Actual      : {result['details']}\n")
                    if result.get("error"):
                        f.write(f"         Error       : {result['error']}\n")
            else:
                f.write("  No security probe tests were generated.\n")

        # --- Summary ---
        total = len(results)
        passed = sum(1 for r in results if r.get("verification_success"))
        failed = total - passed
        vuln_count = len(audit_result.vulnerabilities) if audit_result else 0

        f.write("\n\n" + "=" * 70 + "\n")
        f.write("  SUMMARY\n")
        f.write("=" * 70 + "\n")
        f.write(f"  Total Test Cases   : {total}\n")
        f.write(f"  Passed             : {passed}\n")
        f.write(f"  Failed / Errors    : {failed}\n")
        f.write(f"  Static Vulns Found : {vuln_count}\n")
        f.write("=" * 70 + "\n")

    await stream_log(f"\n[Report] Test Cases Report : {tc_path}")

    # ─────────────────────────────────────────────────────────────
    # 2. JSON Report (raw data for programmatic use)
    # ─────────────────────────────────────────────────────────────
    report_data = {
        "generated_at": display_timestamp,
        "target_url": target_url,
        "folder_name": os.path.basename(output_dir),
        "static_audit": audit_result.model_dump() if audit_result else None,
        "execution_results": results,
        "summary": {
            "total_actions": total,
            "successful_actions": passed,
            "failed_actions": failed,
            "vulnerabilities_found": vuln_count
        }
    }
    json_path = os.path.join(output_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4)

    await stream_log(f"[Report] Full JSON Report  : {json_path}")
    return report_data
