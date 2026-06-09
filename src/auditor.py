from playwright.async_api import Page
from pydantic import BaseModel
from typing import List
from src.browser_manager import ask_llm, distill_dom
from src.logger import stream_log

class Vulnerability(BaseModel):
    title: str
    description: str
    severity: str  # "low", "medium", "high", "critical"

class StaticAuditResult(BaseModel):
    vulnerabilities: List[Vulnerability]

async def perform_static_audit(page: Page) -> StaticAuditResult:
    """
    Sends the distilled HTML to the LLM and asks it to identify security vulnerabilities.
    """
    await stream_log("\n--- Performing Static HTML Security Audit ---")
    
    clean_html = await distill_dom(page)

    prompt = f"""
You are a web application security expert. Analyze the following distilled HTML for security vulnerabilities.

Look for:
1. Exposed API keys, tokens, or secrets in the source
2. Forms missing CSRF tokens
3. Insecure endpoints (http:// instead of https://)
4. Missing Content Security Policy hints in meta tags
5. Password fields that are not of type="password"

HTML to analyze:
{clean_html}

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
        await stream_log(f"Warning: Could not parse audit result. Error: {e}")
        result = StaticAuditResult(vulnerabilities=[])

    await stream_log(f"Audit complete. Found {len(result.vulnerabilities)} vulnerability(ies).")
    for v in result.vulnerabilities:
        await stream_log(f"  [{v.severity.upper()}] {v.title}: {v.description}")

    return result
