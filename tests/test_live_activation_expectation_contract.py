"""Synthetic-only checks of operator-declared activation expectations.

No network requests, credentials, production settings or service mutations.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "integration" / "test_health.py"
_SPEC = importlib.util.spec_from_file_location("activation_acceptance_contract", SOURCE)
assert _SPEC and _SPEC.loader
acceptance = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(acceptance)

OBSERVED_DISABLED_AUTONOMY = {
    "enabled": False,
    "dispatch_enabled": False,
    "direct_workers_enabled": False,
    "control": {
        "mode": "enabled",
        "new_assignments_allowed": True,
        "scheduler_ticks_allowed": True,
    },
}
DECLARED = {
    "AUTO_ASSIGN_EXPECT_SCHEDULER_ENABLED": "false",
    "AUTO_ASSIGN_EXPECT_DISPATCH_ENABLED": "false",
    "AUTO_ASSIGN_EXPECT_DIRECT_WORKERS_ENABLED": "false",
    "AUTO_ASSIGN_EXPECT_NEW_ASSIGNMENTS_ALLOWED": "true",
    "AUTO_ASSIGN_EXPECT_SCHEDULER_TICKS_ALLOWED": "true",
    "AUTO_ASSIGN_EXPECT_CONTROL_MODE": "enabled",
}


def _set_expectations(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in DECLARED.items():
        monkeypatch.setenv(key, value)


def test_no_silent_activation_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in DECLARED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(AssertionError, match="explicitly set"):
        acceptance._read_owner_declared_activation()


@pytest.mark.parametrize("key", tuple(DECLARED))
def test_all_owner_declarations_are_required(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    _set_expectations(monkeypatch)
    monkeypatch.delenv(key)
    with pytest.raises(AssertionError):
        acceptance._read_owner_declared_activation()


@pytest.mark.parametrize("value", ("TRUE", "1", "yes", "", "enabled"))
def test_boolean_declarations_must_be_unambiguous(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _set_expectations(monkeypatch)
    monkeypatch.setenv("AUTO_ASSIGN_EXPECT_DISPATCH_ENABLED", value)
    with pytest.raises(AssertionError, match="AUTO_ASSIGN_EXPECT_DISPATCH_ENABLED"):
        acceptance._read_owner_declared_activation()


def test_readonly_profile_accepts_explicit_disabled_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_expectations(monkeypatch)
    expected = acceptance._read_owner_declared_activation()
    acceptance._assert_owner_declared_activation(OBSERVED_DISABLED_AUTONOMY, expected)
    assert len(expected) == 6


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("enabled", True),
        ("dispatch_enabled", True),
        ("direct_workers_enabled", True),
        ("control.new_assignments_allowed", False),
        ("control.scheduler_ticks_allowed", False),
        ("control.mode", "paused"),
    ),
)
def test_unexpected_activation_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _set_expectations(monkeypatch)
    actual = {
        **OBSERVED_DISABLED_AUTONOMY,
        "control": dict(OBSERVED_DISABLED_AUTONOMY["control"]),
    }
    target = actual
    names = field.split(".")
    for part in names[:-1]:
        target = target[part]
    target[names[-1]] = value
    expected = acceptance._read_owner_declared_activation()
    with pytest.raises(AssertionError, match="differs"):
        acceptance._assert_owner_declared_activation(actual, expected)


def test_missing_control_is_never_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_expectations(monkeypatch)
    actual = dict(OBSERVED_DISABLED_AUTONOMY)
    actual.pop("control")
    with pytest.raises(AssertionError, match="missing"):
        acceptance._assert_owner_declared_activation(
            actual, acceptance._read_owner_declared_activation()
        )


def test_control_policy_boolean_must_be_real_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_expectations(monkeypatch)
    actual = {**OBSERVED_DISABLED_AUTONOMY, "control": dict(OBSERVED_DISABLED_AUTONOMY["control"])}
    actual["control"]["new_assignments_allowed"] = "true"
    with pytest.raises(AssertionError, match="not boolean"):
        acceptance._assert_owner_declared_activation(
            actual, acceptance._read_owner_declared_activation()
        )


def test_missing_profile_refuses_before_health_http(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in DECLARED:
        monkeypatch.delenv(key, raising=False)

    class NeverCalledClient:
        def get(self, *_args, **_kwargs):
            raise AssertionError("network must never be reached")

    with pytest.raises(AssertionError, match="explicitly set"):
        acceptance.test_live_activation_matches_owner_declared_profile(NeverCalledClient())


def test_removed_enablement_assumptions_are_not_reintroduced() -> None:
    text = SOURCE.read_text()
    assert 'assert health["scheduler"]["dispatch_enabled"] is True' not in text
    assert 'assert health["scheduler"]["control"]["mode"] == "enabled"' not in text
