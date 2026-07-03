"""Smoke test for the recording-friendly CLI wrapper in scripts/."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_demo_scenario.py"


def test_headless_scenario_runs_and_reports_summary():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--speed", "0", "--no-dashboard"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    out = result.stdout
    assert "SCENARIO SUMMARY" in out
    assert "audit chain" in out
    assert "breaker trips" in out
