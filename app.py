import asyncio
import json
import os
from quart import Quart, render_template, request, jsonify, make_response, send_file
from main import run_automation
from src.logger import stream_logger
from src.browser_manager import init_browser, close_browser
from src.planner import analyze_for_required_context

app = Quart(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Analyze the page for required context (login forms, etc.)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
async def analyze():
    """
    Navigates to the URL, inspects the DOM using the LLM,
    and returns whether user credentials/context are required.
    """
    data = await request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL is required"}), 400

    await stream_logger.log(f"Analyzing page: {url}...")
    page = await init_browser(url, is_html=False)
    if not page:
        await close_browser()
        return jsonify({"needs_input": False, "prompt_message": None,
                        "error": "Could not load page"}), 200

    result = await analyze_for_required_context(page)
    await close_browser()

    if result:
        return jsonify({
            "needs_input": True,
            "prompt_message": result["prompt_message"],
            "field_label":    result["field_label"],
            "placeholder":    result["placeholder"]
        })
    return jsonify({"needs_input": False, "prompt_message": None,
                    "field_label": None, "placeholder": None})


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Run the full automation (optionally with user-supplied context)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/start-test", methods=["POST"])
async def start_test():
    data = await request.get_json()
    url = data.get("url")
    context = data.get("context", "")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    asyncio.create_task(run_background_automation(url, context))
    return jsonify({"status": "started", "message": f"Testing started for {url}"})


async def run_background_automation(url: str, context: str):
    await stream_logger.log("--- INIT ---")
    report = await run_automation(url, is_html=False, extra_context=context)
    if report:
        await stream_logger.log("--- COMPLETE ---")
        await stream_logger.log(json.dumps(report))
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
            while True:
                message = await q.get()
                yield f"data: {message}\n\n"
        except asyncio.CancelledError:
            stream_logger.remove_listener(q)
            raise

    response = await make_response(
        sse_generator(),
        {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Transfer-Encoding': 'chunked',
            'Connection': 'keep-alive'
        }
    )
    response.timeout = None
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Report History
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
async def index():
    return await render_template("index.html")

@app.route("/api/reports", methods=["GET"])
async def list_reports():
    reports_dir = "reports"
    if not os.path.exists(reports_dir):
        return jsonify([])

    report_list = []
    folders = sorted(os.listdir(reports_dir), reverse=True)

    for folder in folders:
        folder_path = os.path.join(reports_dir, folder)
        if os.path.isdir(folder_path):
            json_path = os.path.join(folder_path, "report.json")
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        report_list.append({
                            "folder": folder,
                            "date": d.get("generated_at", "Unknown"),
                            "url": d.get("target_url", folder),
                            "vulnerabilities": d.get("summary", {}).get("vulnerabilities_found", 0),
                            "passed": d.get("summary", {}).get("successful_actions", 0),
                            "failed": d.get("summary", {}).get("failed_actions", 0)
                        })
                except Exception:
                    pass
    return jsonify(report_list)

@app.route("/api/reports/<folder>/json", methods=["GET"])
async def get_report_json(folder):
    path = os.path.join("reports", folder, "report.json")
    if os.path.exists(path):
        return await send_file(path, mimetype="application/json")
    return jsonify({"error": "Report not found"}), 404

@app.route("/api/reports/<folder>/txt", methods=["GET"])
async def get_report_txt(folder):
    path = os.path.join("reports", folder, "test_cases_report.txt")
    if os.path.exists(path):
        return await send_file(path, as_attachment=True, attachment_filename="test_cases_report.txt")
    return jsonify({"error": "Report not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
