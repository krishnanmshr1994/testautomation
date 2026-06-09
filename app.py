import asyncio
import json
from quart import Quart, render_template, request, jsonify, make_response
from main import run_automation
from src.logger import stream_logger

app = Quart(__name__)

@app.route("/")
async def index():
    return await render_template("index.html")

@app.route("/api/start-test", methods=["POST"])
async def start_test():
    data = await request.get_json()
    url = data.get("url")
    context = data.get("context", "")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Run the automation in the background so we don't block the HTTP response
    asyncio.create_task(run_background_automation(url, context))
    return jsonify({"status": "started", "message": f"Testing started for {url}"})

async def run_background_automation(url: str, context: str):
    await stream_logger.log(f"--- INIT ---")
    report = await run_automation(url, is_html=False, extra_context=context)
    if report:
        await stream_logger.log(f"--- COMPLETE ---")
        await stream_logger.log(json.dumps(report)) # Send raw JSON so frontend can render it
    else:
        await stream_logger.log(f"--- FAILED ---")

@app.route("/api/stream-logs")
async def stream_logs():
    async def sse_generator():
        q = stream_logger.listen()
        try:
            while True:
                message = await q.get()
                # Server-Sent Events format
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

import os
from quart import send_file

@app.route("/api/reports", methods=["GET"])
async def list_reports():
    """Returns a list of all executed reports."""
    reports_dir = "reports"
    if not os.path.exists(reports_dir):
        return jsonify([])
    
    report_list = []
    # Sort folders by name descending (newest timestamp first)
    folders = sorted(os.listdir(reports_dir), reverse=True)
    
    for folder in folders:
        folder_path = os.path.join(reports_dir, folder)
        if os.path.isdir(folder_path):
            json_path = os.path.join(folder_path, "report.json")
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        report_list.append({
                            "folder": folder,
                            "date": data.get("generated_at", "Unknown"),
                            "url": data.get("target_url", folder.split("_", 2)[-1]),
                            "vulnerabilities": data.get("summary", {}).get("vulnerabilities_found", 0),
                            "passed": data.get("summary", {}).get("successful_actions", 0),
                            "failed": data.get("summary", {}).get("failed_actions", 0)
                        })
                except Exception as e:
                    pass
    return jsonify(report_list)

@app.route("/api/reports/<folder>/json", methods=["GET"])
async def get_report_json(folder):
    """Returns the raw JSON for a specific report."""
    path = os.path.join("reports", folder, "report.json")
    if os.path.exists(path):
        return await send_file(path, mimetype="application/json")
    return jsonify({"error": "Report not found"}), 404

@app.route("/api/reports/<folder>/txt", methods=["GET"])
async def get_report_txt(folder):
    """Downloads the test_cases_report.txt."""
    path = os.path.join("reports", folder, "test_cases_report.txt")
    if os.path.exists(path):
        return await send_file(path, as_attachment=True, attachment_filename="test_cases_report.txt")
    return jsonify({"error": "Report not found"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
