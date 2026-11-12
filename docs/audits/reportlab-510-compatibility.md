# ReportLab 5.1.0 compatibility

## Dependency and renderer boundary

Upgraded only the direct ReportLab pin from 4.4.10 to 5.1.0. Verification used
shared-environment Python 3.14.4, pypdf 7.0.1, arabic-reshaper 3.0.1 and
python-bidi 0.6.11. ReportLab's required dependencies remain `pillow>=9.0.0`
(resolved 13.0.0) and `charset-normalizer` (resolved 3.5.2). No optional
acceleration, Cairo, bidi or HarfBuzz extras were installed; no other direct
pin changed. `pip check` reported no broken requirements.

Inspected `CHANGES.md` in the PyPI `reportlab-5.1.0.tar.gz` source distribution
(SHA-256 `6878200464527e87672d19e9f7cfc6bb890e3a6931e74ac6a1a8d1754aa55671`).
Relevant intervening changes include:

- 4.5.x table-bound diagnostics and configuration reset changes;
- 5.0's default refusal of remote hosts with `trustedHosts=None`;
- 5.0.1's redirect-destination checks and subsequent safe-evaluation fixes;
- 5.0.4's default-font work and 5.1's configuration/file-handling changes.

The changelog announces a Python 3.10 minimum, although the installed wheel's
`Requires-Python` still says `>=3.9,<4`; the project's Python 3.14 satisfies both.

`schedules/pdf.py` already explicitly selects bundled Noto fonts for its styles,
uses ReportLab's Paragraph/LongTable/Flowable layout, and escapes user text before
adding its own paragraph markup. Arabic runs still use arabic-reshaper followed
by `bidi.algorithm.get_display` and the bundled Noto Naskh Arabic font. No
renderer compatibility adaptation, layout redesign or shaping-backend change
was necessary. Font provenance and OFL licensing remain in
`schedules/fonts/README.md` and `LICENSE-OFL.txt`.

No ReportLab host/scheme permissions were relaxed. Inspection of the installed
URL opener confirmed that the host/scheme checks remain on the opener used for
redirects. A one-time diagnostic confirmed `trustedHosts` remained `None` and
an HTTPS asset request was refused before the underlying network opener was
called. All generated fixtures used bundled fonts, not network assets.

## Artifact evidence

The four existing suites passed unchanged (87 tests):
`test_precepting_calendar_pdf.py`, `test_master_schedule_workspace.py`,
`test_schedule_reports.py` and `test_stipend_reports.py`. Their real-artifact
assertions retain metadata, orientation, date/period/position association,
page/cell geometry, repeated headings, totals, warnings, authentication and
read-only source-state protection. The pypdf oracle was not changed.

`test_pdf_text.py` adds a shared-renderer artifact regression for embedded fonts
associated with Chinese and joined Arabic names and for literal user markup,
including an apparent remote image tag. It checks the actual font streams used
for those names rather than incidental PDF object order or compressed bytes.
A diagnostic replacement of Arabic reshaping with the identity function failed
at the intended joined-name assertion; the normal renderer passes.

Visual inspection used the already installed Chromium 158.0.8127.18 PDF viewer
(PDFium), driven by the project's Playwright installation. These were rendered
PDF screenshots, not HTML substitutes or extraction-only claims. Inspected:

| Fixture | Pages inspected | Observations |
| --- | --- | --- |
| Text-boundary calendar: February 3–7, 2025, with `李雷`, `محمد`, ampersand/bold-tag text and a literal image tag | 1, including a 200% name detail | Chinese glyphs and connected Arabic readable; markup remains literal within the table. |
| `MasterScheduleWorkspaceTests.test_pdf_places_distinct_assignments_in_their_date_period_and_position_cells` | 1 | Landscape clinic/supporting grids, rotated Morning/Afternoon labels, distinct assignments and capture time remain legible. |
| `ScheduleReportPdfTests.test_pdf_artifact_includes_the_precepting_record_and_findings_table` | 1 | Long physician name, separate numeric columns, provenance, planning notice and findings fit without visible overlap. |
| `StipendReportPdfTests.test_stipend_pdf_repeats_detail_and_totals_headings_across_real_page_breaks` | 1–3 | Detail and totals headers repeat; amounts remain in their columns; overall total is visible on the final page. |
| `PreceptingCalendarPdfTests.test_exceptional_content_repeats_headings_and_keeps_each_week_day_and_slot_together` | 1–2 | Week groups stay together; weekday headings repeat; adjacent-month shading and lane labels remain intact. |

No clipping or overlap was observed in these samples. This bounded inspection
complements, rather than replaces, the existing geometry assertions; it does
not establish support for additional scripts or arbitrary input lengths.
The initial viewer probe captured its unloaded background and then timed out
waiting for an `embed` in the outer document. The actual PDF viewer lives in an
extension frame. Final screenshots waited for that frame's PDF load progress
to reach 100%; the blank probe was not treated as rendering evidence.

## Local verification

- Focused existing PDF/report suites: 87 passed.
- New text-boundary test: passed; representative unjoined-Arabic fault detected.
- Four fixture tests rerun while capturing their PDFs: passed.
- `./scripts/quality-check`: passed, including no migration drift.
- `./scripts/check`: 28 JavaScript tests passed; 1,634 Django tests completed
  successfully with 8 existing skips. Database-override warnings came from
  existing reset tests; their printed synthetic timeout/reset messages were
  fixture output, not live-data operations or runner failures.
- `./scripts/security-audit`: no known vulnerabilities found.

Temporary PDFs, screenshots, source archive and capture scripts were disposable
verification resources, not application artifacts or a new visual-baseline
platform. Worker observations are diagnostic; exact Candidate Check and
independent Review remain required before acceptance.
