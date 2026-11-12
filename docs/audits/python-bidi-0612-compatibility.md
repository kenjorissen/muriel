# python-bidi 0.6.12 compatibility

## Dependency and rendering boundary

Upgraded only the python-bidi pin from 0.6.11 to 0.6.12. Installation used
`pip install -r requirements.txt` with the main repository's shared
`.venv/bin/python`, standard (GIL-enabled) Python 3.14.4. Pip installed the
CPython 3.14 manylinux wheel without a native build; `pip check` passed.
Installed direct packages:

| Package | Version |
| --- | --- |
| Django | 6.1.3 |
| gunicorn | 27.0.0 |
| whitenoise | 6.12.0 |
| psycopg / psycopg-binary | 3.3.8 |
| pypdf | 7.0.1 |
| reportlab | 5.1.0 |
| arabic-reshaper | 3.1.0 |
| python-bidi | 0.6.12 |
| ortools | 9.15.6755 |
| openpyxl | 3.1.5 |

Pillow and charset-normalizer remain 13.0.0 and 3.5.2 respectively.

Read the [v0.6.12 release](https://github.com/MeirKriheli/python-bidi/releases/tag/v0.6.12)
through GitHub's release API and the matching
[tagged changelog](https://github.com/MeirKriheli/python-bidi/blob/v0.6.12/CHANGELOG.rst).
They describe standard/free-threaded Python 3.15 wheels, PyO3 0.30.0, and
security patches to development dependencies, dropping Python 3.9 support
because those patched dependencies require Python 3.10+. These notes do not
establish a runtime application vulnerability. This upgrade neither adopts
Python 3.15/free-threading nor changes the text algorithm.

`schedules/pdf.py::_text` continues to use `bidi.algorithm.get_display`, the
Python implementation, rather than the Rust-backed top-level entry point.
Arabic-run matching, reshaping, escaping and ReportLab Paragraph rendering
are unchanged, as are bundled-font provenance and OFL licensing in
`schedules/fonts/README.md`. No new script or layout capability is claimed.

## Reused evidence and local verification

Reused the existing fixtures and independent expectations documented in
[ReportLab compatibility](reportlab-510-compatibility.md) and
[arabic-reshaper compatibility](arabic-reshaper-310-compatibility.md), without
changing tests or adding a version assertion. Those records retain the prior
rendered-PDF inspections and representative shaping/display fault checks;
no new raster inspection was performed for this patch.

The five focused suites (`test_pdf_text`, `test_precepting_calendar_pdf`,
`test_master_schedule_workspace`, `test_schedule_reports`, and
`test_stipend_reports`) passed all 89 tests on the new version. They retain
Arabic joining/display order, surrounding Latin/numeric text, literal escaped
metacharacters, actual embedded font streams, content/cell association,
geometry, provenance and read-only source-state protection.

Required commands completed successfully with their output inspected:

- `./scripts/quality-check`: lint, formatting, system checks and migration drift
  passed.
- `./scripts/check`: 28 JavaScript tests passed; 1,635 Django tests completed
  successfully with 8 existing skips, including the existing browser/PDF suites.
  Database-override warnings and synthetic reset/timeout messages were existing
  test-fixture output, not live data operations or test failures.
- `./scripts/security-audit`: no known vulnerabilities found.

No standalone PDF, raster or scratch files were created. No source, test,
font, database or transport behavior was changed. These are local diagnostic
results; exact Candidate Check and independent Review remain required.
