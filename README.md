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
[Step 2] Static Security Audit (auditor.py)
         • distill_dom() minifies HTML
         • LLM scans using full OWASP Top 10 (2021) checklist
         • Returns vulnerabilities with: title, severity, owasp_category,
           cwe_id, evidence, remediation
        │
        ▼
[Step 3] Context Analysis (planner.py)
         • LLM inspects form elements for required user input
         • If login form / payment form / OTP detected →
           emits NEEDS_INPUT: SSE signal to UI
         • UI shows dynamic prompt (label + placeholder from LLM)
         • User fills in or leaves blank → automation uses sensible defaults
         • Continues in the SAME browser session (no re-open)
        │
        ▼
[Step 4] Test Plan Generation (planner.py)
         • Parses any Custom Test Cases provided by user
         • Extracts all input/button/link/form elements from DOM
         • LLM generates TestIntent list: description + expected_outcome
         • Includes: happy path, negative, security probes (XSS, SQLi)
         • Combines Custom Tests + AI Tests
         • test_cases_planned.txt written to disk IMMEDIATELY (before execution)
        │
        ▼
[Step 5] Execution Loop (executor.py) — with live file writes
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

Set your NVIDIA API key in `.env`:
```env
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxx
```

Model in use: **`meta/llama-3.3-70b-instruct`** via NVIDIA API (OpenAI-compatible endpoint).

---

## 📊 Report Files

| File | When written | Contents |
|---|---|---|
| `test_cases_planned.txt` | After planning (before execution) | All test intents, no results |
| `test_cases_report.txt` | Live during execution | Audit + results per test |
| `report.json` | After all tests complete | Full structured JSON |

Download from the **Execution History** table on the dashboard, or view the interactive HTML report with filters.
