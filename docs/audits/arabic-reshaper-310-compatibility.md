# arabic-reshaper 3.1.0 compatibility

## Dependency and rendering boundary

Only `requirements.txt`'s arabic-reshaper pin changed, from 3.0.1 to 3.1.0.
Installation used the main repository's shared `.venv/bin/python`, Python
3.14.4, with `pip install -r requirements.txt`; `pip check` passed.
Resolved direct packages:

| Package | Version |
| --- | --- |
| Django | 6.1.3 |
| gunicorn | 27.0.0 |
| whitenoise | 6.12.0 |
| psycopg / psycopg-binary | 3.3.8 |
| pypdf | 7.0.1 |
| reportlab | 5.1.0 |
| arabic-reshaper | 3.1.0 |
| python-bidi | 0.6.11 |
| ortools | 9.15.6755 |
| openpyxl | 3.1.5 |

ReportLab's Pillow and charset-normalizer dependencies remain 13.0.0 and 3.5.2.
Fonttools is not installed; no optional extras or external fonts were needed.
Bundled fonts and their provenance/OFL licensing are unchanged.

Read the [upstream v3.1.0 release](https://github.com/mpcabd/python-arabic-reshaper/releases/tag/v3.1.0)
through GitHub's release API. It corrects Kurdish forms for lam with small v,
reh with small v below, heh, isolated hah and isolated ye with small v; adds
Python 3.15 support; and fixes the reported `__version__`. The application uses
the default Arabic mode, not `language: Kurdish`, and remains on Python 3.14.
No changes to `schedules/pdf.py` were needed: its Arabic-run matcher, reshaper,
`bidi.algorithm.get_display`, escaped Paragraph markup and bundled Noto Naskh
Arabic font remain the owning boundary. This does not introduce full-document
RTL, Hebrew or Kurdish layout support.

## Text and artifact evidence

Extended the preceding renderer Task's `test_pdf_text.py` fixture to place
`Dr 12 محمد & <7>` in Tuesday, February 4, 2025's AM assignment, alongside the
existing Chinese name and literal bold/image-tag assignments on other days.
The actual generated PDF retains the Arabic font stream and mixed assignment
text; no remote image is loaded. Existing calendar, Master Schedule and report
suites retain date/period/cell association, geometry, provenance, totals,
warnings, authentication and read-only source-state coverage unchanged.

The independent expected joining rule for محمد is initial meem U+FEE3,
medial hah U+FEA4, medial meem U+FEE4, final dal U+FEAA. Left-to-right PDF
painting requires the display sequence U+FEAA U+FEE4 U+FEA4 U+FEE3. The new
`_text` boundary test checks that sequence alongside Latin letters, digits,
ampersand, angle brackets and quotes, parsing markup rather than freezing its
font-tag or escape serialization. The artifact test checks the mixed assignment
in layout extraction and retains the existing embedded-font checks. Expected
literals are not computed with the production reshaper or bidi function.

Both text tests passed with 3.0.1 and 3.1.0 while bidi stayed fixed at 0.6.11;
the independently expected display codepoints were identical. Diagnostic
identity replacements of reshaping and bidi display each failed both tests at
the intended assertions. An initial mixed-string assertion using plain pypdf
extraction failed even on 3.0.1: that extraction applies bidi again, rearranging
neutral punctuation after the Arabic run. Layout extraction preserves painted
order, so the artifact assertion uses that mode, without normalizing away
punctuation or weakening escaping.

Rendered both versions' PDFs in the installed Chromium 158.0.8127.18 PDFium
viewer through the project's Playwright installation. Inspected the target
page and both versions' enlarged Tuesday-cell screenshots: connected محمد,
Latin/numeric context and literal `& <7>` remain in the February 4 AM cell;
no visible difference or clipping was observed. This is rendered-PDF evidence,
not an extraction-only claim. It complements the broader visual inspection
recorded in `reportlab-510-compatibility.md`; it is not a broad script/layout
certification. Initial viewer probes timed out because they targeted an iframe
locator or nonexistent load-progress properties. Final captures used the
actual extension frame and waited for its `loadState_` to report `success`;
failed probes were not treated as visual evidence.

## Local verification

- Focused PDF-owning suites: 89 tests passed.
- `./scripts/quality-check`: passed, including no migration drift.
- `./scripts/check`: 28 JavaScript tests passed; 1,635 Django tests completed
  successfully with 8 existing skips. Database-override warnings and printed
  synthetic reset/timeout messages came from existing test fixtures, not live
  data operations or runner failures.
- `./scripts/security-audit`: no known vulnerabilities found.

Owned temporary PDFs, screenshots and capture scripts were removed. No database,
font, renderer or system-service changes were made. These are local diagnostic
findings; exact Candidate Check and independent Review remain required before
acceptance.
