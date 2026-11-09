# Responsive work summary verification

Goal: [goal-000001](../goals/goal-000001-clean-ci-deployment-baseline.md)

## Objective

Correct Work remaining browser verification to protect usable responsive header presentation without requiring a particular system font to fit the heading and Schedule Bot link on one row at phone width.

## Context and contracts

- `docs/design.md` section 7.2, Work remaining projections: the scoped summary follows period controls, preserves complete operational counts and required/advisory meanings, and has the accepted semantic tone. Presentation must not change projection policy or domain scope.
- `docs/design.md` section 23, Application shell and navigation: small shared server-rendered workspace/surface primitives, semantic HTML, responsive CSS and accessible interaction rather than frontend framework infrastructure. Section 32.1 and 32.5 own surface inset and layout-parent spacing. Keep the real-browser evidence for those presentation obligations.
- Owners: `schedules/templates/schedules/work_remaining_summary.html`, `schedules/static/schedules/application.css` (`.surface-header`, `.workspace-work-summary-action`, narrow media rules), `schedules/tests/test_work_remaining_spacing_browser.py`, `schedules/tests/test_work_remaining.py` (summary action response evidence), and `schedules/views/preceptors.py` (range action and selected-month destination).
- The current shared header explicitly uses flex-wrap. The current test nevertheless requires `sharesHeadingRow` and right alignment at both 1280px and 375px. GitHub run https://github.com/kenjorissen/muriel/actions/runs/42649585943 on `624bf6031cf919755c3dad20e43c4fadac047724` failed at the Precepting Calendar 375px assertion after browser provisioning succeeded.
- A read-only local fixture with the unchanged header classes and text, using Liberation Sans plus 0.07em letter spacing, reproduced orderly wrapping: heading bottom about 30.8px, action top about 34.8px, action bottom about 70.8px within header bottom about 80.6px. The old assertion rejects that contained nonoverlapping second row. This is reproducible evidence of the overly strict test premise, not proof of GitHub's exact font choice.

## Acceptance

1. At ordinary widths where the contents fit, browser evidence protects the existing heading/action relationship and useful alignment. At constrained widths, accept the shared header's orderly wrapping as well as a same-row fit; require readable distinct heading and action, containment in the header/viewport, no overlap or clipping, and a usable accessible link. Do not require a fixed row count at a particular viewport across all system fonts.
2. Preserve the original standard section-gap checks following period controls and the typed summary's rendering/overflow protection for both Precepting Calendar and List. Keep the real action associated with its Work remaining header and the existing server-owned selected-month destination; do not change action semantics or operational counts.
3. Exercise both fitting and constrained cases with a feasible narrow/wider-text fixture using existing browser seams. Broken overlap, overflow or missing controls must still fail; do not weaken the test into mere page-wide text existence, hide failures, or add arbitrary tolerance.

## Verification and boundaries

Run focused Work remaining browser/response evidence, then `./scripts/quality-check` and `./scripts/check` with the existing shared main virtualenv and declared browser tooling. Record pre-fix reproduction and corrected evidence, distinguishing fixture assumptions from production defects. No production CSS change or new responsive design is expected; if the implemented layout actually overflows or hides controls, report that consequential defect rather than assuming it is harmless wrapping. Preserve browser cleanup, no-JavaScript behavior, and existing test isolation. No new font package, UI framework, dependency upgrade, PR disposition, push, deployment or data mutation is authorized.

Task 000004 independently repairs tag-test content-width assumptions; it precedes this Task only for delivery order. This Task must preserve that accepted outcome and must not reinterpret table semantics. Earlier CI dependency/tooling repairs remain intact. A future exact-revision GitHub run is still required for the Goal; local passes alone are not CI success.
