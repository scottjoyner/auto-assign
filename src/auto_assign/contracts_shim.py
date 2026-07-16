"""Local shim re-exporting the auto-assist shared contract package.

When the canonical package is importable (auto-assist on sys.path as a
submodule/path) this module re-exports the real ``assistx.contracts`` types so
every repo emits the single source-of-truth envelope. Otherwise it falls back to
the local mirror classes below so standalone runs keep working without the
canonical package.

Canonical source of truth:
  /media/scott/SSD_4TB/hermes-home/home_scott_git_auto-assist/src/assistx/contracts/
See docs/LLD_UNIFIED_FLEET.md §1 and §2 (Trace Observability / G1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Lane


try:
    from assistx.contracts.event_envelope import (
        Actor,
        AuthState,
        EventEnvelope,
        EventLink,
        TraceEvent,
        TraceGroup,
    )
    from assistx.contracts.version import SCHEMA_VERSION

    # Canonical-shaped mirror alias used by existing callers.
    ContractEventEnvelope = EventEnvelope
    _USING_CANONICAL = True
except ImportError:

    class AuthState(str, Enum):
        """Mirror of assistx.contracts.AuthState (Sophia §5.6)."""

        AUTHENTICATED_SCOTT = "authenticated_scott"
        UNKNOWN_SPEAKER = "unknown_speaker"
        REGISTERED_USER_UNVERIFIED = "registered_user_unverified"
        ADMIN_VOICE_OVERRIDE = "admin_voice_override"
        REJECTED = "rejected"

    class Actor(BaseModel):
        """Mirror of assistx.contracts.Actor."""

        model_config = ConfigDict(extra="forbid")

        user_id: str = Field(..., description="Stable identifier of the actor.")
        auth_state: AuthState = Field(..., description="Voice/identity auth outcome.")
        display_name: Optional[str] = None

    class EventLink(BaseModel):
        """Mirror of assistx.contracts.EventLink."""

        model_config = ConfigDict(extra="forbid")

        rel: str = Field(..., description="Relationship name, e.g. FOR_TASK.")
        target_type: str = Field(..., description="Entity label, e.g. Task, Dispatch.")
        target_id: str = Field(..., description="Entity id the event is about.")

    class ContractEventEnvelope(BaseModel):
        """Local mirror of assistx.contracts.EventEnvelope.

        ``correlation_id`` is REQUIRED (UUID) — matching the canonical contract so
        trace linkage works uniformly across repos.
        """

        model_config = ConfigDict(extra="forbid")

        schema_version: str = Field(..., description="Contract schema version.")
        source_repo: str = Field(..., description="Originating repo, e.g. auto-assign.")
        event_type: str = Field(..., description="Domain event name.")
        correlation_id: str = Field(
            ...,
            description="Required UUID tying an event to a trace group.",
        )
        actor: Optional[Actor] = None
        ts: datetime = Field(
            default_factory=lambda: datetime.now(timezone.utc),
            description="Event timestamp (UTC).",
        )
        payload: dict[str, Any] = Field(default_factory=dict)
        links: list[EventLink] = Field(default_factory=list)

        @field_validator("correlation_id")
        @classmethod
        def _valid_correlation_id(cls, value: str) -> str:
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError, TypeError):
                raise ValueError("correlation_id must be a valid UUID")
            return value

    # Re-export under the canonical name too, for callers that prefer it.
    EventEnvelope = ContractEventEnvelope

    class TraceEvent:  # pragma: no cover - fallback only
        pass

    class TraceGroup:  # pragma: no cover - fallback only
        pass

    SCHEMA_VERSION = "2026-06-08.v1"

    _USING_CANONICAL = False


# Re-export the shared Lane enum from the local models so consumers have a single
# import surface for "contract-shaped" types.
__all__ = [
    "AuthState",
    "Actor",
    "EventLink",
    "ContractEventEnvelope",
    "EventEnvelope",
    "TraceEvent",
    "TraceGroup",
    "Lane",
    "SCHEMA_VERSION",
    "_USING_CANONICAL",
]
