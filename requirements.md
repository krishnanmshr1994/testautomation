# requirements.md: LLM-Driven QA & Security Automation

## 1. System Input & Initialization
*   **AC 1.1 (URL Handling):** Given a valid URL, the system must initialize a headless Playwright browser instance, navigate to the URL, and wait for the network to reach an idle state.
*   **AC 1.2 (Raw HTML Handling):** Given a raw HTML string, the system must initialize a Playwright browser context and use `page.setContent()` to render the DOM locally.
*   **AC 1.3 (Context Configuration):** The system must configure the OpenAI client to connect to the designated LLM API endpoints (NVIDIA API / DeepSeek v4 Pro) and establish system prompts defining the agent's role as a QA & Security Tester.

## 2. Autonomous Discovery & Test Planning
*   **AC 2.1 (Element Mapping):** When the page loads, the system must utilize Playwright DOM evaluation to extract an optimized, LLM-readable map of all interactive elements (forms, buttons, links, dropdowns).
*   **AC 2.2 (Test Plan Generation):** The system must prompt the LLM with the element map to generate a JSON-structured test plan, covering:
    *   Happy paths (valid data entry and navigation).
    *   Negative paths (invalid data, boundary testing, empty submissions).

## 3. Dynamic Execution & Functional Testing
*   **AC 3.1 (Action Execution):** The system must iterate through the test plan using the LLM to identify specific CSS selectors based on the current DOM, and then execute those actions dynamically using Playwright primitives (`click`, `fill`, `press`).
*   **AC 3.2 (State Verification):** After each action, the system must extract the updated DOM/text and verify via the LLM if the intended outcome occurred.
*   **AC 3.3 (Recovery/Error Handling):** If an action fails (e.g., element not found), the system must log a definitive failure and continue to the next test case.

## 4. Error & Anomaly Capture
*   **AC 4.1 (Network Logging):** The system must implement Playwright network listeners to intercept and log all failed HTTP requests (status codes >= 400).
*   **AC 4.2 (Console Logging):** The system must capture all browser console outputs, flagging `console.error` and `console.warn` events as potential defects.
*   **AC 4.3 (Crash Detection):** The system must detect blank screens or unexpected navigation redirects indicating a catastrophic UI failure.

## 5. Security Scanning & Active Probing
*   **AC 5.1 (Static HTML Audit):** The system must pass the initial raw HTML payload to the LLM with a strict security prompt to identify:
    *   Exposed API keys or secrets in source.
    *   Forms lacking CSRF tokens.
    *   Insecure endpoints (`http://`).
*   **AC 5.2 (Payload Injection):** The LLM test plan must include active probing steps where it injects predefined client-side payloads (e.g., standard XSS vectors, basic SQLi characters like `' OR 1=1`) into discovered text inputs.
*   **AC 5.3 (Dialog Trapping):** The system must implement Playwright's `page.on('dialog')` listener. If an injected payload triggers an unexpected JavaScript `alert()`, `confirm()`, or `prompt()`, the system must log it as a critical XSS vulnerability and auto-dismiss the dialog to prevent execution blocking.

## 6. Output & Reporting
*   **AC 6.1 (Structured Output):** Upon completion, the system must generate a structured JSON and Markdown report.
*   **AC 6.2 (Test Cases Documentation):** The system must output a dedicated human-readable text file (`test_cases_report.txt`) listing all generated functional and security test cases alongside their PASS/FAIL execution results.
*   **AC 6.3 (Report Contents):** The report must detail:
    *   Total actions executed.
    *   Functional bugs found.
    *   Static vulnerabilities detected.
    *   Active vulnerabilities triggered (e.g., XSS dialogs caught).