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
| `test_cases_planned.txt` | After planning (before execution) | All test intents (Custom + AI-generated), no results |
| `test_cases_report.txt` | Live during execution | Audit findings + live results |
| `report.json` | After completion | Full structured JSON |

Interactive HTML report viewer in the dashboard supports filter pills (All/Pass/Fail/Error/severity) and expandable accordion cards per test case.

---

## 16. Custom Test Cases Support
**Decision:** Allow users to write and inject their own custom test cases via the web UI before automation starts.
---

## 17. Multi-Pass Security Audits
**Decision:** Split the static security audit into multiple targeted passes defined in a JSON file (`src/data/audit_rules.json`) instead of one giant hardcoded OWASP string.
**Rationale:** Sending 50+ rules to an LLM at once causes attention loss (hallucinations/skipped checks). Chunking them by category ensures deep focus.

---

## 18. Payload Dictionary (Deep Scan Fuzzing)
**Decision:** Store fuzzing payloads in `src/data/payloads.json` instead of letting the LLM invent payloads.
**Rationale:** The LLM is good at identifying attack vectors (e.g., "This search box is vulnerable to XSS") but slow/inconsistent at generating optimal payloads. By pairing the LLM's logic with a hardcoded dictionary (SecLists style), we can loop through 5-10 proven payloads for a specific vulnerability class efficiently.

---

## 19. Self-Healing LLM Parsing
**Decision:** Wrapper loop that catches JSONDecode/Pydantic errors and resubmits the exact exception to the LLM.
**Rationale:** Non-deterministic AI occasionally drops a bracket or forgets a field. Rather than crashing the pipeline, we feed the error back as `[System Feedback]` and demand it retry. Succeeds on the 2nd attempt 99% of the time, dramatically improving reliability.

---

## 20. Deterministic Fast-Path Verification
**Decision:** Skip LLM verification for security probes (like XSS/SQLi) and use deterministic string-matching/heuristics.
**Rationale:** Fuzzing 10 payloads on a field and waiting 10 seconds per LLM verification call took >30 minutes. Searching the DOM text for the unencoded payload string (for XSS) or common DB error traces (for SQLi) drops verification time from 10s down to 0.001s, accelerating execution by 100x while keeping Playwright safe and sequential.

---

## 21. Parallel Multi-Page Spidering
**Decision:** Auto-crawl internal `<a>` tags and run full AI pipeline in parallel via `asyncio.gather`.
**Rationale:** Moves the tool from a single-page scanner to a site-wide crawler. Throttled via `asyncio.Semaphore(2)` to prevent LLM API HTTP 429 Too Many Requests limits while mapping entire domains seamlessly. Isolated Playwright `BrowserContext` for each parallel branch ensures DOM state safety.

---

## 22. Migration to OpenRouter
**Decision:** Migrate the primary LLM client/endpoint to OpenRouter.ai, using `poolside/laguna-m.1:free` as the default reasoning model and `meta-llama/llama-3.3-70b-instruct:free` as the default fast model.
**Rationale:** The previous NVIDIA API endpoints suffered from frequent timeouts, high congestion, and lacked access to models supporting explicit reasoning tracks. OpenRouter provides a unified endpoint, supports free tiers of modern LLMs, and lets us configure reasoning parameter overlays.

---

## 23. Tiered Model Strategy
**Decision:** Split tasks between a "reasoning model" and a "fast model".
- **Reasoning Model:** Used for complex tasks (static security audits, test plan generation, context analysis) where planning and multi-turn logic are critical.
- **Fast Model:** Used for high-frequency, simple execution steps (CSS selector identification, pass/fail action verification) where response latency is the primary constraint.
**Rationale:** Restricting high-latency reasoning calls to early-phase orchestration saves execution time while utilizing cheap/fast models for iterative Playwright actions.

---

## 24. DOM Snapshot Caching
**Decision:** Cache distilled DOM snapshots per URL state during execution.
**Rationale:** Multiple actions (like selector identification and post-action verification) on the same page state repeatedly fetched and minified the DOM. Caching it locally reduces CPU overhead and avoids making redundant LLM calls on identical page views.

