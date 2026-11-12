# python-bidi 0.6.12

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective and predecessors

Upgrade python-bidi 0.6.11 to 0.6.12 after accepted task-000019. Preserve the established PDF Arabic-run display behavior with ReportLab 5.1.0, pypdf 7.0.1 and arabic-reshaper 3.1.0 unchanged. This independently attributable patch delivers the refreshed package without switching algorithms or adding script/layout capabilities.

## Owners and contracts

`requirements.txt` declares the dependency. `schedules/pdf.py::_text` explicitly imports `get_display` from `bidi.algorithm` (the retained Python implementation), not the Rust-backed top-level `bidi.get_display`. Preserve that choice and the ordinary shaping/escaping/bundled-font boundary. `schedules/fonts/README.md` preserves local font provenance/OFL. `docs/design.md` accepted month-calendar PDF, Master Schedule and sections 25.1/25.2 reporting require read-only, correctly scoped artifacts with the same content, provenance and layout.

Read `https://github.com/MeirKriheli/python-bidi/releases/tag/v0.6.12` and the matching changelog. Changes add Python 3.15 wheels, update PyO3 and patch development dependencies; they do not justify claiming a demonstrated runtime application vulnerability, adopting free-threaded Python, or swapping to the Rust algorithm. Python remains 3.14.

## Acceptance and verification

1. Pin/install python-bidi 0.6.12 with shared main Python. Preserve other direct pins and record installed versions. No native build/runtime redesign or additional text framework.
2. Existing PDF artifact suites and the accepted text-path evidence from tasks 000018/000019 pass unchanged in meaning: Arabic joining/display order in the current supported runs, surrounding Latin/numeric text, escaped metacharacters, actual bundled-font rendering, content/cell association and no source mutation. Reuse that evidence instead of duplicating a fixture collection. Investigate a genuine difference against independent expected behavior rather than blessing new output or switching implementations to make it disappear.
3. `./scripts/quality-check`, `./scripts/check`, and `./scripts/security-audit` pass with actual output inspected. No broad warning filters, omitted browser/PDF tests, weakened Unicode/geometry assertions or direct package-version regression test. Exact Candidate Check and independent Review are required.

## Resource and authority boundary

Use existing runners and shared interpreter. Worker-owned generated PDFs, raster evidence and scratch are recorded and removed on success/failure; preserve useful results in Attempt evidence. No system font/package changes, live application data, production/staging access, deployment, resets, push/PR disposition, credential/sudo changes or unrelated cleanup. Database and transport behavior remain unchanged. The coordinator separately owns GitHub and grouped isolated staging verification.
