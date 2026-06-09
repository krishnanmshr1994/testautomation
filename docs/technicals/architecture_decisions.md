# Architecture Decisions

## 1. Environment & Containerization
**Decision:** Use GitHub Codespaces / DevContainers over Gitpod.
**Rationale:** The project originally started targeting Gitpod, but shifted to GitHub Codespaces as the primary execution environment due to more reliable Docker/Debian `apt` handling and native Playwright container support.

## 2. Language & Core Libraries
**Decision:** Python + Playwright instead of Node.js + Stagehand TS.
**Rationale:** The user explicitly requested Python for this automation project. 

## 3. The Stagehand Pivot (Removing Browserbase Dependency)
**Decision:** Build a custom Playwright + LLM abstraction layer rather than relying on the `stagehand-py` SDK.
**Rationale:** 
- The official Python SDK for Stagehand (`stagehand-py`) strictly enforces the usage of a Browserbase Cloud API key (`BROWSERBASE_API_KEY`) to initialize a session, preventing a truly local, cost-free execution.
- To bypass this lock-in, the architecture was rewritten to use Playwright's native Async API to control local Chromium browsers, and the `openai` Python client to perform the exact same DOM-extraction and semantic reasoning that Stagehand uses under the hood.

## 4. LLM Provider
**Decision:** NVIDIA API Endpoint with `deepseek-ai/deepseek-v4-pro`.
**Rationale:** 
- User requested avoiding direct OpenAI and leveraging an NVIDIA-hosted endpoint.
- DeepSeek v4 Pro was chosen over Gemma 4 31B because of its vastly superior reasoning and coding capabilities, which are essential for generating accurate CSS selectors from raw HTML, strict JSON schema adherence, and creative security payloads.

## 5. Reporting Strategy
**Decision:** Generate unified plain-text Test Case results, alongside JSON and Markdown.
**Rationale:** The user wanted to clearly see the generated test cases side-by-side with their pass/fail status. The reporting engine produces `reports/test_cases_report.txt` which cleanly segregates Static Audits, Functional Tests, and Security Probes.
