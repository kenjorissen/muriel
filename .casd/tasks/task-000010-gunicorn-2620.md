# Gunicorn 26.2.0

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective

Upgrade Gunicorn from 23.0.0 to 26.2.0 while preserving Muriel's existing foreground synchronous WSGI service and trusted proxy boundary. This does not adopt Gunicorn's new management interfaces, ASGI, HTTP/2, or worker infrastructure.

## Contracts and owning evidence

- `docs/design.md` section 26, commissioned deployment direction; `docs/production.md`, Service command; `docs/deployment.md`, Production service restart and Staging update behavior; `docs/staging.md`, fixed loopback service and isolated identity; `docs/public-edge.md`, public edge and private Admin transport. Preserve systemd process ownership, production Unix socket permissions and staging loopback binding, exact-host/proxy handling, foreground standard-stream logging, and finite graceful shutdown.
- Owners: `requirements.txt`, `scripts/start-production`, `preceptor_project/wsgi.py`, `preceptor_project/production_proxy.py`, `preceptor_project/production_settings.py`, `preceptor_project/staging_settings.py`, and `ops/systemd/muriel.service` / `muriel-staging.service`. The same launcher starts both installations; service identities, paths, and topology are not being changed.
- Existing evidence: `schedules/tests/test_production_runtime.py` / `ProductionLauncherTests` verifies launcher options with a fake interpreter; `schedules/tests/test_production_settings.py` verifies secure proxy handling with Django's test client; `schedules/tests/test_public_edge.py` / `PrivateAdminTransportTests` verifies Admin/header and secure-session policy. These remain useful but do not establish the real upgraded server's process or peer-address behavior.
- Captured target is the Gunicorn portion of Dependabot PR #2 revision `bfb734df6079ecb45b51591eee96ed744d2737a6`. Consult https://github.com/benoitc/gunicorn/blob/26.2.0/docs/content/2026-news.md and https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/config.py . Intervening versions change HTTP parsing/body framing, add a default control socket, and introduce optional worker/protocol features. Removed Eventlet is not used by Muriel. Assess only applicable changes; do not weaken upstream protocol/security validation to mimic older invalid-request acceptance.

## Acceptance

1. Pin and install Gunicorn 26.2.0 in the shared main virtualenv; preserve all other dependency pins and accepted outcomes. Keep two synchronous workers by default, existing bounded overrides, foreground execution, log destinations, timeouts and root refusal.
2. Production retains its existing `/run/muriel/gunicorn.sock`/umask contract and staging its `127.0.0.1:8082` service binding. No new listener, alternate protocol, worker class, dirty arbiter, metrics service or administration surface is adopted. Explicitly disable the newly default-on Gunicorn control socket through its supported setting in the shared launcher, preserving systemd as the existing management owner; document that compatibility choice narrowly in the service runbook.
3. Real upgraded-server evidence verifies usable ordinary HTTP requests through disposable Unix and loopback listeners, and that Gunicorn's Unix peer representation still satisfies the existing production middleware trust gate. Under that gate, trusted Unix-proxy HTTPS/Admin headers retain their intended behavior while network-peer spoofed headers are not accepted as production proxy authority. Do not confuse staging's separate trusted loopback setup with production's Unix-only rule.
4. The real foreground server starts and exits cleanly under its supported graceful termination mechanism with owned workers and listener resources accounted for. Standard HTTP/1 application responses and streaming/static-file behavior used by Muriel remain usable; changed invalid-request rejection is not a regression to undo. Preserve existing login/CSRF/secure-cookie protection through the owning application tests.
5. `scripts/quality-check` and `scripts/check` pass unchanged in policy. Make only compatibility changes and proportionate test/runbook updates needed for this upgrade. Escalate consequential application, security, lifecycle or configuration changes rather than broadening this Task.

## Verification and boundary

Run existing launcher, production-settings and private-Admin tests. Supplement their mocks with bounded direct execution of the installed Gunicorn and application WSGI/middleware in a disposable fixture; a package version or `--check-config` pass alone is insufficient. Keep production paths and launcher's fixed-bind guard intact: use temporary Unix sockets, loopback ephemeral ports and disposable settings/data for direct package execution rather than relaxing live-launcher restrictions for tests. Record effective settings and observed peer/header, HTTP and shutdown results. Equivalent valid framing, headers and internal library implementation remain allowed; do not assert incidental log prose, PIDs, exact timing or unrelated feature absence. Protect the intentional no-extra-management-socket boundary, not a generic inventory of Gunicorn features.

Use the shared main interpreter for project commands. The worker owns only its explicitly created local test server handles, workers, sockets, temporary settings/data and directories; close requests, terminate/await those owned servers, and clean those resources on success or failure. Do not signal Cassidy processes or search host-wide for cleanup targets. No production/staging configuration, service, login or data access, remote push, PR disposition or deployment is authorized to the worker.

The coordinator separately owns exact-revision GitHub checks and non-reset staging deployment/readiness/HTTP verification, attributable to this server increment before the later Django upgrade. Local fixture success is not a live deployment or authenticated scheduling smoke test. No production rollout.

## Execution strategy

Start with release notes and effective configuration: the new control socket is enabled by default even with ordinary synchronous workers, so a bare pin change would alter the existing process/resource contract. Keep its supported disable setting with the pin and owning launcher evidence. Exercise real Gunicorn with disposable Unix and loopback transports before the complete suite, because mocked argv and injected REMOTE_ADDR cannot prove server-to-middleware compatibility. Reuse current security tests for full authentication semantics rather than constructing a second authentication harness. Record and await cleanup separately from response success. Other verification arrangements are allowed if they establish the same behavior without changing the runtime topology or weakening isolation.
