import json
import os

def generate_report(results: list, audit_result, output_dir: str = "reports"):
    """
    Generates structured JSON and Markdown reports.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. JSON Report
    report_data = {
        "static_audit": audit_result.model_dump() if audit_result else None,
        "execution_results": results,
        "summary": {
            "total_actions": len(results),
            "successful_actions": sum(1 for r in results if r.get("action_success")),
            "failed_actions": sum(1 for r in results if not r.get("action_success")),
            "vulnerabilities_found": len(audit_result.vulnerabilities) if audit_result else 0
        }
    }
    
    json_path = os.path.join(output_dir, "report.json")
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=4)
        
    # 2. Markdown Report
    md_path = os.path.join(output_dir, "report.md")
    with open(md_path, "w") as f:
        f.write("# Stagehand QA & Security Automation Report\n\n")
        
        f.write("## Summary\n")
        f.write(f"- Total Actions Executed: {report_data['summary']['total_actions']}\n")
        f.write(f"- Successful Actions: {report_data['summary']['successful_actions']}\n")
        f.write(f"- Failed Actions: {report_data['summary']['failed_actions']}\n")
        f.write(f"- Static Vulnerabilities: {report_data['summary']['vulnerabilities_found']}\n\n")
        
        f.write("## Static Audit Findings\n")
        if audit_result and audit_result.vulnerabilities:
            for v in audit_result.vulnerabilities:
                f.write(f"- **[{v.severity.upper()}]** {v.title}: {v.description}\n")
        else:
            f.write("No static vulnerabilities found.\n")
            
        f.write("\n## Execution Details\n")
        for idx, r in enumerate(results):
            f.write(f"### Step {idx + 1}: {r['intent']['description']}\n")
            f.write(f"- **Action Success**: {r['action_success']}\n")
            if r.get('error'):
                f.write(f"- **Error**: {r['error']}\n")
            f.write(f"- **Verification Success**: {r['verification_success']}\n")
            f.write(f"- **Details**: {r['details']}\n\n")
            
    print(f"\nReports generated at {output_dir}/")
    return report_data
