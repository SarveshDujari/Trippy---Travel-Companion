"""Run the thirty isolated product scenarios and save an honest scorecard."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "data" / "evaluation"
target.mkdir(parents=True, exist_ok=True)
report = target / "scenarios.xml"
result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_travel_scenarios.py",
        "-q",
        f"--junitxml={report}",
    ],
    cwd=ROOT,
    check=False,
)
cases = ET.parse(report).findall(".//testcase")
score = {
    "scope": (
        "Deterministic workflow scenarios with scripted inputs and mocked provider failures; "
        "not a live LLM conversation evaluation"
    ),
    "total": len(cases),
    "passed": sum(
        not any(c.find(x) is not None for x in ["failure", "error", "skipped"]) for c in cases
    ),
    "cases": [
        {
            "name": c.attrib["name"],
            "passed": not any(c.find(x) is not None for x in ["failure", "error", "skipped"]),
        }
        for c in cases
    ],
}
(target / "scorecard.json").write_text(json.dumps(score, indent=2), encoding="utf-8")
print(
    f"Workflow scorecard: {score['passed']}/{score['total']} passed. "
    "Not a live model-quality score."
)
raise SystemExit(result.returncode)
