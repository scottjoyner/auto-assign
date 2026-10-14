"""Source-only CI gate contract; no live network or credential activity."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LIVE_TESTS = ROOT / "tests" / "integration" / "test_health.py"


def test_hosted_unit_lane_excludes_only_live_integration_directory():
    content = WORKFLOW.read_text()
    assert "ruff check src tests" in content
    assert "python -m compileall src tests" in content
    assert "pytest -q --ignore=tests/integration" in content


def test_external_service_gate_is_preserved_and_explicitly_unverified():
    content = WORKFLOW.read_text()
    assert LIVE_TESTS.is_file(), "live integration tests must not be deleted"
    assert "Live AssistX / Router / Auto-Assign integration was NOT executed" in content
    assert "independently authorized running stack" in content
    assert "Do not treat this workflow as live deployment" in content
