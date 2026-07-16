from __future__ import annotations

from hashlib import sha256

from .events import EventType
from .models import (
    AssignmentCandidate,
    AssignmentDecision,
    AssignmentStatus,
    Lane,
    RouterSnapshot,
    SkipReason,
)
from .settings import Settings

SENSITIVE_PRIVACY_LABELS = {
    "local_only",
    "private",
    "private_data",
    "secret",
    "secrets",
    "voice_auth",
    "enrollment",
    "enrollment_sample",
}
HOSTED_LANES = {Lane.FREE_API}
# NOTE: These status checks mirror AssistX canonical states.
# AssistX Neo4j is the source of truth for task lifecycle.
# These are local cache optimizations to avoid round-trips.
# If there's a mismatch, AssistX's answer wins.
TERMINAL_STATUSES = {"done", "cancelled", "failed_terminal", "terminal", "complete", "completed"}
ACTIVE_ASSIGNMENT_STATUSES = {"reserved", "dispatched", "running", "claimed", "claimed_passive"}


class AssignmentScorer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def score(
        self,
        candidate: AssignmentCandidate,
        router: RouterSnapshot,
        candidate_lanes: list[Lane] | None = None,
        canonical_status: str | None = None,
    ) -> AssignmentDecision:
        lanes = candidate_lanes or candidate.allowed_lanes
        skipped: list[SkipReason] = []
        reasons: list[str] = []
        normalized_status = (canonical_status or "").lower()

        if normalized_status in TERMINAL_STATUSES:
            return self.blocked(
                candidate,
                router,
                canonical_status,
                "canonical_terminal_state",
                f"AssistX/Neo4j reports terminal state: {canonical_status}",
            )
        if normalized_status in ACTIVE_ASSIGNMENT_STATUSES:
            return self.blocked(
                candidate,
                router,
                canonical_status,
                "duplicate_active_assignment",
                f"AssistX/Neo4j reports active assignment state: {canonical_status}",
            )

        if candidate.approval_required or candidate.risk_level.lower() in {"high", "critical"}:
            decision = self._base_decision(candidate, router, canonical_status)
            decision.status = AssignmentStatus.APPROVAL_REQUIRED
            decision.selected_lane = Lane.BLOCKED
            decision.score = 0.0
            decision.approval_required = True
            decision.reasons = ["approval is required before assignment can dispatch"]
            decision.skipped_lanes = [
                SkipReason(lane=lane, reason_code="approval_required", reason="approval required")
                for lane in lanes
            ]
            return decision

        sensitive = self._is_sensitive(candidate)
        local_only = self._is_local_only(candidate)

        scored: list[tuple[float, Lane, str, list[str]]] = []
        for lane in lanes:
            lane_skips = self._skip_reasons(candidate, router, lane, sensitive, local_only)
            if lane_skips:
                skipped.extend(lane_skips)
                continue
            score, lane_reasons = self._score_lane(candidate, router, lane, sensitive, local_only)
            scored.append((score, lane, self._target_for_lane(lane), lane_reasons))

        if not scored:
            decision = self._base_decision(candidate, router, canonical_status)
            decision.status = AssignmentStatus.BLOCKED
            decision.selected_lane = Lane.BLOCKED
            decision.score = 0.0
            decision.reasons = ["no eligible lane available"]
            decision.skipped_lanes = skipped
            return decision

        scored.sort(key=lambda item: item[0], reverse=True)
        score, lane, target, lane_reasons = scored[0]
        reasons.extend(lane_reasons)
        decision = self._base_decision(candidate, router, canonical_status)
        decision.status = AssignmentStatus.RECOMMENDED
        decision.selected_lane = lane
        decision.selected_target = target
        decision.score = round(score, 4)
        decision.reasons = reasons
        decision.skipped_lanes = skipped
        return decision

    def _skip_reasons(
        self,
        candidate: AssignmentCandidate,
        router: RouterSnapshot,
        lane: Lane,
        sensitive: bool,
        local_only: bool,
    ) -> list[SkipReason]:
        skips: list[SkipReason] = []
        if lane in HOSTED_LANES and (sensitive or local_only or not candidate.allow_cloud):
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="privacy_cloud_denied",
                    reason="sensitive, local-only, or cloud-disallowed work cannot use hosted/free API lanes",
                )
            )
        if lane == Lane.PAPERCLIP:
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="deprecated_execution_lane",
                    reason="deprecated execution lane; use router_model, local_only, or free_api",
                )
            )
        if lane == Lane.ROUTER_MODEL and (local_only or not candidate.allow_cloud):
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="router_cloud_denied",
                    reason="router model lane is skipped because the task is local-only or cloud-disallowed",
                )
            )
        if lane == Lane.DIRECT_WORKER and not self.settings.direct_workers_enabled:
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="direct_workers_disabled",
                    reason="direct worker lane is disabled by configuration",
                )
            )
        if lane in {Lane.FREE_API, Lane.ROUTER_MODEL} and not router.reachable:
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="router_unavailable",
                    reason="router snapshot is unavailable; cloud/router lanes are conservatively blocked",
                )
            )
        if lane == Lane.FREE_API and self._quota_preserve_mode(router):
            skips.append(
                SkipReason(
                    lane=lane,
                    reason_code="quota_preserve_mode",
                    reason="quota is in preserve mode for higher-priority traffic",
                )
            )
        return skips

    def _score_lane(
        self,
        candidate: AssignmentCandidate,
        router: RouterSnapshot,
        lane: Lane,
        sensitive: bool,
        local_only: bool,
    ) -> tuple[float, list[str]]:
        base_by_lane = {}
        for pair in self.settings.lane_base_scores.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                lane_key = k.strip().upper()
                try:
                    base_by_lane[Lane[lane_key]] = float(v.strip())
                except (KeyError, ValueError):
                    pass
        score = base_by_lane.get(lane, 0.0)
        reasons = []

        if lane == Lane.LOCAL_ONLY and (sensitive or local_only):
            score += self.settings.local_only_boost
            reasons.append("local-only lane preferred for sensitive or local-only work")
        if lane == Lane.ROUTER_MODEL:
            reasons.append("router model lane is available for planning/drafting/review")
        if lane == Lane.FREE_API:
            reasons.append("free API lane is eligible and does not violate reserve policy")
        if lane == Lane.DIRECT_WORKER:
            reasons.append("direct worker lane is available for local execution")
        if candidate.priority in {"critical", "repo_critical", "interactive"}:
            score += self.settings.priority_boost
            reasons.append(f"priority boost applied for {candidate.priority}")
        if candidate.retry_count:
            penalty = min(candidate.retry_count * self.settings.retry_penalty_per_attempt, self.settings.retry_penalty_max)
            score -= penalty
            reasons.append(f"retry penalty applied: {candidate.retry_count}")
        if router.context_revision:
            reasons.append(f"router context revision used: {router.context_revision}")
        return max(score, 0.0), reasons

    def _target_for_lane(self, lane: Lane) -> str:
        targets = {
            Lane.PAPERCLIP: "deprecated",
            Lane.LOCAL_ONLY: "local_only",
            Lane.ROUTER_MODEL: "auto-router",
            Lane.FREE_API: "auto-router/free_api",
            Lane.DIRECT_WORKER: "direct_worker",
            Lane.BLOCKED: "blocked",
        }
        return targets.get(lane, "unknown")

    def _quota_preserve_mode(self, router: RouterSnapshot) -> bool:
        if not isinstance(router.quota, dict):
            return False
        metadata = router.quota.get("metadata", {})
        mode = metadata.get("mode") or router.quota.get("mode")
        return mode == "preserve"

    def _is_sensitive(self, candidate: AssignmentCandidate) -> bool:
        labels = {str(label).lower() for label in candidate.privacy_labels}
        if candidate.privacy:
            labels.add(candidate.privacy.lower())
        return bool(candidate.sensitive or labels & SENSITIVE_PRIVACY_LABELS)

    def _is_local_only(self, candidate: AssignmentCandidate) -> bool:
        labels = {str(label).lower() for label in candidate.privacy_labels}
        if candidate.privacy:
            labels.add(candidate.privacy.lower())
        return bool(candidate.local_only or "local_only" in labels or "private" in labels or "secret" in labels)

    def _base_decision(
        self,
        candidate: AssignmentCandidate,
        router: RouterSnapshot,
        canonical_status: str | None,
    ) -> AssignmentDecision:
        idempotency_key = self.decision_idempotency_key(candidate.task_id, candidate.status, canonical_status)
        assignment_id = f"assign_{self._digest(candidate.task_id, canonical_status or 'unknown')[:16]}"
        decision_id = f"decision_{self._digest(idempotency_key, router.context_revision or 'no-router-revision')[:16]}"
        return AssignmentDecision(
            assignment_id=assignment_id,
            task_id=candidate.task_id,
            decision_id=decision_id,
            status=AssignmentStatus.RECOMMENDED,
            selected_lane=Lane.BLOCKED,
            context_revision=router.context_revision,
            idempotency_key=idempotency_key,
            canonical_status=canonical_status,
            cache_only=True,
        )

    def blocked(
        self,
        candidate: AssignmentCandidate,
        router: RouterSnapshot,
        canonical_status: str | None,
        reason_code: str,
        reason: str,
    ) -> AssignmentDecision:
        decision = self._base_decision(candidate, router, canonical_status)
        decision.status = AssignmentStatus.BLOCKED
        decision.selected_lane = Lane.BLOCKED
        decision.score = 0.0
        decision.reasons = [reason]
        decision.skipped_lanes = [
            SkipReason(lane=lane, reason_code=reason_code, reason=reason)
            for lane in candidate.allowed_lanes
        ]
        return decision

    def decision_idempotency_key(
        self,
        task_id: str,
        candidate_status: str = "ready",
        canonical_status: str | None = None,
    ) -> str:
        return f"{EventType.ASSIGNMENT_DECISION}:{task_id}:{candidate_status}:{canonical_status or 'unknown'}"

    def _digest(self, *parts: str) -> str:
        joined = "::".join(parts)
        return sha256(joined.encode("utf-8")).hexdigest()
