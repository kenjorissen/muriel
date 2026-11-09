# Portable tag layout verification

Goal: [goal-000001](../goals/goal-000001-clean-ci-deployment-baseline.md)

## Objective

Correct the ordinary-table browser verification so its fit/wrap scenarios reflect actual available content width, independent of the operator or CI runner's system font metrics, while retaining protection for the implemented semantic tag presentation.

## Context and contracts

- `docs/design.md` section 23, Application shell and navigation, ordinary table renderer paragraphs: the shared renderer owns semantic table sections, scrolling, density and presentation; domains supply meaningful content. Preserve the semantic server-rendered baseline and responsive CSS without adding a UI framework or font dependency.
- `docs/design.md` section 8.5: compact Physician directory presentation and role/source context. This repair does not change those domain values or introduce new directory behavior.
- `README.md`, Development checks: use declared Playwright tooling and the existing `./scripts/check`; root `AGENTS.md` requires the shared main Python virtualenv and ordinary helper commands.
- Owning code/evidence: `schedules/tests/test_table_presentation_browser.py`, `schedules/tests/test_table_presentation.py:test_renderer_supports_escaped_ordered_tag_lists`, `schedules/table_presentation.py`, `schedules/templates/schedules/data_table.html` and `schedules/static/schedules/application.css` (`.data-table-tags`, `.data-table-tag`, table/cell padding). Existing tag behavior is semantic ordered lists, intact nonwrapping individual labels, wrapping between labels, and using available content width when labels fit. Keep the existing record/column associations, capacity meaning, scroll containment and semantic tone checks.
- GitHub run https://github.com/kenjorissen/muriel/actions/runs/42649585943 on `624bf6031cf919755c3dad20e43c4fadac047724` passed browser setup and security auditing but failed this test with `The tag cell does not use its available cell width.` The fixture is a 22rem table; its precondition compares total tag widths plus gap with `td.clientWidth`, which includes horizontal padding rather than only usable content width.
- Read-only local diagnosis reproduced the false premise using unchanged stylesheet and wider text metrics (Liberation Sans plus 0.07em letter spacing): client width 350px, usable content width about 330.8px, tag widths plus gap about 331.05px. Correct wrapping occurs, but the current check wrongly says they fit. This establishes the faulty precondition; it is not a claim to have measured GitHub's exact font selection.

## Acceptance

1. The browser test independently establishes one actual content-box-constrained case in which both complete tags plus gap fit with sensible headroom, and another where wrapping between tags is required. Measurements exclude borders/padding as appropriate to the chosen box. Do not derive an expected result from the current observed wrap outcome itself.
2. Fit and constrained cases preserve meaningful checks: fitting labels stay on one row, constrained labels wrap between tags, individual labels remain intact, and the existing table's semantic/scroll/record-value assertions remain protected. The fixtures permit legitimate differences in system font metrics and equivalent CSS presentation rather than assuming exactly 22rem proves fit on every machine.
3. Representative wider-text or altered-box metrics exercise the observed padding-boundary error without adding a global font package or pinning one platform's glyph widths. A representative broken tag-width/wrapping behavior must still be detected. Reuse the existing browser test seam; no broad font/viewport matrix or duplicate end-to-end suite.

## Verification and boundaries

Run the focused table browser/renderer tests with declared tooling, then `./scripts/quality-check` and `./scripts/check`. Retain attributable pre-fix and corrected evidence for the fixture flaw and evidence that the lasting regression protection remains effective. Do not simply remove the failing assertion, increase a tolerance until it passes, suppress errors or freeze a specific font. No production CSS/domain change is expected: surface a demonstrated product defect or unresolved contract before broadening scope. Browser resource cleanup and offline checks remain unchanged. No dependency upgrades, PR disposition, push or deployment authority comes from this Task.

Task 000002's declared browser tooling and Task 000001's parallel reporting are already accepted. The independent Work remaining alignment failure is Task 000005; fixing it is not required to establish this Task's table-specific outcome, and must not be hidden or claimed resolved here.
