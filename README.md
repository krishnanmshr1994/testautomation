# AI QA & Security Automation Agent

An autonomous AI-powered web testing agent that navigates websites, performs OWASP Top 10 security audits, generates and executes functional test cases, and streams real-time results via a premium web UI.

---

## 🚀 Quick Start

### Prerequisites (GitHub Codespaces / Linux)
```bash
pip install -r requirements.txt
playwright install chromium
```

### Run the Web UI
```bash
python app.py
```
Open Port `5000` in the Ports tab → visit the dashboard.

### CLI Mode (no UI)
```bash
python main.py
```

---

## 🏗️ Architecture & Execution Flow

```
User enters URL → clicks "Commence Automation"
        │
        ▼
[Step 1] Browser opens (single session — no double open)
        │
        ▼
[Step 2] Multi-Pass Security Audit (auditor.py)
         • Reads `src/data/audit_rules.json` to perform thorough OWASP checks
         • Splits audit into multiple passes (e.g. Injection vs. Misconfigurations)
         • Can be skipped via UI toggle
        │
        ▼
[Step 3] Context Analysis (planner.py)
         • LLM inspects form elements for required user input
         • UI asks user for missing context (OTP, complex login credentials)
        │
        ▼
[Step 4] Test Plan Generation (planner.py)
         • Extracts DOM elements and combines with User Custom Tests
         • Generates Happy Path / Negative Tests (if enabled in UI)
         • Generates Security Probes by assigning `attack_type` (e.g. "XSS") (if enabled in UI)
         • test_cases_planned.txt written to disk IMMEDIATELY
        │
        ▼
[Step 5] Execution Loop & Fuzzing (executor.py) — live file writes
         • Reads intents and interacts via Playwright
         • If `attack_type` is present, loads `src/data/payloads.json` and fuzzes the field with multiple payloads (Deep Scan)
         • Captures live success/fail verification via LLM
         • Appends result to test_cases_report.txt as they happen
         For each intent:
           a. LLM identifies CSS selector from distilled DOM
           b. scroll_into_view → click/fill/press (5s timeout)
           c. Fallback: force click if element hidden
           d. wait_for_load_state("networkidle") after navigation
           e. URL-keyword match for navigation tests (no LLM needed)
           f. Full LLM verification with URL context for non-navigation tests
           g. Result written to test_cases_report.txt IMMEDIATELY via LiveReporter
        │
        ▼
[Step 6] Report Finalization (reporter.py)
         • Security probe section + summary appended to TXT
         • report.json written with full structured data
         • Browser closes
```

---

## 📁 Project Structure

```
testautomation/
├── app.py                    # Quart web server + SSE + API routes
├── main.py                   # Orchestrator — single browser session flow
├── src/
│   ├── browser_manager.py    # Playwright init, DOM distillation, LLM calls
│   ├── auditor.py            # OWASP Top 10 static HTML security audit
│   ├── planner.py            # Context analysis + test plan generation
│   ├── executor.py           # Action execution + smart verification
│   ├── reporter.py           # LiveReporter — streams results to disk live
│   └── logger.py             # AsyncLogger + SSE broadcast to UI
├── templates/
│   ├── index.html            # Main dashboard UI
│   └── report.html           # Interactive report viewer (filters + accordion)
├── reports/                  # Auto-created; one timestamped folder per run
│   └── YYYYMMDD_HHMMSS_domain/
│       ├── test_cases_planned.txt   # Generated BEFORE execution
│       ├── test_cases_report.txt    # Results written live DURING execution
│       └── report.json              # Full structured JSON report
├── logs/
│   └── automation.log        # Persistent log file (NYC timezone)
└── docs/
    └── technicals/
        └── architecture_decisions.md
```

---

## 🔑 Key Features

### Security Auditing
- Full **OWASP Top 10 (2021)** checklist embedded in the LLM prompt
- Extended checks: SSRF, open redirects, SRI missing, DOM-based XSS, PII exposure
- Each vulnerability includes: `title`, `description`, `severity`, `owasp_category`, `cwe_id`, `evidence`, `remediation`

### Smart Context Detection
- Detects login forms, credit card fields, OTP fields, address forms
- Dynamic label + placeholder shown in UI matching detected form type
- If user leaves blank → LLM uses sensible defaults (e.g., `testuser / TestPass@123`)

