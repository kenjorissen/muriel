# arabic-reshaper 3.1.0

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective and boundary

Upgrade arabic-reshaper 3.0.1 to 3.1.0 after accepted task-000018, preserving Arabic names within the existing PDF exports. ReportLab 5.1.0, pypdf 7.0.1 and python-bidi 0.6.11 stay fixed for this increment. The successor bidi patch must retain the same readable, escaped and correctly associated text outcome. This is not adoption of a new language mode or shaping engine.

## Contracts and owners

`schedules/pdf.py::_text` uses its Arabic-run matcher, `arabic_reshaper.reshape`, `bidi.algorithm.get_display`, escaped Paragraph markup and the bundled Noto Naskh Arabic font. Keep that conventional library boundary instead of implementing joining/bidi algorithms. `requirements.txt` owns the package pin; `schedules/fonts/README.md` owns bundled font provenance/OFL licensing. `docs/design.md` accepted month-calendar PDF and Master Schedule read-only projections plus sections 25.1/25.2 report scopes, totals, provenance and warnings remain unchanged.

Read upstream release `https://github.com/mpcabd/python-arabic-reshaper/releases/tag/v3.1.0`: principal changes correct Kurdish-mode glyph forms and add Python 3.15 support. The current application uses the default Arabic reshaper, not `language: Kurdish`; do not add Kurdish-mode configuration, fonts or broad script-support promises as part of this update. Use Python 3.14 as before.

## Acceptance and verification

1. Declare/install arabic-reshaper 3.1.0 with shared main Python and retain all other direct pins. Record resolved package versions; no optional fonttools dependency or external font source is needed without demonstrated evidence.
2. Existing PDF-owning suites pass. Retain or supply narrowly scoped evidence of actual Arabic text passing through `_text` into a real generated PDF with its bundled Arabic font, alongside surrounding Latin/numeric text and escaped markup metacharacters. Preserve characters, expected joining/display order under the existing supported mode, surrounding text, record/cell association and read-only export behavior. Do not claim new full-document RTL/Hebrew/Kurdish layout support.
3. Expected shaping/display evidence must be independently justified by a known fixture or documented rule, not calculated by calling the production function under test or copying changed output. Inspect rendered output when needed to establish joining/order; extraction alone does not establish visual Arabic correctness. Reuse the preceding renderer Task's meaningful evidence where sufficient and report unavailable visual evidence honestly. Any added regression protects this enduring text behavior, not a particular package version or internal glyph serialization.
4. Normal `./scripts/quality-check`, `./scripts/check` and `./scripts/security-audit` pass with output inspected. Do not suppress errors, relax escaping, skip geometry checks or substitute tofu/ASCII to obtain success. Exact Candidate Check and independent Review are required before acceptance.

## Resource and authority boundary

Use the normal project test runner and shared interpreter. Record/remove only owned generated PDF/raster/scratch resources on success/failure, retaining findings in Attempt evidence. No edits to fonts, system services, existing databases, deployment topology, credentials or live mail. No pushes, PR disposition, deployment, production/staging access, resets, sudo or unrelated process cleanup. The coordinator owns GitHub and grouped non-reset staging verification.

## Execution strategy

Inspect existing PDF text evidence before adding anything. Compare the same supported Arabic fixture across the old and target reshaper as diagnostic evidence, independently checking its intended result; unchanged output alone does not prove correctness. Keep bidi fixed to attribute any difference, then run focused and full checks. Do not build a custom text-rendering test platform.
