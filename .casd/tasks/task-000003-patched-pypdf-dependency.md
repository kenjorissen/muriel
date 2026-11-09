# Patched pypdf dependency

Goal: [goal-000001](../goals/goal-000001-clean-ci-deployment-baseline.md)

## Objective

Replace the vulnerable pypdf pin with a compatible patched release while preserving existing PDF output and verification behavior and retaining the declared-dependency security gate.

## Context and authoritative contracts

- `README.md`, Dependency updates and security monitoring: `scripts/security-audit` audits the declared application requirements and their resolved closure, not an arbitrary installed environment; findings must fail the job.
- `docs/design.md`, Accepted direction: month-calendar Precepting, PDF last-edit paragraphs; sections 5, 21 Master Schedule, 25.1 and 25.2: PDFs are read-only current-corrected outputs, with their existing content, provenance, metadata and scope contracts. Do not change these contracts to accommodate a library update.
- `docs/design.md` sections 26 and 26.3, and root `AGENTS.md`: conventional stack and project check/virtualenv conventions.
- Owners: `requirements.txt`, `scripts/security-audit`, `.github/workflows/clean-environment-checks.yml`; PDF consumers in `schedules/tests/test_precepting_calendar_pdf.py`, `test_master_schedule_workspace.py`, `test_schedule_reports.py`, and `test_stipend_reports.py` use PdfReader for generated-artifact verification.
- Main-revision GitHub run https://github.com/kenjorissen/muriel/actions/runs/42634181537 at `95e8cf674896d49a9eb06370bf192baa8b42140b` reported 29 pypdf vulnerabilities against 6.15.0, with listed fixes through 6.21.4. PyPI confirms 6.21.4 exists and supports Python >=3.9. Recheck current audit evidence rather than treating that dated advisory list as exhaustive.
- Dependabot PR #2 proposes nine upgrades, including Django 6.1.3, ReportLab 5.1.0 and pypdf 7.0.0. That broader upgrade batch is not this Task's scope.

## Acceptance

1. Pin a patched compatible pypdf 6.x release in the existing dependency declaration; the intended narrow baseline is 6.21.4 or a later necessary patched 6.x release. If no 6.x release satisfies current findings, surface that fact for a separate major-version decision rather than silently taking the broad Dependabot batch.
2. `./scripts/security-audit` successfully audits the resulting declared application dependency closure with no known findings. Preserve strict auditing, vulnerability visibility and nonzero failures; no advisory ignores, suppressions or removal of the dependency merely to avoid auditing.
3. Existing generated PDF artifact tests continue to establish content, metadata, selected-period/scope, geometry and last-edit/generation-time behavior. Diagnose extraction differences against those obligations instead of bulk-updating expected output. Do not alter production PDF behavior to make parser-specific assertions pass.

## Verification

Run the existing audit and relevant PDF-owning tests using the declared updated dependency in the shared main virtualenv, followed by `./scripts/quality-check` and `./scripts/check`. Existing PDF tests are the primary compatibility evidence; a new test that only asserts the version literal is not needed. Record the resolved pypdf version, exact audit result and test results. A clean audit is current dependency evidence, not a general security certification. Do not claim a GitHub run until an explicitly authorized exact revision has been pushed and its jobs inspected.

## Constraints and predecessor

The preceding browser setup and parallel failure-reporting Tasks remain intact. No unrelated dependency upgrades, audit weakening, PDF semantic changes, deployment, remote push, PR merge, or data changes. If the audit reveals unrelated dependency findings, report them for scope judgment rather than hiding them. Version choice within the bounded patched 6.x contract remains ordinary implementation discretion.
