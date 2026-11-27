"""Hermetic contract tests for the operator assignment-summary recommendations.

A control mode that is not one of the four known modes still produces a
fail-closed decision state (every ``*_allowed`` flag False).  The operator
summary must therefore never report such a state as ``steady_state`` with the
reason "no ... control risks detected" .

Pure helpers only: no HTTP, no SQLite file, no network, no live service.
"""

from __future__ import annotations

import pytest

from auto_assign.control import control_state_for_mode
from auto_assign.main import _assignment_summary_recommendations

_CLEAN_OPS = {
    "outbox_by_status": {},
    "assignments_by_status": {},
}


def _recs(mode: object) -> list[dict]:
    control = control_state_for_mode(mode)  # type: ignore[arg-type]
    return _assignment_summary_recommendations(
        ops=_CLEAN_OPS,
        stale_count=0,
        dispatch_enabled=True,
        direct_workers_enabled=True,
        control=control,
    )


def _actions(mode: object) -> set[str]:
    return {rec["action"] for rec in _recs(mode)}


# --- fail-before: the defect -------------------------------------------------


@pytest.mark.parametrize("mode", ["paused_v2", "PAUSED", "future-mode", ""])
def test_unknown_control_mode_is_never_reported_as_steady_state(mode: str) -> None:
    actions = _actions(mode)
    assert "steady_state" not in actions, (
        f"mode {mode!r} is unrecognized and fail-closed, but summary claimed steady_state"
    )


@pytest.mark.parametrize("mode", ["paused_v2", "PAUSED", "future-mode", ""])
def test_unknown_control_mode_emits_an_explicit_warning(mode: str) -> None:
    recs = _recs(mode)
    warnings = [rec for rec in recs if rec.get("level") == "warning"]
    assert warnings, f"mode {mode!r} produced no operator warning"
    assert any("control" in rec["action"] or "control" in rec["reason"] for rec in warnings), (
        f"warning for {mode!r} does not mention assignment control: {warnings}"
    )


def test_unknown_mode_warning_identifies_the_actual_mode() -> None:
    recs = _recs("paused_v2")
    joined = " ".join(str(rec.get("reason", "")) for rec in recs)
    assert "paused_v2" in joined, f"operator warning omits offending mode: {recs}"


# --- controls: known modes keep their existing recommendation ---------------


def test_enabled_mode_still_reports_steady_state() -> None:
    assert _actions("enabled") == {"steady_state"}


@pytest.mark.parametrize("mode", ["paused", "maintenance"])
def test_paused_modes_still_warn_to_keep_paused(mode: str) -> None:
    assert "keep_assignment_governor_paused" in _actions(mode)


def test_draining_mode_still_warns_to_drain() -> None:
    assert "drain_assignment_governor" in _actions("draining")


def test_unknown_mode_remains_fail_closed_at_decision_boundary() -> None:
    """Guard the behaviour that makes the summary lie: unknown mode disables work."""
    state = control_state_for_mode("paused_v2")
    assert state["assignment_allowed"] is False
    assert state["new_assignments_allowed"] is False
    assert state["scheduler_ticks_allowed"] is False
    assert state["dispatch_allowed"] is False
    assert state["recommended_scheduler_state"] == "paused"