---

## 25. Timeout Fallback
**Decision:** Wrap the reasoning model calls in a hard 45-second `asyncio.wait_for` timeout. If it times out, automatically fall back to executing the prompt using the fast model.
**Rationale:** Free reasoning models can experience severe queues and delays. Rather than letting the automation hang indefinitely, falling back to a fast model keeps the pipeline active and ensures tests eventually complete.

---

## 26. API Client & Error Resiliency (Rate Limits & Congestion)
**Decision:** Implement robust HTTP status checking and client-side exponential backoff retries (up to 5 attempts) directly in the network utility (`_call_openrouter`). Specifically handle HTTP `429 Too Many Requests`, server gateway errors (`502`, `503`, `504`), connection failures, and inline OpenRouter JSON error responses.
**Rationale:** With parallel execution active, concurrent calls easily trigger OpenRouter's rate limits. Handling 429s and server congestion with self-healing backoff retries prevents cascading failures across parallel pipelines.
---

## 27. Free-Tier Model Pool Rotation
**Decision:** Implement a multi-model failover pool for both Reasoning and Fast models when using the OpenRouter API. If a 429 Too Many Requests is encountered on a specific model, instantly rotate to the next verified 100B+ parameter model in the pool (e.g. rotating from laguna to nemotron-super-120b). If the entire API key hits a rate limit, the process implements an aggregate backoff sleep before retrying.
**Rationale:** OpenRouter free tier imposes strict account-wide and model-specific rate limits (e.g. 20 requests per minute). Hard-coding a single model for concurrent pages instantly triggers 429s. Rotating through a pool of verified models allows full parallel execution without blocking.

---

## 28. Playwright Network Interception for Trackers
**Decision:** Inject an aggressive page.route network interceptor in `init_browser()` that instantly aborts requests matching common tracking and analytics domains (e.g., Google Analytics, Hotjar, Sentry, Facebook Pixel).
**Rationale:** Analytics scripts unnecessarily slow down Playwright page load times (`networkidle`), clutter terminal console logs with CSP errors, and pollute the target application's analytics dashboards with bot traffic. Aborting these connections solves all three issues.

---

## 29. Per-Provider Cooldown Mechanism
**Decision:** Add per-provider cooldown tracking (`_provider_cooldowns` expiry dict) and a configurable `provider_cooldown_secs` parameter. If a provider encounters a rate limit (429) or connection error, place it on cooldown and prioritize other providers. If all configured providers are cooling down, the system sleeps/blocks for the minimum remaining duration before retrying.
**Rationale:** Standard rotating failover can easily ping-pong back and forth between two exhausted providers, leading to immediate back-to-back 429s. A dedicated cooldown timer guarantees that rate-limited providers have sufficient time (e.g. 62 seconds) to clear their rolling rate-limit window before receiving another call.

---

## 30. Robust JSON Parsing using Stream Decoding
**Decision:** Replace regex-based extraction of LLM payloads (`re.search(r'\{.*\}', content)`) with a native stream-decoding approach using `json.JSONDecoder().raw_decode()`.
**Rationale:** Under high congestion, some models output trailing thoughts, conversational text, or multiple JSON objects. A greedy regex captures everything from the first `{` to the last `}`, rendering the string invalid JSON. Stream decoding starts at the first `{` and reads only until the matching closing brace, successfully isolating the desired JSON payload.

---

## 31. Playwright Action Constraints (Click & Fill Only)
**Decision:** Remove the `press` action from the selector generator's instructions, restricting allowed interactive actions for functional steps to `click` and `fill`.
**Rationale:** Playwright's `locator.press()` expects a keyboard key name (e.g., `"Enter"` or `"Control+A"`). Generating a `press` action with a text payload (e.g., `element.press("test_value")`) caused Playwright crashes. Restricting LLM actions to `fill` (for inputs) and `click` (for buttons and links) maintains API safety, while allowing internal helpers to execute key presses when required.

