"""Synthetic-only tests for explicit external integration authorization.

Never contact the configured synthetic endpoints and never load live secrets.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "integration" / "conftest.py"
_SPEC = importlib.util.spec_from_file_location("controlled_live_fixtures", FIXTURES)
assert _SPEC and _SPEC.loader
fixture_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture_module)

TARGETS = {
    "ASSISTX_URL": "http://localhost:8000",
    "ROUTER_URL": "http://localhost:8088",
    "ASSIGN_URL": "http://localhost:8090",
}


def _set_synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_ASSIGN_LIVE_INTEGRATION_APPROVED", "1")
    monkeypatch.setenv("AUTO_ASSIGN_API_TOKEN", "synthetic-never-valid")
    for name, url in TARGETS.items():
        monkeypatch.setenv(name, url)


def test_live_network_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_synthetic(monkeypatch)
    monkeypatch.delenv("AUTO_ASSIGN_LIVE_INTEGRATION_APPROVED")
    with pytest.raises(RuntimeError, match="NOT authorized"):
        fixture_module._live_configuration()


@pytest.mark.parametrize("missing", list(TARGETS))
def test_all_three_explicit_targets_required(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_synthetic(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match="explicit HTTP"):
        fixture_module._live_configuration()


@pytest.mark.parametrize(
    "invalid",
    [
        "localhost:8090",
        "file:///tmp/synthetic",
        "https://user:fake@example.invalid",
        "https://example.invalid/path?fake=1",
        "https://example.invalid/path#fake",
    ],
)
def test_url_must_not_hide_credentials_or_use_implicit_scheme(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    _set_synthetic(monkeypatch)
    monkeypatch.setenv("ASSIGN_URL", invalid)
    with pytest.raises(RuntimeError, match="ASSIGN_URL"):
        fixture_module._live_configuration()


def test_token_must_be_explicit_even_if_local_env_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_synthetic(monkeypatch)
    monkeypatch.delenv("AUTO_ASSIGN_API_TOKEN")
    (tmp_path / ".env").write_text("AUTO_ASSIGN_API_TOKEN=synthetic-stale-token\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="explicitly injected"):
        fixture_module._live_configuration()


def test_explicit_synthetic_configuration_is_not_live_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_synthetic(monkeypatch)
    targets, token = fixture_module._live_configuration()
    assert targets == TARGETS
    assert token == "synthetic-never-valid"


def test_fixture_source_does_not_read_checkout_env() -> None:
    text = FIXTURES.read_text()
    assert "with open(" not in text
    assert "env_file =" not in text
    assert 'os.getenv("ASSIGN_URL", "http://localhost' not in text
