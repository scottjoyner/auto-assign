# Auto-assign HTTP validation gates

## Hosted CI: synthetic contract verification
The default `AUTO_ASSIGN_INTEGRATION_MODE=synthetic` runs the real
auto-assign ASGI application with an isolated, temporary SQLite database.
AssistX and Router HTTP health responses are synthetic transports. It
executes the seven existing/extended HTTP contract assertions, including
auth rejection, header propagation and the stale-heartbeat response.

This tests application HTTP behavior only. **It does not establish**
three-service startup, real credentials, dispatch eligibility, Docker
network reachability, Tailnet access or a production release.

## Separate controlled staging: real integration
Select `AUTO_ASSIGN_INTEGRATION_MODE=live` only in a protected,
pre-provisioned staging job. Supply `ASSISTX_URL`, `ROUTER_URL`,
`ASSIGN_URL` and `AUTO_ASSIGN_API_TOKEN` through staging-approved
configuration. The tests do not fall back to reading a production .env.

Use a dedicated disposable staging stack: synthetic/test data only,
no production credential or live graph mount, and explicit auth-negative
checks. Do not point this suite at production. The current live suite
expects staging scheduler control mode enabled and dispatch flag true;
do not enable real dispatch merely to make tests pass. Validate those
expectations independently before provisioning the staging stack.

Passing hosted synthetic CI is **not** a production cutover approval.
The Router credential-alignment gate, real positive and negative
consumer tests, independent rollback and supervisor approval must
all be satisfied separately.
