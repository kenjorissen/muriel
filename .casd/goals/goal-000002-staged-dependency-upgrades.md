# Staged dependency upgrades

## Outcome and human direction

Integrate the upgrades proposed by the existing Dependabot PRs in independently reviewable increments, preserving Muriel behavior, then verify the resulting runtime on isolated staging. The human explicitly selected staging first, no production deployment, and authorized decomposition and carrying the work through without routine approval stops. That direction includes normal planning review/publication, implementation/revision, passing Candidate acceptance, pushes and GitHub verification, and staged verification within scope. It does not authorize weakening checks, changing product semantics, replacing data, bypassing sudo, or production deployment.

The starting baseline is `9e16310d1c78ace9dd60f5d2342a21c8606563a8`. Both GitHub jobs passed in https://github.com/kenjorissen/muriel/actions/runs/42657266122 . Read-only staging inspection subsequently confirmed that release active, health/sign-in/static responses, the staging marker, and its private port-10000 route without Funnel. This was infrastructure/unauthenticated HTTP verification, not an authenticated scheduling smoke test.

## Upgrade inventory

The initial captured proposals are PR #1 (`813f366a291f27db7ea8bea682012f98f9eeb570`) and PR #2 (`bfb734df6079ecb45b51591eee96ed744d2737a6`) in `kenjorissen/muriel`:

- actions/checkout, actions/setup-python, actions/setup-node: v6 to v7.
- Ruff: 0.16.3 to 0.17.4, development-only.
- WhiteNoise: 6.11.0 to 6.12.0.
- psycopg[binary]: 3.2.10 to 3.3.8.
- Gunicorn: 23.0.0 to 26.2.0.
- Django: 5.2.17 to 6.1.3.
- pypdf: 6.21.4 to 7.0.0.
- ReportLab: 4.4.10 to 5.1.0.
- arabic-reshaper: 3.0.1 to 3.1.0; python-bidi: 0.6.11 to 0.6.12.

These are version-update proposals, not proof that each current version has an applicable vulnerability. Keep vulnerability evidence distinct from version currency. Upstream security fixes may exceed the audit database's current findings. Do not change target versions merely because Dependabot later refreshes its branch without assessing the change.

## Success conditions

- Every inventory item has been delivered through accepted ordinary Tasks with preserved behavior, compatible declared dependencies and applicable upstream migration notes considered. Real compatibility or security blockers are surfaced rather than hidden.
- Exact-revision GitHub complete checks and dependency-security jobs pass after integration; strict auditing and browser coverage remain enabled.
- Runtime upgrade increments receive direct owning compatibility evidence and staged verification proportionate to their boundary: static delivery, database connectivity/transactions/migrations, foreground server behavior, framework/authentication, and PDF/text artifacts as applicable. SQLite tests alone do not establish PostgreSQL compatibility.
- Existing staging records, accounts, protected configuration and isolation remain intact. Verification that needs writes uses explicit test/disposable resources or separately authorized staging smoke operations, not an implicit reset or copied production data.
- After equivalent reviewed changes are pushed and verified, reconcile the original Dependabot PRs as superseded rather than merging their broad bundle again; retain clear links to delivered evidence. Do not disable alerts or upgrade monitoring to make the PR list quiet.
- No production rollout occurs under this Goal.

## Coordination and future decomposition

The first published batch contains separate workflow and development-tool upgrades, each with directly observable acceptance. Neither changes runtime behavior or needs a staging restart solely to prove a development tool. Inspect directly relevant upstream release/migration contracts and narrow owning code/tests before adding runtime Tasks; the inventory is not itself a published implementation contract.

Use the existing canonical topic contracts, `docs/production.md`, `docs/staging.md` and `docs/deployment.md`. Preserve single-host Python 3.14, PostgreSQL 18 and SQLite-development support unless a separately accepted change says otherwise. Do not convert a library upgrade into a new server topology, framework feature rollout, PDF redesign, test platform, or global refactor.

The coordinator runs sequential Attempts, inspects joined evidence, accepts only passing Candidates without unresolved concerns, and verifies authorized remote runs. Runtime increments are staged in bounded groups with larger framework/server changes separately attributable; stop before proceeding past an unresolved compatibility failure. Existing operator-local staging selection is explicitly sourced in a subshell; server secrets stay on the server. An interactive sudo prompt is a human boundary, not permission to bypass it. Do not claim authenticated smoke coverage without actual evidence.

## Tasks

- [Task 000006: GitHub Actions v7](../tasks/task-000006-github-actions-v7.md)
- [Task 000007: Ruff 0.17.4](../tasks/task-000007-ruff-0174.md)
