# ReportLab 5.1.0

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective and predecessors

Upgrade ReportLab 4.4.10 to 5.1.0 while retaining usable, correct on-demand PDF exports. Run after task-000017 has accepted pypdf 7.0.1 and its artifact evidence. Keep arabic-reshaper 3.0.1 and python-bidi 0.6.11 for attribution; later text-library increments preserve this renderer's guarantees. Pin changes, necessary renderer compatibility fixes, direct tests and documentation form this single outcome, not new report design or rendering infrastructure.

## Contracts and owners

- `schedules/pdf.py` owns presentation using ReportLab Paragraph/Table/LongTable/Flowable and bundled TTFont APIs; `schedules/views/reports.py` and existing PDF endpoints consume projections. Do not duplicate the layout engine or move domain calculations into rendering.
- `docs/design.md` accepted month-calendar PDF generation/last-edit distinction, Master Schedule projections, sections 25.1/25.2 stipend/date-range report content, totals, provenance, warning and authorization semantics. PDFs remain current read-only outputs, not retained revisions; printing must not initialize periods or edit source data.
- `schedules/fonts/README.md` and its OFL license/provenance preserve bundled Noto coverage independent of host fonts. Keep existing Arabic-run shaping through arabic-reshaper and `bidi.algorithm.get_display`; do not switch shaping backends, introduce network fonts/assets, or expand script support silently.
- Inspect ReportLab 5.1.0 source distribution `CHANGES.md` from PyPI. Intervening changes include default-font work, table/layout behavior, safe-evaluation fixes and tighter trusted-host handling (5.0 makes `trustedHosts=None` deny remote hosts; 5.0.1 checks redirect destinations). Preserve these restrictions; never enable remote fetching to make current exports work. Current fonts/assets are bundled/local. Python 3.14 satisfies the new minimum.

## Acceptance and verification

1. Pin/install ReportLab 5.1.0 in the shared virtualenv, keeping all other direct pins and recording resolved version/transitive requirements. Do not add optional shaping/Cairo/acceleration stacks unless a demonstrated accepted-contract requirement is escalated first.
2. Existing PDF suites (`test_precepting_calendar_pdf.py`, `test_master_schedule_workspace.py`, `test_schedule_reports.py`, `test_stipend_reports.py`) pass on real generated artifacts. Preserve readable text and metadata, Chinese names, expected font embedding, supported Arabic presentation, data/coordinate association, no clipping/overlap, page orientation, headers and multi-page continuation where the existing product supports them. Exact compressed bytes, object ordering or incidental font serialization are not requirements.
3. Inspect representative rendered PDF artifacts as well as extracted assertions for the affected renderer/font surfaces. If an available renderer is used for visual evidence, identify it and the fixture; do not claim visual correctness from extraction alone. Existing bounded geometry evidence is preferred over a new visual-baseline platform. Any unavailable evidence or actual layout regression must be reported, not hidden.
4. User text remains escaped before paragraph markup; bundled assets work without remote asset permission. Existing report/authentication/no-mutation assertions remain effective. Use proportional direct regression coverage for any changed compatibility boundary rather than broadening product behavior or weakening assertions.
5. `./scripts/quality-check`, `./scripts/check` and `./scripts/security-audit` pass with inspected output. Exact Candidate Check and independent Review must pass before coordinator acceptance.

## Resource and execution boundary

Use shared main Python and normal project runners, never install/change system fonts or services. Record exact worker-owned generated PDF/raster/scratch resources and remove them on success/failure while retaining verification evidence in the Attempt. No live data, production/staging access, resets, pushes, PR disposition, deployment, sudo/credential changes or unrelated cleanup. SQLite/PostgreSQL behavior and deployment topology remain unchanged. The coordinator owns GitHub checks and grouped isolated staging verification, not the worker.

## Execution strategy

Inspect release changes and the shared renderer's explicit font/style choices before installing only ReportLab. Run the existing artifact/geometry suite first, then inspect representative supported non-ASCII and multi-page output to distinguish rendering regressions from parser expectations. Keep the accepted pypdf oracle meaningful. A compatibility adaptation must retain its consumer's same visible contract, not merely avoid exceptions.
