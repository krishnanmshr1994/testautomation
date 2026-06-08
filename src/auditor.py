from stagehand import Stagehand
from pydantic import BaseModel
from typing import List

class Vulnerability(BaseModel):
    title: str
    description: str
    severity: str

class StaticAuditResult(BaseModel):
    vulnerabilities: List[Vulnerability]

async def perform_static_audit(stagehand: Stagehand, raw_html: str) -> StaticAuditResult:
    """
    Performs a static audit of the raw HTML looking for secrets and missing CSRF tokens.
    """
    print("\n--- Performing Static HTML Audit ---")
    
    # We use stagehand.page.extract on the loaded HTML content to ask the LLM to find vulnerabilities.
    audit_prompt = (
        "Analyze the following HTML content for security vulnerabilities. "
        "Look for: exposed API keys, secrets in the source code, forms lacking CSRF tokens, "
        "and insecure endpoints (http://). "
        "Return a list of identified vulnerabilities."
    )
    
    result = await stagehand.page.extract({
        "instruction": audit_prompt,
        "schema": StaticAuditResult
    })
    
    print(f"Audit Complete. Found {len(result.vulnerabilities)} vulnerabilities.")
    for vuln in result.vulnerabilities:
        print(f"[{vuln.severity.upper()}] {vuln.title}: {vuln.description}")
        
    return result
