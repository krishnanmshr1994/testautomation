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
        from src.browser_manager import ask_llm_json_with_healing
        try:
            result = await ask_llm_json_with_healing(
                pass_prompt,
                system="You are an OWASP-certified penetration tester. Respond only with JSON.",
                temperature=0.0,
                pydantic_model=StaticAuditResult
            )
            return result.vulnerabilities
        except Exception as e:
            await stream_log(f"[Warning] Could not parse audit result for {name} after retries. Error: {e}")
            return []

    tasks = []
    for pass_key, pass_info in rules_data.items():
        pass_name = pass_info.get("name", pass_key)
        await stream_log(f"  → Queueing Pass: {pass_name}")
        
        rules_list = pass_info.get("rules", [])
        rules_text = "\n".join([f"- {r['category']}: {r['description']}" for r in rules_list])

        prompt = f"""You are a senior penetration tester and OWASP-certified web application security expert.
Your job is to perform a thorough security audit of the HTML below, focusing specifically on the following categories:

{rules_text}

HTML to analyze:
{clean_html}

For EVERY issue you find, return a vulnerability object with:
- title: Short descriptive name
- description: What the vulnerability is and why it matters
- severity: "low" | "medium" | "high" | "critical"
- owasp_category: Most relevant category from the list above
- cwe_id: Most relevant CWE ID (e.g. "CWE-79")
- evidence: The exact HTML snippet or attribute that proves the finding (keep it short)
- remediation: One concrete sentence describing the fix

IMPORTANT ENFORCEMENT RULES:
1. Think like an attacker. Do not skip anything. Be thorough and specific.
2. If multiple elements share the SAME vulnerability (e.g., 3 different 'http://' links instead of 'https://'), DO NOT create separate vulnerabilities. Group them into a SINGLE vulnerability object and list ALL instances in the `evidence` field.
3. Focus ONLY on the categories provided for this pass.
4. ABSOLUTELY NO THEORETICAL RISKS: You MUST have concrete, visible evidence in the HTML snippet to report a vulnerability. If you cannot point to an exact line or attribute in the HTML, DO NOT report it. Do not say "potential for X exists" or "no explicit evidence found".

Respond ONLY with valid JSON in this exact format:
{{
  "vulnerabilities": [
    {{
      "title": "...",
      "description": "...",
      "severity": "...",
      "owasp_category": "...",
      "cwe_id": "...",
      "evidence": "...",
      "remediation": "..."
    }}
  ]
}}

If truly nothing is found for these categories, return: {{"vulnerabilities": []}}
"""
        tasks.append(run_audit_pass(prompt, pass_name))

    import asyncio
    results = await asyncio.gather(*tasks)
    for res in results:
        all_vulnerabilities.extend(res)

    final_result = StaticAuditResult(vulnerabilities=all_vulnerabilities)
    await stream_log(f"Audit complete. Found {len(final_result.vulnerabilities)} vulnerability(ies).")
    
    for v in final_result.vulnerabilities:
        cwe = f" ({v.cwe_id})" if v.cwe_id else ""
        owasp = f" [{v.owasp_category}]" if v.owasp_category else ""
        await stream_log(f"  [{v.severity.upper()}]{owasp}{cwe} {v.title}")
        if v.remediation:
            await stream_log(f"    → Fix: {v.remediation}")

    return final_result
