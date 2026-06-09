from playwright.async_api import Page
from pydantic import BaseModel
from typing import List
from src.browser_manager import ask_llm

class Vulnerability(BaseModel):
    title: str
    description: str
    severity: str  # "low", "medium", "high", "critical"

class StaticAuditResult(BaseModel):
    vulnerabilities: List[Vulnerability]

async def perform_static_audit(page: Page, raw_html: str) -> StaticAuditResult:
    """
    Sends the raw HTML to the LLM and asks it to identify security vulnerabilities.
    """
    print("\n--- Performing Static HTML Security Audit ---")

    prompt = f"""
You are a web application security expert. Analyze the following raw HTML for security vulnerabilities.

Look for:
1. Exposed API keys, tokens, or secrets in the source
2. Forms missing CSRF tokens
3. Insecure endpoints (http:// instead of https://)
4. Inline JavaScript with dangerous patterns (eval, document.write, innerHTML)
5. Missing Content Security Policy hints in meta tags
6. Password fields that are not of type="password"

HTML to analyze:
{raw_html[:5000]}

Respond ONLY with a JSON object in this exact format:
{{
  "vulnerabilities": [
    {{"title": "...", "description": "...", "severity": "low|medium|high|critical"}},
    ...
  ]
}}

If no vulnerabilities are found, return: {{"vulnerabilities": []}}
"""
    import json
    response = await ask_llm(prompt, system="You are a web security expert. Respond only with JSON.")
    try:
        cleaned = response.strip().strip("```json").strip("```").strip()
        data = json.loads(cleaned)
        result = StaticAuditResult(**data)
    except Exception as e:
        print(f"Warning: Could not parse audit result. Error: {e}")
        result = StaticAuditResult(vulnerabilities=[])

    print(f"Audit complete. Found {len(result.vulnerabilities)} vulnerability(ies).")
    for v in result.vulnerabilities:
        print(f"  [{v.severity.upper()}] {v.title}: {v.description}")

    return result
