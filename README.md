# auto-assign — retired by fleet reconciliation

> **Do not deploy this service on branch `full-auto-reconciliation-20260730`.**

`auto-assign` duplicated assignment, scheduler, lease, heartbeat, approval, and stale-recovery responsibilities that are now implemented inside AssistX. Running both services creates split scheduling authority even when both write events to Neo4j.

## Authoritative replacement

`scottjoyner/auto-assist` owns:

- task eligibility and priority;
- node/model allocation scoring;
- atomic reservations;
- claims and claim fencing;
- leases and heartbeats;
- stale-claim reconciliation;
- checkpoint, preemption, and migration;
- approval and recovery policy;
- canonical assignment provenance in Neo4j.

`scottjoyner/auto-router` is limited to an offline OpenAI-compatible gateway and admission controller. It does not own assignment or worker lifecycle.

## Migration rule

1. Stop and disable the `auto-assign` process/container.
2. Run AssistX directly or with the router-only overlay.
3. Preserve the local SQLite database only for incident analysis; it is not imported as authority.
4. Port reusable deterministic scorer tests, event fixtures, and heartbeat edge cases into `auto-assist`.
5. Do not add new runtime features here.
6. Archive the repository after the useful tests/contracts have been absorbed.

## Historical material

The existing source and documents remain in Git history for migration reference. The former architecture described this service as the assignment governor between AssistX and auto-router; that boundary has been superseded because AssistX now contains the canonical allocation, reservation, claim, lease, reconciliation, migration, and recovery loops.

See `auto-assist/docs/FULL_AUTO_RECONCILIATION_20260730.md` on the matching reconciliation branch for the authoritative repository map and acceptance gates.
