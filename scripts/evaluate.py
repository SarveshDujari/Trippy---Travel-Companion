"""Run reproducible product scenarios and write an honest workflow scorecard."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/evaluation"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = OUTPUT / "scenarios.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_travel_scenarios.py",
            "tests/test_assistant_experience.py",
            "tests/test_family_images.py",
            "tests/test_real_comparison.py",
            f"--junitxml={report}",
        ],
        cwd=ROOT,
        check=False,
    )
    groups = {}
    for case in ET.parse(report).iter("testcase"):
        group = case.attrib.get("classname", "unknown")
        counts = groups.setdefault(group, {"passed": 0, "failed": 0, "skipped": 0})
        outcome = (
            "failed"
            if case.find("failure") is not None or case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        counts[outcome] += 1
    score = {
        "evaluation": "Deterministic workflow and provider-controlled scenarios",
        "live_model_quality_evaluated": False,
        "categories": groups,
        "totals": {k: sum(v[k] for v in groups.values()) for k in ["passed", "failed", "skipped"]},
    }
    (OUTPUT / "scorecard.json").write_text(json.dumps(score, indent=2), encoding="utf-8")
    print(json.dumps(score, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
