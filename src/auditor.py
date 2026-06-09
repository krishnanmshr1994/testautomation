import json
import re
from playwright.async_api import Page
from pydantic import BaseModel, Field
from typing import List, Optional
from src.browser_manager import ask_llm, distill_dom
from src.logger import stream_log


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


# ─────────────────────────────────────────────────────────────
# OWASP Top 10 (2021) + Extended checklist embedded directly
# into the prompt so the LLM acts as a structured pentester.
# ─────────────────────────────────────────────────────────────
OWASP_CHECKLIST = """
OWASP Top 10 (2021) — check ALL of the following:

A01 – Broken Access Control
  - Admin/debug routes exposed in source (e.g. /admin, /debug, /config)
  - Hidden form fields that control access (role=admin, isAdmin=true)
  - Directory listing hints

A02 – Cryptographic Failures
  - HTTP links instead of HTTPS
  - Sensitive data in URL parameters (passwords, tokens, SSNs)
  - Autocomplete not disabled on sensitive fields (password, credit card)

A03 – Injection
  - Forms that accept raw user input without visible sanitization hints
  - Dynamic SQL hints in error messages or comments
  - Server-side template injection hints (e.g., {{7*7}} in field names)

A04 – Insecure Design
  - Password reset forms that leak username enumeration
  - "Remember me" tokens in visible HTML

A05 – Security Misconfiguration
  - Missing Content-Security-Policy meta tag
  - X-Frame-Options not set (clickjacking)
  - Verbose error messages in comments or HTML
  - Framework/version disclosure in meta generators or comments

A06 – Vulnerable and Outdated Components
  - Version numbers disclosed in script src or link href attributes
  - jQuery, Bootstrap, or other library CDN links with old version numbers

A07 – Identification and Authentication Failures
  - Login forms missing rate-limiting hints (no CAPTCHA, no lockout)
  - Password fields that are not type="password"
  - Multi-step auth flows missing second factor indicators

A08 – Software and Data Integrity Failures
  - Script/link tags without integrity (SRI) attributes
  - External scripts loaded over HTTP
  - No Subresource Integrity on CDN resources

A09 – Security Logging and Monitoring Failures
  - No visible CAPTCHA or bot-protection on login/registration
  - Contact/feedback forms with no rate limiting hints

A10 – Server-Side Request Forgery (SSRF)
  - URL input fields that accept full URLs (e.g., image URL, webhook URL)
  - Import/fetch-from-URL features

Additional checks beyond OWASP Top 10:
  - API keys, tokens, secrets hardcoded in JavaScript or meta tags
  - CSRF tokens missing from state-changing forms (POST/PUT/DELETE)
  - Dangerously permissive CORS hints (Access-Control-Allow-Origin: *)
  - iFrame embedding with no sandbox attribute
  - Open redirect parameters (?redirect=, ?next=, ?url=)
  - DOM-based XSS hints (document.write, innerHTML in inline scripts)
  - PII exposure (email addresses, phone numbers, SSNs in plain HTML)
  - Social engineering risk (fake urgency, fake security badges)
"""


async def perform_static_audit(page: Page) -> StaticAuditResult:
    """
    Sends the distilled HTML to the LLM with a full OWASP Top 10 + extended
    checklist and asks it to act as a penetration tester finding all vulnerabilities.
    """
    await stream_log("\n--- Performing Deep Static HTML Security Audit (OWASP Top 10) ---")

    clean_html = await distill_dom(page)

    prompt = f"""You are a senior penetration tester and OWASP-certified web application security expert.
Your job is to perform a thorough security audit of the HTML below.

{OWASP_CHECKLIST}

HTML to analyze:
{clean_html}

For EVERY issue you find, return a vulnerability object with:
- title: Short descriptive name
- description: What the vulnerability is and why it matters
- severity: "low" | "medium" | "high" | "critical"
- owasp_category: Most relevant OWASP Top 10 category (e.g. "A02:2021 – Cryptographic Failures")
- cwe_id: Most relevant CWE ID (e.g. "CWE-319")
- evidence: The exact HTML snippet or attribute that proves the finding (keep it short)
- remediation: One concrete sentence describing the fix

IMPORTANT ENFORCEMENT RULES:
1. Think like an attacker. Do not skip anything. Be thorough and specific.
2. If multiple elements share the SAME vulnerability (e.g., 3 different 'http://' links instead of 'https://'), DO NOT create separate vulnerabilities. Group them into a SINGLE vulnerability object and list ALL instances in the `evidence` field.

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

If truly nothing is found, return: {{"vulnerabilities": []}}
"""

    response = await ask_llm(prompt, system="You are an OWASP-certified penetration tester. Respond only with JSON.", temperature=0.0)
    try:
        match = re.search(r'\{.*\}', response, re.DOTALL)
        cleaned = match.group(0) if match else response
        data = json.loads(cleaned)
        result = StaticAuditResult(**data)
    except Exception as e:
        await stream_log(f"[Warning] Could not parse audit result. Error: {e}")
        result = StaticAuditResult(vulnerabilities=[])

    await stream_log(f"Audit complete. Found {len(result.vulnerabilities)} vulnerability(ies).")
    for v in result.vulnerabilities:
        cwe = f" ({v.cwe_id})" if v.cwe_id else ""
        owasp = f" [{v.owasp_category}]" if v.owasp_category else ""
        await stream_log(f"  [{v.severity.upper()}]{owasp}{cwe} {v.title}")
        if v.remediation:
            await stream_log(f"    → Fix: {v.remediation}")

    return result
