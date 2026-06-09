import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import re
from src.planner import TestIntent, TestPlan
from src.logger import stream_log


# ─────────────────────────────────────────────────────────────────────────────
# LiveReporter: Opens files immediately and streams results as they complete.
# Usage:
#   reporter = LiveReporter(target_url, audit_result)
#   await reporter.initialize()                        ← writes header + audit
#   await reporter.record(intent, result)              ← called after EACH step
#   report_data = await reporter.finalize()            ← writes summary + JSON
# ─────────────────────────────────────────────────────────────────────────────
class LiveReporter:
    def __init__(self, target_url: str, output_dir: str = None, base_output_dir: str = "reports"):
        nyc_tz = ZoneInfo("America/New_York")
        timestamp = datetime.now(nyc_tz).strftime("%Y%m%d_%H%M%S")
        
        # A unique slug for the specific page being tested
        page_slug = re.sub(r'[^a-zA-Z0-9]', '_', target_url.replace('https://', '').replace('http://', ''))[:30]
        
        if output_dir:
            self.output_dir = output_dir
        else:
            self.output_dir = os.path.join(base_output_dir, f"{timestamp}_{page_slug}")
            
        os.makedirs(self.output_dir, exist_ok=True)

        self.target_url       = target_url
        self.page_slug        = page_slug
        self.display_ts       = datetime.now(nyc_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        # Use page_slug so parallel files don't overwrite each other
        self.tc_path          = os.path.join(self.output_dir, f"test_cases_report_{page_slug}.txt")
        self.json_path        = os.path.join(self.output_dir, f"report_{page_slug}.json")

        self.audit_result     = None
        self.results: list    = []
        self.intents: list    = []
        self._func_counter    = 0
        self._sec_counter     = 0

    async def initialize(self, audit_result, plan: TestPlan):
        """Write the file header and static audit section immediately."""
        self.audit_result = audit_result
        self.intents      = plan.intents[:]

        # ── Write main combined report header (results appended live later) ──
        with open(self.tc_path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"  QA & SECURITY TEST CASES REPORT FOR: {self.target_url}\n")
            f.write(f"  Generated: {self.display_ts}\n")
            f.write("=" * 70 + "\n\n")

            # Section 1 — Static Security Audit
            f.write("SECTION 1: STATIC SECURITY AUDIT\n")
            f.write("  (Deep OWASP Top 10 HTML analysis)\n")
            f.write("-" * 70 + "\n")
            if audit_result and audit_result.vulnerabilities:
                for idx, v in enumerate(audit_result.vulnerabilities, 1):
                    f.write(f"  SA-{idx:03d} [{v.severity.upper()}] {v.title}\n")
                    f.write(f"         OWASP     : {v.owasp_category or 'N/A'}\n")
                    f.write(f"         CWE       : {v.cwe_id or 'N/A'}\n")
                    f.write(f"         Evidence  : {v.evidence or 'N/A'}\n")
                    f.write(f"         Fix       : {v.remediation or 'N/A'}\n\n")
            else:
                f.write("  No static vulnerabilities found.\n\n")

            # Section 2 & 3 headers — results will be appended live
            f.write("\nSECTION 2: FUNCTIONAL TEST CASES\n")
            f.write("  (Happy path + negative/boundary tests — results written live)\n")
            f.write("-" * 70 + "\n")

        # ── Write standalone planned test cases RIGHT NOW (before execution) ──
        planned_path = os.path.join(self.output_dir, f"test_cases_planned_{self.page_slug}.txt")
        func   = [i for i in self.intents if not i.is_security_probe]
        probes = [i for i in self.intents if i.is_security_probe]

        with open(planned_path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"  PLANNED TEST CASES FOR: {self.target_url}\n")
            f.write(f"  Generated: {self.display_ts}\n")
            f.write(f"  Total: {len(self.intents)} cases "
                    f"({len(func)} functional, {len(probes)} security probes)\n")
            f.write("=" * 70 + "\n\n")

            f.write("FUNCTIONAL TEST CASES\n")
            f.write("  (Happy path + negative/boundary tests)\n")
            f.write("-" * 70 + "\n")
            for idx, intent in enumerate(func, 1):
                f.write(f"\n  TC-{idx:03d}\n")
                f.write(f"    Test     : {intent.description}\n")
                f.write(f"    Expected : {intent.expected_outcome}\n")

            f.write("\n\nSECURITY PROBE TEST CASES\n")
            f.write("  (XSS, SQLi, OWASP injection payloads)\n")
            f.write("-" * 70 + "\n")
            for idx, intent in enumerate(probes, 1):
                f.write(f"\n  SEC-{idx:03d}\n")
                f.write(f"    Probe    : {intent.description}\n")
                f.write(f"    Expected : {intent.expected_outcome}\n")

            f.write("\n\n" + "=" * 70 + "\n")
            f.write("  NOTE: This file contains planned test cases only.\n")
            f.write("  Results are written to test_cases_report.txt during execution.\n")
            f.write("=" * 70 + "\n")

        self.planned_path = planned_path
        await stream_log(f"[LiveReporter] Test plan saved: {planned_path}")
        await stream_log(f"[LiveReporter] Report file opened: {self.tc_path}")

    async def record(self, intent: TestIntent, result: dict):
        """
        Called immediately after each test step completes.
        Appends the result to the TXT file in real-time.
        """
        self.results.append(result)

        with open(self.tc_path, "a", encoding="utf-8") as f:
            if intent.is_security_probe:
                self._sec_counter += 1
                # Defer security probes to section 3 — write a placeholder marker
                # (We'll fill in section 3 at finalize, but log it immediately)
                pass
            else:
                self._func_counter += 1
                status = (
                    "PASS"  if result.get("verification_success") else
                    "FAIL"  if result.get("action_success")       else
                    "ERROR"
                )
                f.write(f"\n  TC-{self._func_counter:03d} [{status}]\n")
                f.write(f"         Test     : {intent.description}\n")
                f.write(f"         Expected : {intent.expected_outcome}\n")
                if result.get("details"):
                    f.write(f"         Actual   : {result['details']}\n")
                if result.get("error"):
                    f.write(f"         Error    : {result['error']}\n")

    async def finalize(self) -> dict:
        """
        Appends security probe section + summary to TXT, then writes JSON.
        Returns the report_data dict for the UI.
        """
        # Collect security probe results
        security_pairs = [
            (self.intents[i], self.results[i])
            for i in range(min(len(self.intents), len(self.results)))
            if self.intents[i].is_security_probe
        ]

        total      = len(self.results)
        passed     = sum(1 for r in self.results if r.get("verification_success"))
        failed     = total - passed
        vuln_count = len(self.audit_result.vulnerabilities) if self.audit_result else 0

        with open(self.tc_path, "a", encoding="utf-8") as f:
            # Section 3 — Security Probes (appended at end)
            f.write("\n\nSECTION 3: SECURITY PROBE TEST CASES\n")
            f.write("  (XSS, SQLi, and OWASP attack payload injection)\n")
            f.write("-" * 70 + "\n")
            if security_pairs:
                for idx, (intent, result) in enumerate(security_pairs, 1):
                    status = (
                        "SAFE"       if result.get("verification_success") else
                        "VULNERABLE" if result.get("action_success")       else
                        "ERROR"
                    )
                    f.write(f"\n  SEC-{idx:03d} [{status}]\n")
                    f.write(f"         Probe    : {intent.description}\n")
                    f.write(f"         Expected : {intent.expected_outcome}\n")
                    if result.get("details"):
                        f.write(f"         Actual   : {result['details']}\n")
                    if result.get("error"):
                        f.write(f"         Error    : {result['error']}\n")
            else:
                f.write("  No security probe tests were generated.\n")

            # Final Summary
            f.write("\n\n" + "=" * 70 + "\n")
            f.write("  SUMMARY\n")
            f.write("=" * 70 + "\n")
            f.write(f"  Total Test Cases   : {total}\n")
            f.write(f"  Passed             : {passed}\n")
            f.write(f"  Failed / Errors    : {failed}\n")
            f.write(f"  Static Vulns Found : {vuln_count}\n")
            f.write("=" * 70 + "\n")

        await stream_log(f"\n[Report] Test Cases Report : {self.tc_path}")

        # Write JSON report
        report_data = {
            "generated_at": self.display_ts,
            "target_url":   self.target_url,
            "folder_name":  os.path.basename(self.output_dir),
            "static_audit": self.audit_result.model_dump() if self.audit_result else None,
            "execution_results": self.results,
            "summary": {
                "total_actions":       total,
                "successful_actions":  passed,
                "failed_actions":      failed,
                "vulnerabilities_found": vuln_count
            }
        }
        return report_data


# ─────────────────────────────────────────────────────────────────────────────
# Legacy one-shot function — kept for backward compatibility with main.py
# Now delegates entirely to LiveReporter.
# ─────────────────────────────────────────────────────────────────────────────
async def generate_report(target_url: str, results: list, audit_result,
                          plan: TestPlan = None, base_output_dir: str = "reports") -> dict:
    reporter = LiveReporter(target_url, base_output_dir)
    await reporter.initialize(audit_result, plan)
    for i, result in enumerate(results):
        if i < len(plan.intents):
            await reporter.record(plan.intents[i], result)
    return await reporter.finalize()
