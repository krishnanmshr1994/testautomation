import json
import re
from playwright.async_api import Page
from pydantic import BaseModel, Field
from typing import List, Optional
from src.browser_manager import ask_llm, distill_dom
from src.logger import stream_log


import os

class Vulnerability(BaseModel):
    title: str
    description: str
    severity: str                         # "low" | "medium" | "high" | "critical"
    owasp_category: Optional[str] = None  # e.g. "A03:2021 – Injection"
    cwe_id: Optional[str] = None          # e.g. "CWE-79"
    evidence: Optional[str] = None        # Snippet from HTML that proves the finding
    remediation: Optional[str] = None     # One-line fix suggestion


class StaticAuditResult(BaseModel):
    vulnerabilities: List[Vulnerability]


async def perform_static_audit(page: Page) -> StaticAuditResult:
    """
    Sends the distilled HTML to the LLM with a comprehensive JSON-based checklist.
    Performs multi-pass analysis if rules are grouped into multiple passes.
    """
    await stream_log("\n--- Performing Deep Static HTML Security Audit ---")

    rules_path = os.path.join(os.path.dirname(__file__), "data", "audit_rules.json")
    if not os.path.exists(rules_path):
        await stream_log("[Error] audit_rules.json not found. Skipping static audit.")
        return StaticAuditResult(vulnerabilities=[])

    with open(rules_path, "r", encoding="utf-8") as f:
        rules_data = json.load(f)

    clean_html = await distill_dom(page)
    all_vulnerabilities = []

    async def run_audit_pass(pass_prompt: str, name: str) -> list:
        # Use the fast model directly for audit passes:
        #   - Avoids the 45s reasoning-model timeout → fast-model fallback chain
        #   - The fast Llama 70B model reads HTML just as well as the reasoning model
        #   - Keeps all calls going through the global lock + rate-limiter
        from src.browser_manager import ask_llm_fast_json_with_healing
        try:
            result = await ask_llm_fast_json_with_healing(
                pass_prompt,
                system="You are an OWASP-certified penetration tester. Respond only with JSON.",
                temperature=0.0,
                pydantic_model=StaticAuditResult,
                max_retries=2
            )
            return result.vulnerabilities
        except Exception as e:
            await stream_log(f"[Warning] Could not parse audit result for {name} after retries. Error: {e}")
            return []

    # Run passes SEQUENTIALLY to avoid bursting concurrent API calls that trigger 429s.
    # The global LLM semaphore in browser_manager provides additional protection if
    # other coroutines (planner, executor) are running in parallel with the auditor.
    for pass_key, pass_info in rules_data.items():
        pass_name = pass_info.get("name", pass_key)
        await stream_log(f"  → Running Pass: {pass_name}")
        
        rules_list = pass_info.get("rules", [])
        rules_text = "\n".join([f"- {r['category']}: {r['description']}" for r in rules_list])

        from src.prompts import AUDITOR_PROMPT
        prompt = AUDITOR_PROMPT.format(
            rules_text=rules_text,
            clean_html=clean_html
        )
        pass_vulns = await run_audit_pass(prompt, pass_name)
        all_vulnerabilities.extend(pass_vulns)

    final_result = StaticAuditResult(vulnerabilities=all_vulnerabilities)
    await stream_log(f"Audit complete. Found {len(final_result.vulnerabilities)} vulnerability(ies).")
    
    for v in final_result.vulnerabilities:
        cwe = f" ({v.cwe_id})" if v.cwe_id else ""
        owasp = f" [{v.owasp_category}]" if v.owasp_category else ""
        await stream_log(f"  [{v.severity.upper()}]{owasp}{cwe} {v.title}")
        if v.evidence:
            await stream_log(f"    → Evidence: {v.evidence}")
        if v.remediation:
            await stream_log(f"    → Fix: {v.remediation}")

    return final_result
