import asyncio
import json
import os
from quart import Quart, render_template, request, jsonify, make_response, send_file
from main import run_automation
from src.logger import stream_logger

app = Quart(__name__)

# ── Active context queues (one per running job, keyed by URL) ─────────────────
_context_queues: dict[str, asyncio.Queue] = {}

# ── Pending NEEDS_INPUT signal — replayed to new SSE listeners if still awaiting ──
# Stores the full raw signal string (e.g. "NEEDS_INPUT:msg|label|placeholder")
_pending_input_signal: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
async def index():
    return await render_template("index.html")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Kick off the automation (single browser session)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/start-test", methods=["POST"])
async def start_test():
    data = await request.get_json()
    url              = data.get("url", "").strip()
    custom_tests_raw = data.get("custom_tests", "")
    run_audit        = data.get("run_audit", True)
    run_functional   = data.get("run_functional", True)
    run_probes       = data.get("run_probes", True)
    max_pages        = data.get("max_pages", 1)
    if not url:
        return jsonify({"error": "URL is required"}), 400

    q = asyncio.Queue()
    _context_queues[url] = q

    asyncio.create_task(run_background_automation(url, q, custom_tests_raw, run_audit, run_functional, run_probes, max_pages))
    return jsonify({"status": "started"})


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 (optional): User submits credentials/context after NEEDS_INPUT prompt
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/submit-context", methods=["POST"])
async def submit_context():
    global _pending_input_signal
    data    = await request.get_json()
    url     = data.get("url", "").strip()
    context = data.get("context", "")
    q = _context_queues.get(url)
    if q:
        _pending_input_signal = None   # Clear the pending signal — user has responded
        await q.put(context)
        return jsonify({"status": "context received"})
    return jsonify({"error": "No active session for this URL"}), 404


# ─────────────────────────────────────────────────────────────────────────────
# Background runner — one browser session, analyze → (optional pause) → run
# ─────────────────────────────────────────────────────────────────────────────
async def run_background_automation(url: str, context_queue: asyncio.Queue,
                                    custom_tests_raw: str = "",
                                    run_audit: bool = True,
                                    run_functional: bool = True,
                                    run_probes: bool = True,
                                    max_pages: int = 1):
    global _pending_input_signal
    await stream_logger.log("--- INIT ---")

    # Intercept stream_logger messages to capture and persist the NEEDS_INPUT signal
    # so it can be replayed to new SSE clients that connect while we are paused.
    _orig_log = stream_logger.log
    async def _intercepting_log(msg: str):
        global _pending_input_signal
        if msg.startswith("NEEDS_INPUT:"):
            _pending_input_signal = msg   # Store for replay to late-connecting clients
        await _orig_log(msg)
    stream_logger.log = _intercepting_log

    try:
        report = await run_automation(url, is_html=False,
                                      custom_tests_raw=custom_tests_raw,
                                      run_audit=run_audit,
                                      run_functional=run_functional,
                                      run_probes=run_probes,
                                      max_pages=max_pages,
                                      context_queue=context_queue)
    finally:
        stream_logger.log = _orig_log   # Always restore original logger
        _pending_input_signal = None    # Clear pending signal when automation ends
        _context_queues.pop(url, None)

    if report:
        await stream_logger.log("--- COMPLETE ---")
    else:
        await stream_logger.log("--- FAILED ---")


# ─────────────────────────────────────────────────────────────────────────────
# SSE: Real-time log streaming
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/stream-logs")
async def stream_logs():
    async def sse_generator():
        q = stream_logger.listen()
        try:
            # Immediately replay any pending NEEDS_INPUT signal to this new client.
            # This ensures the context box appears even if the client connected late.
            if _pending_input_signal:
                lines = _pending_input_signal.splitlines()
                sse_data = "\n".join(f"data: {line}" for line in lines)
                yield f"{sse_data}\n\n"

            while True:
                message = await q.get()
                # SSE requires 'data: ' prefix for every line of a multi-line payload
                lines = str(message).splitlines()
                if not lines:
                    continue
                sse_data = "\n".join(f"data: {line}" for line in lines)
                yield f"{sse_data}\n\n"
        except asyncio.CancelledError:
            stream_logger.remove_listener(q)
            raise

    response = await make_response(
        sse_generator(),
        {
            "Content-Type":     "text/event-stream",
            "Cache-Control":    "no-cache",
            "Transfer-Encoding":"chunked",
            "Connection":       "keep-alive",
        }
    )
    response.timeout = None
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Report History
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/reports", methods=["GET"])
async def list_reports():
    reports_dir = "reports"
    if not os.path.exists(reports_dir):
        return jsonify([])
    report_list = []
    for folder in sorted(os.listdir(reports_dir), reverse=True):
        folder_path = os.path.join(reports_dir, folder)
        if not os.path.isdir(folder_path):
            continue
        json_path = os.path.join(folder_path, "report.json")
        if not os.path.exists(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            report_list.append({
                "folder":          folder,
                "date":            d.get("generated_at", "Unknown"),
                "url":             d.get("target_url", folder),
                "vulnerabilities": d.get("summary", {}).get("vulnerabilities_found", 0),
                "passed":          d.get("summary", {}).get("successful_actions", 0),
                "failed":          d.get("summary", {}).get("failed_actions", 0),
            })
        except Exception:
            pass
    return jsonify(report_list)


@app.route("/api/reports/<folder>/view")
async def view_report(folder):
    json_path = os.path.join("reports", folder, "report.json")
    if not os.path.exists(json_path):
        return "Report not found", 404
    with open(json_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    return await render_template("report.html", report=report, folder=folder)


@app.route("/api/reports/<folder>/json")
async def get_report_json(folder):
    path = os.path.join("reports", folder, "report.json")
    if os.path.exists(path):
        return await send_file(path, mimetype="application/json")
    return jsonify({"error": "Not found"}), 404


@app.route("/api/reports/<folder>/planned")
async def get_report_planned(folder):
    path = os.path.join("reports", folder, "test_cases_planned.txt")
    if os.path.exists(path):
        return await send_file(path, as_attachment=True,
                               attachment_filename="test_cases_planned.txt")
    return jsonify({"error": "Planned test cases not found"}), 404


@app.route("/api/reports/<folder>/txt")
async def get_report_txt(folder):
    path = os.path.join("reports", folder, "test_cases_report.txt")
    if os.path.exists(path):
        return await send_file(path, as_attachment=True,
                               attachment_filename="test_cases_report.txt")
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
