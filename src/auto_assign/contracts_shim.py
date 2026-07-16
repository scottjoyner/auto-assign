"""Local shim mirroring the auto-assist shared contract package.

auto-assist owns the canonical envelope at
``src/assistx/contracts/event_envelope.py`` (symlinked hub). To avoid a
cross-repo import dependency during the unification transition, this module
re-declares the shared field names / enum values locally. The shapes are kept
in sync with the contract so a future swap is a one-line import change.

Contract source of truth (do NOT diverge):
  - EventEnvelope: schema_version, source_repo, event_type, correlation_id
    (required UUID), actor, ts, payload, links
  - AuthState taxonomy, Actor, EventLink, TraceEvent, TraceGroup

See docs/LLD_UNIFIED_FLEET.md §1 and §2 (Trace Observability / G1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Lane


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


# Re-export the shared Lane enum from the local models so consumers have a single
# import surface for "contract-shaped" types.
__all__ = [
    "AuthState",
    "Actor",
    "EventLink",
    "ContractEventEnvelope",
    "Lane",
]
