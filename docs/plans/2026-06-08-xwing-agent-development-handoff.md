# Xwing-First Agent Development Handoff

_Last updated: 2026-06-08_

## Purpose

This is the ready-to-start handoff for development agents working across Sophia, auto-assist, auto-router, and auto-assign. It captures the live fleet state verified on 2026-06-08 and defines how xwing should be used as the first active Hermes worker while auto-router/auto-assign direct-worker support is implemented.

## Verified live readiness

Verified from x1-370 over Tailscale SSH to `scott@100.108.99.47` on 2026-06-08 10:43-10:45 Eastern:

| Node | Tailscale URL | Status | Notes |
|---|---:|---|---|
| xwing | `http://100.108.99.47:1234/v1` | online | Fresh Ubuntu replacement node. LM Studio is listening on `0.0.0.0:1234`; Hermes gateway and CLI processes are running. |
| Scott's MacBook Air | `http://100.85.64.117:1234/v1` | online from xwing | Good quick-draft / Sophia response-prep endpoint. |
| deathstar-XPS-8920 | `http://100.78.106.121:1234/v1` | online from xwing | Good VRAM-fit fallback and ingest-adjacent local work. |
| x1-370 | `http://100.64.43.123:1234/v1` | not reachable from xwing during this probe | Treat as degraded for xwing-routed LM Studio traffic until the listener/firewall/config is fixed; do not block agent kickoff on it. |

xwing repo sync state at the same probe:

| Repo on xwing | Branch | State |
|---|---|---|
| `~/git/Sophia` | `master` | clean |
| `~/git/auto-assist` | `main` | clean |
| `~/git/auto-router` | `main` | clean |
| `~/git/auto-assign` | `main` | clean |
| `~/git/auto-ingest` | missing | Not required for the first direct-worker implementation tasks; clone/sync later if ingest tasks are assigned. |

Loaded LM Studio model inventory visible from xwing:

- `google/gemma-4-12b` - current xwing default; use for normal local agent chat, small implementation tasks, and quick review.
- `qwen/qwen3.6-35b-a3b` - stronger local reasoning/MoE option when loaded and responsive.
- `qwen3.6-27b-claude-opus-sonnet-distilledv2-mtp` - stronger reasoning/distilled option when loaded and responsive.
- `qwen3.5-4b-uncensored-hauhaucs-aggressive` - lightweight local fallback; use only for low-risk drafts/triage.
- `liquid/lfm2.5-1.2b` - fastest ideation/outline fallback; not a sole implementation reviewer.

## Execution policy for agents

1. Start development work from xwing unless a task explicitly requires x1-370 services, deathstar-local data, or MacBook Air latency.
2. Use `google/gemma-4-12b` as the baseline xwing model for normal local implementation tasks.
3. Prefer xwing for repo-local code edits, tests, docs sync, and direct-worker adapter work because the repos are clean and the host is fresh.
4. Escalate to MacBook Air only for short draft/response-prep loops.
5. Escalate to deathstar for VRAM-fit local generation or legacy ingest/Neo4j-adjacent work.
6. Escalate to x1-370 for final private heavy reasoning and graph/service integration review, but only after verifying the endpoint is reachable from the requesting node.
7. Keep voice/auth payloads, Signal content, credentials, and raw personal graph context local-only. Do not route those through public APIs.
8. Direct worker execution must emit traceable route/assignment/heartbeat/completion events; no repo should invent worker status labels without a claim/lease event.

## Recommended agent kickoff sequence

Use this from the control node or an operator terminal after pulling the docs commits onto xwing:

```bash
# On xwing
cd ~/git/auto-assist && git pull --ff-only
cd ~/git/auto-router && git pull --ff-only
cd ~/git/auto-assign && git pull --ff-only

# Verify local model inventory
curl -fsS http://127.0.0.1:1234/v1/models | jq '.data[].id'

# Verify remote fallback endpoints from xwing
for host in 100.85.64.117 100.78.106.121 100.64.43.123; do
  echo "== $host =="
  curl -fsS --max-time 4 "http://$host:1234/v1/models" | jq -r '.data[].id' || true
done
```

Then start with the direct-worker implementation sequence:

1. auto-assist: implement/verify canonical trace events and `GET /api/traces/{correlation_id}`.
2. Sophia: make the execution trace view read AssistX trace state after dispatch.
3. auto-router: ensure route decisions include `target_node_id`, `target_service`, `model`, lane, and privacy/local-only reason metadata.
4. auto-assign: implement worker assignment/claim/lease/heartbeat surfaces using xwing as the first direct worker candidate, but keep dispatch dry-run until approval/sandbox policy is satisfied.
5. End-to-end: voice event -> AssistX dispatch -> route selected -> xwing assignment claimed -> heartbeat -> completion/artifact refs.

## Acceptance criteria for ready-to-run agents

- xwing can pull the latest docs/code without dirty-state conflicts.
- xwing can reach its own LM Studio endpoint and at least one fallback endpoint.
- auto-router exposes xwing as an available local model/service candidate in context projection or bootstrap config.
- auto-assign can score xwing as a direct-worker candidate but blocks mutation unless lease, approval, and sandbox constraints are met.
- AssistX trace view can show route/assignment/heartbeat/completion events by `correlation_id` without raw private payload leakage.
