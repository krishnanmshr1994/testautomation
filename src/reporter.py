import json
import os
from datetime import datetime
from src.planner import TestPlan


def save_test_cases(plan: TestPlan, output_dir: str = "reports/test_cases"):
    """
    Saves the generated test cases from the plan to a human-readable text file.
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"test_cases_{timestamp}.txt")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("  GENERATED TEST CASES\n")
        f.write(f"  Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")

        happy_paths = [i for i in plan.intents if not i.is_security_probe]
        security_probes = [i for i in plan.intents if i.is_security_probe]

        f.write(f"Total Test Cases: {len(plan.intents)}\n")
        f.write(f"  - Functional Tests: {len(happy_paths)}\n")
        f.write(f"  - Security Probes: {len(security_probes)}\n\n")

        f.write("-" * 60 + "\n")
        f.write("FUNCTIONAL TESTS\n")
        f.write("-" * 60 + "\n")
        for idx, intent in enumerate(happy_paths, 1):
            f.write(f"\nTC-{idx:03d}: {intent.description}\n")
            f.write(f"  Expected: {intent.expected_outcome}\n")

        f.write("\n" + "-" * 60 + "\n")
        f.write("SECURITY PROBE TESTS\n")
        f.write("-" * 60 + "\n")
        for idx, intent in enumerate(security_probes, 1):
            f.write(f"\nSEC-{idx:03d}: {intent.description}\n")
            f.write(f"  Expected: {intent.expected_outcome}\n")

    print(f"Test cases saved to: {filepath}")
    return filepath


def generate_report(results: list, audit_result, plan: TestPlan = None, output_dir: str = "reports"):
    """
    Generates structured JSON and Markdown reports, and saves test cases to a text file.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Save test cases to text file if plan is provided
    if plan:
        save_test_cases(plan, output_dir=os.path.join(output_dir, "test_cases"))

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
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4)

    # 2. Markdown Report
    md_path = os.path.join(output_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# QA & Security Automation Report\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

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
            if r.get("error"):
                f.write(f"- **Error**: {r['error']}\n")
            f.write(f"- **Verification Success**: {r['verification_success']}\n")
            f.write(f"- **Details**: {r['details']}\n\n")

    print(f"\nReports generated in: {output_dir}/")
    print(f"  - {json_path}")
    print(f"  - {md_path}")
    return report_data