### Resilient Test Execution
- 3-tier click strategy: scroll → click(5s) → force click fallback
- URL-keyword matching for navigation tests (no extra LLM call needed)
- LLM verification includes current URL + explicit leniency rules to prevent false negatives

### Live Reporting
- `test_cases_planned.txt` saved immediately after planning
- `test_cases_report.txt` written result-by-result as tests execute (not batched at end)
- Interactive HTML report viewer with filter pills (All / Pass / Fail / Error / Safe / Vulnerable / by severity)

### Real-time Web UI
- SSE-based terminal log streaming
- 5-stage animated progress bar (Analyze → Audit → Plan → Execute → Report)
- Context prompt appears dynamically only when AI detects it's needed
- Pause/resume architecture — automation waits for user input, then continues same session

---

## ⚙️ Configuration

### 1. API Keys & Models (`.env`)
Set your OpenRouter API key and desired models in `.env`:
```env
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx
MODEL_NAME=poolside/laguna-m.1:free
FAST_MODEL_NAME=meta-llama/llama-3.3-70b-instruct:free
```

### 2. Fine-grained Settings (`config/settings.json`)
You can control concurrency limits, Playwright action timeouts, and LLM rate-limiting thresholds in [config/settings.json](file:///c:/Users/krish/Documents/Test%20Automation/config/settings.json):
- **concurrency**:
  - `max_page_concurrency`: Max parallel pages to crawl/audit.
  - `max_llm_concurrency`: Maximum simultaneous LLM requests.
  - `min_llm_request_delay`: Minimum delay in seconds between consecutive requests.
  - `max_llm_requests_per_minute`: Requests per minute limit (e.g. 5 for free tiers).
  - `provider_cooldown_secs`: How long (in seconds) to put an LLM provider on cooldown after any failure/rate-limit error.
- **timeouts**:
  - Fine-grained Playwright timeouts (in milliseconds) for navigation, scrolling, clicking, and filling.
  - Network idle and DOM loaded wait timeouts.
  - Model-specific reasoning and fast LLM timeout thresholds (in seconds).

### Models in Use (Tiered Strategy)
1. **Reasoning Model (`MODEL_NAME` / default: `poolside/laguna-m.1:free`)**: Used for high-complexity tasks (static security audits, test plan generation, and context analysis) with reasoning enabled.
2. **Fast Model (`FAST_MODEL_NAME` / default: `meta-llama/llama-3.3-70b-instruct:free`)**: Used for low-complexity execution steps (CSS selector identification and action verification) to maximize speed.

### Fault Tolerance, Performance & Parallelism
- **Adaptive Provider Failover & Cooldowns**: If a provider fails or encounters a rate-limit error, it is placed on a cooldown for a duration configured by `provider_cooldown_secs` (e.g. 62.0s). The system automatically skips this provider on subsequent retries and switches to the next configured provider. If all providers are cooling down, it pauses and waits for the one with the shortest remaining cooldown.
- **Robust JSON Extraction**: Employs non-greedy stream parsing using `json.JSONDecoder().raw_decode` to reliably locate and parse the first valid JSON block. This eliminates `Extra data` errors caused by LLMs appending trailing comments or markdown outside of the JSON block.
- **Timeout Fallback**: If the reasoning model times out (hard capped at 45s), the system automatically falls back to the fast model to continue execution without blocking.
- **Rate-Limit & Server Error Resilience**: Integrates automatic exponential backoff retries (up to 5 attempts) on HTTP `429 Too Many Requests`, gateway/server errors (`502`, `503`, `504`), and inline OpenRouter provider errors.
- **Deduplicated DOM Caching**: Caches distilled DOM snapshots per URL state during execution to eliminate redundant LLM calls.
- **Concurrency Cap**: Restricts parallel page audits and execution to a maximum concurrency of 3 to prevent API rate limit exhaustion.

---

## 📊 Report Files

| File | When written | Contents |
|---|---|---|
| `test_cases_planned.txt` | After planning (before execution) | All test intents, no results |
| `test_cases_report.txt` | Live during execution | Audit + results per test |
| `report.json` | After all tests complete | Full structured JSON |

Download from the **Execution History** table on the dashboard, or view the interactive HTML report with filters.
