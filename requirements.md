# requirements.md: LLM-Driven Stagehand QA & Security Automation

## 1. System Input & Initialization
*   **AC 1.1 (URL Handling):** Given a valid URL, the system must initialize a headless Playwright browser instance, navigate to the URL, and wait for the network to reach an idle state.
*   **AC 1.2 (Raw HTML Handling):** Given a raw HTML string, the system must initialize a Playwright browser context and use `page.setContent()` to render the DOM locally.
*   **AC 1.3 (Context Configuration):** The system must configure the Stagehand context with the designated LLM API keys (e.g., Gemini/GPT) and establish system prompts defining the agent's role as a QA & Security Tester.

## 2. Autonomous Discovery & Test Planning
*   **AC 2.1 (Element Mapping):** When the page loads, the system must utilize Stagehand's `page.observe()` to extract an optimized, LLM-readable map of all interactive elements (forms, buttons, links, dropdowns).
*   **AC 2.2 (Test Plan Generation):** The system must prompt the embedded LLM with the element map to generate a JSON-structured test plan, covering:
    *   Happy paths (valid data entry and navigation).
    *   Negative paths (invalid data, boundary testing, empty submissions).

## 3. Dynamic Execution & Functional Testing
*   **AC 3.1 (Action Execution):** The system must iterate through the test plan using Stagehand's `page.act()`, dynamically translating the LLM's natural language intents into Playwright actions.
*   **AC 3.2 (State Verification):** After each action, the system must use Stagehand's `page.extract()` or `page.observe()` to verify if the intended outcome occurred (e.g., "Did the success message appear?").
*   **AC 3.3 (Recovery):** If an action fails (e.g., element not found), the system must pass the current DOM state back to the LLM to attempt a self-correction or log a definitive failure.

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
*   **AC 6.2 (Report Contents):** The report must detail:
    *   Total actions executed.
    *   Functional bugs found (with steps to reproduce).
    *   Static vulnerabilities detected.
    *   Active vulnerabilities triggered (e.g., XSS dialogs caught).
    *   Captured console and network errors.