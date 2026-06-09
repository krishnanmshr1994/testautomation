# Architecture Decisions

## 1. Environment & Containerization
**Decision:** Use GitHub Codespaces / DevContainers over Gitpod.
**Rationale:** More reliable Docker/Debian `apt` handling and native Playwright container support.

---

## 2. Language & Core Libraries
**Decision:** Python + Playwright instead of Node.js + Stagehand TS.
**Rationale:** User explicitly requested Python.

---

## 3. The Stagehand Pivot (Removing Browserbase Dependency)
**Decision:** Custom Playwright + LLM abstraction layer instead of `stagehand-py` SDK.
**Rationale:** `stagehand-py` enforces `BROWSERBASE_API_KEY` preventing local, cost-free execution. Replaced with native Playwright Async API + OpenAI client for semantic reasoning.

---

## 4. LLM Provider
**Decision:** NVIDIA API endpoint with `meta/llama-3.3-70b-instruct`.
**Rationale:** User requested avoiding direct OpenAI. Llama 3.3 70B chosen for strong JSON adherence and reasoning. DeepSeek was initially chosen but switched to Llama 3.3 for better stability.

---

## 5. Web Server: Quart (Async Flask)
**Decision:** Quart instead of Flask for the web UI.
**Rationale:** Automation is fully async (Playwright + LLM calls). Flask is synchronous and would block. Quart is a drop-in async replacement that supports Server-Sent Events (SSE) natively.

---

## 6. DOM Distillation (Minification)
**Decision:** Strip scripts, styles, SVGs, and non-semantic attributes from page HTML before sending to LLM.
**Rationale:** Raw HTML of real websites exceeds LLM context windows. Distillation reduces token usage by ~80% while preserving all element IDs, names, types, and interactive attributes needed for test planning.

---

## 7. Real-time Log Streaming via SSE
**Decision:** Server-Sent Events (SSE) for streaming terminal logs to the browser.
**Rationale:** WebSockets would require additional complexity. SSE is one-directional (server → client), lightweight, and perfectly suited for streaming automation progress. `asyncio.Queue` per listener manages fan-out to multiple browser tabs.

---

## 8. Single Browser Session Architecture
**Decision:** One browser session covers: context analysis → audit → planning → execution → reporting.
**Rationale:** Originally, `/api/analyze` opened and closed the browser, then `/api/start-test` opened it again. This caused a double browser open wasting resources. The fix uses an `asyncio.Queue` (context_queue) to pause the automation mid-run if user input is needed, then resume in the same session.
**Flow:** `NEEDS_INPUT:` SSE signal → UI shows dynamic prompt → user submits via `/api/submit-context` → queue unblocks → execution continues.

---

## 9. Live Reporting (LiveReporter)
**Decision:** `LiveReporter` class streams results to disk as each test step completes instead of batching at the end.
**Rationale:** If automation crashes mid-run, all completed results are preserved. Provides two separate output files:
- `test_cases_planned.txt` — written immediately after planning (before any execution)
- `test_cases_report.txt` — appended result-by-result during execution

---

## 10. OWASP Top 10 Audit Prompt Engineering
**Decision:** Embed the full OWASP Top 10 (2021) checklist as a literal string in the LLM prompt.
**Rationale:** Generic "check for security issues" prompts produce shallow results. By giving the LLM structured per-category instructions (A01–A10 + extended checks), it acts as a structured penetration tester. Each vulnerability now returns: `owasp_category`, `cwe_id`, `evidence`, `remediation`.

---

## 11. Smart Verification — URL-Keyword Matching
**Decision:** For navigation tests, verify by URL keyword match before falling back to LLM verification.
**Rationale:** LLM-only verification caused false negatives — e.g., after clicking "Healthcare" nav link, LLM saw a page with healthcare sections but called it the "homepage with healthcare mentions." URL-based verification is deterministic, faster, and more accurate for navigation. LLM is only called for non-navigation tests where URL alone is insufficient.

---

## 12. Resilient Element Interaction (3-tier click strategy)
**Decision:** scroll_into_view → click(5s timeout) → force click fallback.
**Rationale:** Default Playwright 30s timeout caused 30-second hangs on hidden elements (e.g., off-screen nav items, collapsed accordions). The 3-tier strategy handles 95% of visibility issues without hanging.

---

## 13. Dynamic Context Prompting
**Decision:** Context/credentials prompt shown in UI only after AI detects it's needed, not by default.
**Rationale:** Showing a credentials field by default confused users on non-login sites. The LLM now returns `field_label` and `placeholder` matching the detected form type (login, credit card, OTP, address). If left blank, automation uses sensible typed defaults.

---

## 14. Timezone: America/New_York
**Decision:** All timestamps use `zoneinfo.ZoneInfo("America/New_York")`.
**Rationale:** User is based in NYC. UTC timestamps in report filenames and log files were confusing.

---

## 15. Reporting Strategy
**Decision:** Three output files per run in a timestamped folder.

| File | When | Contents |
|---|---|---|
| `test_cases_planned.txt` | After planning | Test intents only, no results |
| `test_cases_report.txt` | Live during execution | Audit findings + live results |
| `report.json` | After completion | Full structured JSON |

Interactive HTML report viewer in the dashboard supports filter pills (All/Pass/Fail/Error/severity) and expandable accordion cards per test case.
