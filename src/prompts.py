AUDITOR_PROMPT = """You are a senior penetration tester and OWASP-certified web application security expert.
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
- evidence: A single string containing ALL exact HTML snippets, URLs, or attributes that prove the finding (separate multiple instances with a newline or comma). YOU MUST EXTRACT AND INCLUDE THE EXACT URL, LINK, OR CODE SNIPPET. Do not just describe it (e.g., do not say "There is an HTTP link", you must provide the actual HTML `<a href="http://example.com">` or the exact URL `http://example.com`).
- remediation: One concrete sentence describing the fix

IMPORTANT ENFORCEMENT RULES:
1. Think like an attacker. Do not skip anything. Be thorough and specific.
2. If multiple elements share the SAME vulnerability (e.g., 3 identical missing CSRF tokens), DO NOT create separate vulnerabilities. Group them into a SINGLE vulnerability object. You MUST list EVERY SINGLE INSTANCE in the `evidence` field. Do not just list the first one and discard the rest.
3. Focus ONLY on the categories provided for this pass.
4. ABSOLUTELY NO THEORETICAL RISKS: You MUST have concrete, visible evidence in the HTML snippet to report a vulnerability. If you cannot point to an exact line or attribute in the HTML, DO NOT report it. Do not say "potential for X exists" or "no explicit evidence found".
5. ANTI-HALLUCINATION CHECK: Before reporting an issue (like insecure HTTP links), explicitly double-check that the evidence actually violates the rule. Do not report secure links (https://) as insecure.
6. DOMAIN SCOPE CHECK: Do not report vulnerabilities on third-party links or domains (e.g., external URLs). Only report vulnerabilities relevant to the target application's code.
7. CSRF CHECK: Do not assume a missing CSRF token is a vulnerability unless there is a <form> performing a sensitive state-changing action (like POST/PUT).
8. STRICTNESS: Be extremely strict. If you are not 100% sure it's a vulnerability, DO NOT report it.

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

PLANNER_CONTEXT_PROMPT = """
You are an AI analyzing a webpage to determine if specific user input is necessary before automated testing begins.

Page Elements:
{dom_summary_json}

Does this page require specific user input (like login credentials, payment info, OTP, address, etc.) to be properly tested?

If NO user input is needed, return:
{{"needs_input": false, "prompt_message": null, "field_label": null, "placeholder": null}}

If YES, return:
{{
  "needs_input": true,
  "prompt_message": "<A clear sentence explaining what was detected and why input is needed>",
  "field_label": "<Short label describing exactly what is needed, e.g. 'Login Credentials', 'Credit Card Details', 'Shipping Address', 'OTP / Verification Code'>",
  "placeholder": "<Example of what to type, e.g. 'username: admin, password: secret' or 'Card: 4111111111111111, CVV: 123, Exp: 12/26'>"
}}

Respond ONLY with a valid JSON object.
"""

PLANNER_PLAN_PROMPT_BASE = """
You are a QA and Security testing expert. Based on the following page elements extracted from a website, generate a comprehensive test plan.

Page Elements:
{dom_summary_json}

Generate a JSON test plan with a list of test intents. Each intent must have:
- description: A specific natural language instruction of what to do (e.g. "Type 'admin' into the username field")
- expected_outcome: A robust logical assertion of what should happen after the action, accounting for valid alternative states (e.g., "Search results are displayed OR a valid 'no results' message is shown").
- is_security_probe: true if this is a security injection test, false otherwise
- attack_type: If it IS a security probe, specify the attack class (e.g., "XSS", "SQLi", "SSRF", "SSTI", "LFI", "CommandInjection"). Otherwise, leave null.
- press_enter_after_fill: By default, you MUST set this to false. You are ONLY allowed to set it to true if you are 100% certain that the field is a standalone text input (like a search bar) AND there are absolutely zero actionable buttons (Save, Submit, Search, Go) available on the form. If you are unsure, set it to false.

CRITICAL: DO NOT generate passive security/audit test cases that do not target specific, interactive page elements (such as testing for missing HTTP security headers, clickjacking/X-Frame-Options, SSL certificates, cookies, or port scanning). These passive checks are already handled in a separate static audit phase. Every test intent you generate MUST interact with one of the extracted page elements (e.g., input fields, links, buttons) via click or fill.

Include the following types of tests based on the user's request:
{tests_to_run}

{extra_context_instruction}

Respond ONLY with a JSON object in this exact format:
{{
  "intents": [
    {{"description": "...", "expected_outcome": "...", "is_security_probe": false, "attack_type": null, "press_enter_after_fill": false}},
    {{"description": "Inject XSS into search field", "expected_outcome": "Application blocks or sanitizes the payload", "is_security_probe": true, "attack_type": "XSS", "press_enter_after_fill": true}}
  ]
}}
"""

EXECUTOR_SELECTOR_PROMPT = """Given this distilled HTML snippet:
{dom_snapshot}

For the following action: "{intent_description}"

{previous_error_context}

Respond ONLY with a valid JSON object in this exact format (no explanation):
{{"selector": "<css-selector>", "action": "click|fill"}}

- Use "fill" for text inputs, search boxes, textareas.
- Use "click" for buttons, links, checkboxes, dropdowns, and everything else.
- If you cannot identify the element, respond with: {{"selector": null, "action": null}}
"""

EXECUTOR_VERIFY_PROMPT = """You are a fast QA verification classifier. A browser automation just performed an action. Your job is to classify the post-action state.

Action performed : "{intent_description}"
URL BEFORE action: {previous_url}
URL AFTER action : {current_url}
Expected outcome : "{expected_outcome}"

Distilled page HTML:
{page_text}

Classify the result into EXACTLY ONE of these categories:
- SUCCESS_MATCH: The expected outcome happened, or a logical redirect/conceptually equivalent page occurred.
- EMPTY_STATE: A valid empty state (e.g., '0 results found') after a search or filter action.
- AUTH_WALL: The user was redirected to a valid login/signup barrier (e.g., social media login prompt).
- APP_ERROR: The application genuinely crashed (e.g., 500 error, stack trace).
- UNEXPECTED_FAILURE: The action did not yield the expected result and is not an error or auth wall.
- SECURITY_VULNERABILITY: For security probes, if the payload is reflected unsanitized or causes an error trace.
- SECURITY_BLOCKED: For security probes, if the payload is blocked or sanitized.

Respond ONLY with JSON: {{"classification": "CATEGORY_NAME", "details": "One sentence explaining verdict"}}
"""

REFLECTOR_VERIFY_PROMPT = """You are a Senior QA Architect. An automated test was flagged as a failure by the fast-check system, but you need to double-check if it's a GENUINE application defect or just EXPECTED BEHAVIOR (e.g., standard login redirection, rate limiting, validation error, or a search empty state).

Action performed : "{intent_description}"
URL BEFORE action: {previous_url}
URL AFTER action : {current_url}
Expected outcome : "{expected_outcome}"
Fast-Check Result: {fast_classification} ({fast_details})

Distilled page HTML:
{page_text}

Think step-by-step. Consider:
1. Did the application crash or misbehave?
2. Is the "failure" actually just the application enforcing a standard business rule (like requiring auth, showing a validation error for bad input, or displaying a 0-results state)?
3. If it is standard business behavior, it should be marked as SUCCESS.

Respond ONLY with JSON: {{"success": true/false, "details": "One sentence explaining your final verdict"}}
"""
