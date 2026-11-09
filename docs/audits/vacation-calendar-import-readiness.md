# Vacation Calendar Import Readiness Audit

**Audited revision (private pre-export provenance):**
`dfb92313406b2eef698bd156133eabdbeceac3f0` (`dfb9231`,
`Add vacation calendar import workflow`, 2026-08-28). This identifier is
informative, not a required public Git object.

**Historical record:** Findings, source line numbers, verification results, and
the disposition below describe that revision and the inspected workbook, not a
new public revision. Recommendations remain dated audit recommendations, not
current product requirements or a new implementation plan. Current requirements
belong to [the canonical design](../design.md), and current operator guidance to
[the repository README](../../README.md) and its linked runbooks.

**Disposition: READY WITH ACCEPTED LIMITATIONS.** The inspected workbook may be
used to establish or replace Vacation data only while it remains the
authoritative source, retains the inspected no-formula/no-duplicate Faculty
shape, and the operator follows the bounded preconditions below. Formula marker
cells and duplicate normalized Faculty rows remain unsupported source shapes;
the importer does not reject either. ZIP-valid malformed workbooks can also
escape the expected error handling before any plan or mutation. These are
accepted limitations for the present authoritative workbook, not evidence that
arbitrary future workbooks are safe.

This audit examined only the revision above and generated temporary workbooks.
The supplied 2026–2027 Vacation Calendar workbook was inspected read-only in
temporary storage (local path omitted); no names, cell values, or source data
were retained in the repository. No production or application data was changed.

## Scope and ownership map

| Concern | Current owner and evidence |
| --- | --- |
| Shared contract | `schedules/services/vacation_calendar_import.py`: `load_vacation_calendar`, `build_vacation_calendar_import_plan`, `describe_vacation_calendar_import_plan`, and `apply_vacation_calendar_import` |
| CLI adapter | `schedules/management/commands/import_vacation_calendar.py`: `Command`; positional `xlsx_path`, required `--scheduler`, optional `--replace` and `--apply` |
| Browser adapter | `schedules/forms/vacations.py`: `VacationCalendarImportForm`; `schedules/views/vacation_imports.py`: `vacation_calendar_import`; URL `preceptor_project/urls.py:232-236` |
| Browser presentation | `schedules/templates/schedules/vacation_calendar_import.html`; responsive rules at `schedules/static/schedules/application.css:586-599, 655-681` |
| Authority | Browser view calls `require_scheduling_administrator` at `views/vacation_imports.py:90-93`; service repeats it at `services/vacation_calendar_import.py:459-464`; CLI checks `can_manage_scheduling_administration` in `_scheduler` |
| Vacation mutation and history | `schedules/services/vacations.py`: `create_vacation`, `remove_vacation`, `_record_change`; `schedules/models/vacations.py`: `Vacation`, `VacationChange` |
| Serialization primitive | `schedules/services/_mutation.py`: `_serialized_scheduling_mutation`, `scheduling_mutation`; lock row is `SchedulingMutationAuthority` in `schedules/models/publication.py:8-11` |
| Tests delivered with revision | `schedules/tests/test_import_vacation_calendar.py`; `schedules/tests/test_vacation_calendar_import_ui.py` |
| Dependency | `requirements.txt:10`, `openpyxl==3.1.5` |
| User and operational documentation | `README.md:186-221`; deployment/backup context is `docs/deployment.md` and `docs/backup-recovery.md` |

The service is appropriately shared by both adapters: neither adapter parses
cells or writes `Vacation` rows directly. The plan describes source data and
current database state, and application delegates creates/removals to the
ordinary Vacation service, which records the authenticated actor and value/note
snapshots.

## Workbook interpretation

### Observed contract

`load_vacation_calendar` first checks the ZIP archive, then loads the workbook
in read-only, `data_only=True`, `keep_links=False` mode. `_month_metadata`
requires a sheet named `` `Mos.Yr`` and skips rows whose month cell is blank
and whose year cell is `None`; the remaining collected entries must contain
exactly twelve consecutive calendar months. `_calendar_sheet` selects exactly
one title matching each metadata month.
`_date_header_row` requires one of the first twenty rows to have literal day
numbers in columns C onward. `_faculty_header_row` finds the first `FACULTY:`
in column B among the first one hundred rows. `_faculty_rows` consumes up to
sixty nonblank column-B rows after that heading; it does not inspect the
Residents section.

For every consumed row, cells C through the calendar month end are cleaned and
uppercased. `V` becomes a whole-day source absence; adjacent dates for a
normalized identity become one inclusive range through `_contiguous_ranges`.
`E` and `I` are collected as reported-but-not-imported markers. Any other
nonblank value blocks the complete load. `_source_display_name` changes
`Last, First` to `First Last`; `_name_key` case-folds and converts punctuation
to spaces. The same key is used for local `Physician.display_name` matching.

The resulting window is inclusive: first metadata month day 1 through last
metadata month last day (`services/vacation_calendar_import.py:285-294`). It
does enforce twelve consecutive months, but it does **not** require the
documented July-to-June sequence; January-to-December is accepted.

### Shape assumptions and failure modes

| Source assumption | Failure if upstream changes | Current result |
| --- | --- | --- |
| A sheet named exactly `` `Mos.Yr`` holds two-column month/year metadata | Renamed/moved metadata is a false blocker | Explicit blocker |
| After blank metadata rows are skipped, exactly twelve collected month/year entries are chronological and in intended order | Missing/reordered or nonblank malformed metadata is a false blocker; any consecutive non-July window is a false success against the stated legacy contract | Explicit blocker except blank rows and non-July start |
| Calendar sheet title is one accepted month alias and unique | Renamed/duplicate month sheet is a false blocker | Explicit blocker |
| Literal 1..28/29/30/31 headers occur in C onward within first 20 rows | Header row moved or formula/unusual day values becomes a false blocker | Explicit blocker |
| First `FACULTY:` in column B within first 100 rows is authoritative | A moved header is a false blocker; an earlier unrelated heading can select wrong rows | Explicit blocker or wrong interpretation |
| Faculty names are contiguous, nonblank, and no more than 60 rows after the heading | A blank separator ends the section early; an inserted label/nonblank section is consumed as Faculty; row 61+ is ignored | Silent false success, false blocker, or wrong Vacation |
| Each Faculty identity appears only once per sheet | Duplicate rows are merged by name key and their `V` cells unioned | Unsupported source shape; silent wrong Vacation if introduced (Finding 2) |
| `V`, `E`, and `I` cells are literal values | Formula cells without a cached result become blank under `data_only=True`; no formula marker is rejected | Unsupported source shape; silent data loss if introduced (Finding 1) |
| Blank marker cells really mean no marker | A cleared formula cache, accidental blank, or omitted trailing row is indistinguishable from no Vacation | Silent false success |
| Name spelling/punctuation stays stable | Formatting changes can cause unmatched blocker or merge distinct punctuation-normalized identities; local duplicates block rather than choose | Explicit blocker or wrong identity merge |
| Metadata year is an integral numeric value | A decimal year is truncated with `int(year_value)` | Wrong calendar year is possible |

Residents are not scanned only because scanning starts after `FACULTY:`; this
is correct for the supported layout, not a general Resident classifier.
Unmatched Faculty with a `V` blocks. Unmatched Faculty without `V` is a warning.
More than one local normalized identity blocks. `E`/`I` counts are reported per
identity but deliberately not transformed into Academic Events or Interviews.

## Replacement safety matrix

| Case | Evidence and current behavior | Assessment |
| --- | --- | --- |
| Additive import | New exact whole-day ranges enter `plan.create`; no removal is planned | Safe when the plan is current |
| Exact local match | First exact whole-day local record enters `reuse`; apply performs no write | Idempotent when the state remains current |
| Duplicate exact local records | Additive mode blocks; replacement retains one and removes the duplicate records | Deliberate, tested behavior |
| Partial/enclosing overlap | Any nonexact overlapping range is a difference; additive mode blocks, replacement removes it then creates the source range | Bounded when plan is current; partial/period-specific source semantics are deliberately not merged |
| Range crossing source window | `existing` uses inclusive overlap, and a record extending before/after the window blocks replacement. Sanitized 2026-08-28 staging operator evidence confirms a start-boundary record was refused before its separate removal and a later import | Guard works for the read snapshot; deleting the blocker is an operator decision, not an importer remedy |
| Unmatched Faculty with `V` | Blocker; no plan application | Safe and tested |
| Faculty absent from workbook | Not in `matched`, so their local Vacation rows are not selected | Safe for the read snapshot |
| Matched Faculty with no `V` | In replacement, every in-window existing Vacation for that person is stale and is removed | Intended synchronization behavior, but especially dependent on accurate source rows |
| All-or-nothing ordinary failure | `apply_vacation_calendar_import` has an outer `transaction.atomic`; create/removal exceptions propagate | Database rollback protects an exception, not stale-plan semantics |
| Retained history | `create_vacation`/`remove_vacation` create `VacationChange` records with actor and value/note snapshots | Correct owner and tested for creates; removal history is source-supported |
| Re-import unchanged workbook | Exact ranges reuse, so no new Vacation or change history is created | Tested for additive re-import |
| Preview then browser apply | The uploaded file and plan live only for one response; a new Apply POST reparses a newly selected upload | No durable preview identity or confirmation that the reviewed plan is applied |
| Concurrent import/manual edit | Selection occurs before any serialization in `build_vacation_calendar_import_plan`; `apply` does not re-read/revalidate its plan under the scheduling lock | Accepted operating risk: replacement is used only while the workbook is authoritative, imports are at least days and usually weeks apart, and intervening Muriel Vacation edits are disposable. A concurrent edit can still be overwritten or cause failure (Finding 4 hardening). |
| Scope | On its initial read, `existing` is restricted to overlap with the inclusive workbook window and to the names in `matched` (`services/vacation_calendar_import.py:337-341`) | Confirmed for the documented matched-Faculty/window plan scope. A selected row can be manually moved outside the window before apply and still be removed by primary key; that concurrent-edit outcome is accepted under the stated authority contract. |

The scope query otherwise supplies a useful narrow boundary: it cannot plan a
Faculty member absent from source matching, and it cannot plan a record that
did not overlap the source window at plan time. The direct staging evidence
below confirms a cross-window row becomes a blocker rather than an operation.
It does not validate an effective Faculty role, appropriately because Vacation
records are independent source facts under `docs/design.md:459-463`; `Faculty`
here is a workbook section, not a role eligibility rule.

## Adapter and input-security audit

### Shared behavior and authority

The CLI previews unless `--apply` is passed and requires `--replace` as a
second explicit flag before stale in-window matches may be removed. The browser
has separate `Preview workbook` and `Apply import` submit values plus a
replacement checkbox. Preview constructs a plan but calls no mutation. Apply
refuses a plan with blockers before calling the service. Both adapters display
warnings/blockers and contain expected custom parser, validation, and authority
errors as `CommandError` or form errors rather than a traceback. This
containment does not cover every exception raised by `openpyxl`; the ZIP-valid
malformed case below reaches neither adapter's `VacationCalendarImportError`
handler.

The browser route has `@login_required`, the administrator check before form
processing, Django's CSRF middleware, and `{% csrf_token %}` in the multipart
form. The CLI resolves exactly one active Scheduling Administrator before
planning. The service repeats administrator authorization, which prevents a
different direct caller from treating the adapters as the authority boundary.

The browser does not retain the upload after the response in application state;
the form handles the uploaded object only while parsing its POST. This is good
for lifetime and privacy, but also means Apply cannot refer to the previewed
upload or plan.

### File cases and limits

| Input case | Evidence | Result/limitation |
| --- | --- | --- |
| Browser filename and nominal size | `VacationCalendarImportForm.clean_workbook` accepts `.xlsx` by name and rejects above 10 MiB | Content type is advisory; ZIP validation supplies actual format checking |
| Production request size | Public Nginx origins use `client_max_body_size 11m`, while the form rejects workbooks above 10 MiB | Multipart framing fits inside the edge limit, leaving the documented workbook boundary to application validation |
| Archive entry count | `_validate_workbook_archive` rejects more than 2,048 entries | Generated 2,049-entry archive was rejected before parsing |
| Expanded archive size | Same function sums ZIP `file_size` and rejects above 64 MiB | Useful compressed-bomb limit; no test covers the byte threshold or forged/overlapping ZIP-entry accounting |
| Non-ZIP workbook | `_validate_workbook_archive` catches `BadZipFile` and reports `VacationCalendarImportError` | UI test exercises this invalid upload; no data changes |
| ZIP-valid workbook missing `[Content_Types].xml` | A generated archive containing only `xl/workbook.xml` passed ZIP validation then `openpyxl.load_workbook` raised `KeyError`; `load_vacation_calendar` catches `BadZipFile`, `OSError`, `InvalidFileException`, and `ValueError`, not `KeyError` (`services/vacation_calendar_import.py:228-234`) | Browser request is a 500 and CLI traces back before planning or mutation (Finding 3) |
| Encrypted workbook | An encrypted Office container is not a normal readable XLSX ZIP and should reach the generic invalid/unreadable error | Unsupported, not separately identified or tested |
| Macro-bearing workbook | Browser extension check rejects `.xlsm`; `load_workbook` does not request `keep_vba` and the importer never executes or saves workbook code | No macro execution path was found, but macro-bearing `.xlsx` content is not positively detected/rejected or covered by a test |
| External links | `keep_links=False` requests that openpyxl not preserve external links | Link values/formula caches still fall under the formula limitation; no adversarial link fixture covers parser/resource behavior |

## Browser-flow and accessibility review

An isolated live Django server with only generated data was opened in local
Chrome on 2026-08-28 while signed in as a generated Scheduling Administrator.
At a 1,440px viewport, the import GET rendered without horizontal overflow and
the synthetic preview rendered `Preview ready`, its July 1, 2026 through June
30, 2027 window, `1 create, 0 reuse, 0 remove`, and its E/I counts. At a 390px
viewport, the summary and actions each rendered as one column; after opening
the operations disclosure, its 331px scroll frame contained the 673px table
while the 375px document body remained within its viewport. This is direct
rendered responsive evidence, not a CSS-only inference.

With browser script execution disabled before navigation, the server-rendered
GET still exposed the labeled file control, replacement checkbox, both submit
controls, instructions, skip link, and ordinary navigation. The form has normal
multipart POST controls and server-generated response states; this page has no
page-specific client script and its deferred workspace period-picker is not
used because this template suppresses period controls. The no-script review
therefore establishes that uploading and choosing preview/apply do not require
an application JavaScript state machine; it does not substitute for a browser
test of every native upload implementation.

Keyboard was exercised in the live preview at 390px: from the file control,
Tab reached the labeled replacement checkbox, then `Preview workbook`, then
`Apply import`; the focused file input had the delivered 3px focus outline.
Focusing the operation `summary` and pressing Space opened it. The page-wide
skip link and the global `:focus-visible` rule are present
(`base.html:14,94`; `application.css:40-43`). These are correctness and
accessibility observations, not visual-preference judgments.

The page clearly states the supported source, whole-day conversion, resident
exclusion, ignored markers, preview non-mutation, no-retention claim, and
blocker outcome. It shows the source filename/window/counts and an exact
operation table, but that table is collapsed initially. Repeated unchanged
Apply submissions are idempotent; a browser refresh after POST follows normal
resubmission behavior rather than redirecting to a durable result.

Accessibility and safety observations:

* The content begins at `h2` (`Upload workbook`) with no page `h1`; local
  navigation does not name the current page as a heading. This is a low-severity
  heading-structure finding, not a visual preference.
* Replacement is labeled and explained, removal rows are visually tinted, and
  blockers use alert semantics. However, the destructive operation list remains
  inside a closed disclosure and the submit button remains the generic `Apply
  import`; a replacement-specific confirmation/review state would make the
  destructive action less error-prone. This is secondary to the correctness
  findings above.
* Field and non-field errors are server-rendered and visible; both plan
  blockers and expected failed Apply cases use `role="alert"`. There is no
  client-only error path, but the missing-manifest malformed workbook bypasses
  this form feedback and produces the Finding 3 server error.

## Documentation and operations reconciliation

`requirements.txt` pins openpyxl and `README.md:49-80` installs runtime
dependencies and documents disposable-worktree preview serving. `README.md`
also accurately describes the CLI's preview/apply/replace flag contract and
the matched-Faculty/window scope at lines 186-221. `import_vacation_calendar
--help` names the required scheduler and the separate `--apply`/`--replace`
intent; the browser copy expresses the same authority and intent.

The canonical design is inconsistent with the delivered feature:
`docs/design.md:39-42` says facts are currently maintained through manual entry
and imports remain future work, while `docs/design.md:1834-1855` describes
external-source import only as future normalization. The revision provides a
real production-affecting import and must not be represented as still deferred.

The user-facing documentation does not supply a safe operator sequence for a
real replacement import. `docs/backup-recovery.md:13-48` does define a verified
local plus off-host logical backup, and lines 50-68 define a scratch restore
drill that checks checksum, metadata, restore, and readiness before dropping
the drill database. Its production restore procedure (lines 70-81) stops
Muriel, requires both `--confirm` and `--allow-production`, restarts it, and
runs readiness checks. `docs/production.md:88-116` gives similarly guarded
backup/reset sequencing, but that reset helper replaces the whole scheduling
domain and is neither the importer nor an appropriate precondition for this
narrow import.

Those mechanisms do not make an import safe by themselves. The import has no
production operation/runbook that tells an operator to first confirm a recent
verified local-and-off-host backup, perform a scratch restore rehearsal,
preserve the exact sanitized preview output and workbook identity, stop on any
warning/blocker or unexpected create/reuse/remove count, apply only the just
reviewed source, and choose between targeted repair and the documented whole
database restore after an error. Recovery from the documented daily logical
dump can lose changes after that dump (`backup-recovery.md:95-98`), so it is not
a harmless undo facility for repeated replacement. This gap remains material
even though the repository's generic backup and recovery commands are sound.

Sanitized operator evidence from 2026-08-28 reports that the live workbook
imported successfully in staging only after existing Vacation records crossing
the workbook start boundary were removed. The importer explicitly refused that
cross-boundary case before the removal, consistent with
`build_vacation_calendar_import_plan` at lines 377-395. This is useful staging
evidence for the documented boundary guard. Combined with the read-only
structural inspection below, it supports readiness only under these bounded
operator preconditions:

* Use the inspected authoritative workbook, not an arbitrary spreadsheet. If a
  replacement workbook or layout is issued, repeat the structural inspection
  before use; its Faculty marker cells must contain no formulas and no sheet may
  repeat a normalized Faculty identity.
* Preview the exact upload immediately before Apply, stop on every blocker or
  unexpected create/reuse/remove count, and use replacement only with the
  explicit understanding that an in-window matched-Faculty Vacation absent from
  the source is removed. A cross-window blocker remains a stop condition, not
  permission for automatic removal.
* Before a production replacement, follow the existing verified local and
  off-host backup plus scratch-restore procedure, retain the exact workbook
  identity and sanitized preview output, and use targeted repair or the
  documented restore procedure after an error.
* Treat the workbook as authoritative while replacement is in progress.
  Imports occur at least days and usually weeks apart; intervening Muriel
  Vacation edits are disposable. Failure or overwrite caused by a simultaneous
  manual edit during Apply is accepted, rather than a reason to infer that the
  importer preserves concurrent edits.

## Findings

| ID | Severity, confidence, owner | Consequence and evidence |
| --- | --- | --- |
| 1 | **High, high confidence; accepted limitation for the inspected workbook.** Owner: vacation-calendar importer | Formula cells in Faculty marker columns are silently read as blank. `load_workbook(..., data_only=True)` at `services/vacation_calendar_import.py:230` exposes no formula text when a formula lacks a cached value; `_clean_text` then makes it empty at lines 88-90 and the marker loop skips it at 261-265. A generated valid workbook with `=CONCAT("V")` in `JUL!C6` produced no July 1 Vacation. Replacement can consequently delete the corresponding retained range as stale. The read-only inspection found zero formula cells in the supplied workbook's parser-selected Faculty marker cells. Reject formulas in all inspected day cells (and define whether cached formula values are ever supported) before accepting a new source shape. |
| 2 | **High, high confidence; accepted limitation for the inspected workbook.** Owner: vacation-calendar importer | Duplicate Faculty rows are silently merged. `faculty_by_key.setdefault` accepts a repeated matching display name (lines 250-260), then all its markers accumulate by key (266-280). A generated workbook with the same `Probe, Formula` on rows 6 and 7 produced one Faculty and one July 2 Vacation, while the row-6 formula marker was silently dropped. An upstream duplicate, copied row, or split layout can manufacture a union of two schedules. The read-only inspection found no normalized duplicate Faculty rows within any supplied-workbook sheet. Reject duplicate normalized Faculty rows within each calendar sheet and test the supported cross-month identity behavior separately before accepting a new source shape. |
| 3 | **Medium, high confidence.** Owner: vacation-calendar importer and adapters | A ZIP-valid XLSX missing `[Content_Types].xml` is not contained as `VacationCalendarImportError`. A generated archive with only `xl/workbook.xml` passed `_validate_workbook_archive`, then `openpyxl.load_workbook` raised unhandled `KeyError` at `services/vacation_calendar_import.py:230`; the narrow catch at lines 231-234 omits it. The browser's `except VacationCalendarImportError` at `views/vacation_imports.py:108-111` therefore returns 500, and the CLI's same narrow handler at `import_vacation_calendar.py:49-52` traces back. It occurs before planning or mutation, so the consequence is failed/error-leaking import rather than Vacation loss. Proportionate follow-up: normalize this known `openpyxl` malformed-container error to the existing compact import error in the shared loader and add one browser/CLI regression probe. |
| 4 | **Low, high confidence; nonblocking hardening under the accepted authority contract.** Owner: vacation-calendar service and adapters | Plan construction reads `Vacation` rows outside the scheduling serialization boundary (299-411). Application has only `transaction.atomic` (469), then invokes separately serialized create/remove operations. It neither locks/reloads the scoped snapshot nor fingerprints it before applying. A simultaneous manual correction can be overwritten, or an exact reused Vacation deleted after planning can remain absent. The browser also cannot apply its prior preview without re-uploading and rebuilding it. This is not a readiness blocker: while the workbook is authoritative, intervening Vacation edits are disposable, imports are infrequent, and simultaneous-edit failure or overwrite is accepted. Reassess a serialized/fingerprinted plan only if that authority or operating cadence changes. |
| 5 | **Medium, high confidence.** Owner: product/design documentation | `docs/design.md:41-42` says spreadsheet imports are future work, conflicting with this production-facing importer and README. This makes authority and operational expectations ambiguous. Accept and record the bounded legacy Vacation import contract in canonical design, including its source assumptions and limits. |
| 6 | **Resolved.** Owner: deployment and import form | Public Nginx origins now allow 11 MiB request bodies, including multipart framing, while application forms retain the 10 MiB workbook boundary and compact validation feedback. |
| 7 | **Medium, medium confidence.** Owner: import operations documentation | The README gives commands but lacks a production replacement runbook: backup verification, isolated rehearsal, output retention, stop conditions, and recovery are not joined to this operation. Generic daily backup documentation is insufficient assurance before a repeated destructive synchronization. Add a bounded import procedure that references, rather than duplicates, the existing backup/restore runbook. |
| 8 | **Low, high confidence.** Owner: browser presentation | `vacation_calendar_import.html:7` starts content with an `h2`, with no `h1` naming Import Vacation Calendar. Correct heading structure for keyboard/screen-reader navigation. |
| 9 | **Low, medium confidence.** Owner: importer and documentation | Encrypted, macro-bearing, externally linked, and archive-accounting edge cases are only generally rejected/disabled or undocumented. The implementation does not claim a sandbox, and current tests cover only malformed ZIP content and ordinary happy paths. State unsupported workbook cases explicitly and add bounded hostile-file regression evidence after the malformed-container fix. |

## Minimal follow-up slices

1. **Fail closed on source shape.** Reject formulas, duplicate Faculty rows,
   non-integral years, unsupported window starts, and ambiguous section
   boundaries; add synthetic parser tests for each. This independently removes
   silent wrong-Vacation paths.
2. **Contain malformed containers.** Normalize the known missing-manifest
   `openpyxl` error through the shared importer and test compact browser and CLI
   feedback. This is bounded error handling and does not change import scope.
3. **Align operator contract.** Reconcile canonical design, README/UI wording,
   upload limits, unsupported-file behavior, and a backup/rehearsal/recovery
   runbook. This can be reviewed independently of parser correctness.
4. **Finish browser semantics.** Add the missing page heading and make
   replacement review/confirmation unambiguous; exercise CSRF, no-script,
   narrow, and wide browser flows after the plan semantics are stable.

## Verification record and gaps

The focused test command ran from the disposable worktree with the shared
main-worktree Python. This is a sanitized equivalent, not the verbatim recorded
command: the personal interpreter path is replaced by an explicit placeholder.
For a new run, start at the source worktree root and replace
`/path/to/main-repository` with the absolute main checkout root containing the
shared `.venv`, not the disposable worktree root. The named test modules still
exist, but their current tests and results are not the historical evidence below.

```sh
MAIN_REPOSITORY=/path/to/main-repository
DJANGO_SETTINGS_MODULE=preceptor_project.test_settings \
  "$MAIN_REPOSITORY/.venv/bin/python" manage.py test \
  schedules.tests.test_import_vacation_calendar \
  schedules.tests.test_vacation_calendar_import_ui
```

The focused importer and UI suite covers ordinary preview, apply, replacement
of a stale in-window record, idempotent re-apply, marker reporting, non-ZIP
archive rejection, route visibility/authority, and basic browser rendering. It
does not cover formulas, duplicate Faculty rows, ZIP-valid malformed manifests,
archive byte size, encryption, macros, external links, window-start
restrictions, cross-boundary stale mutation, CSRF enforcement for this route, or
concurrent edits. Historical result: 8 tests passed in 6.516 seconds.

Generated temporary probes, deleted on exit, established that a valid workbook
with `=CONCAT("V")` plus a duplicate Faculty row yielded
`faculty=1 vacations=[(2026-07-02, 2026-07-02)]`; the formula-derived July 1
marker was absent and the duplicate was accepted. A 2,049-entry synthetic ZIP
was rejected with `contains too many archive entries.` A separate ZIP-valid
archive with only `xl/workbook.xml` raised `KeyError: "There is no item named
'[Content_Types].xml' in the archive"` from `openpyxl.load_workbook`; the
source catch and both adapter catches exclude that type. The supplied workbook
was opened read-only with `data_only=False` and the importer's own month,
Faculty-section, and name-normalization helpers: all 12 parser-selected month
sheets had zero Faculty marker formulas and zero normalized duplicate Faculty
rows. This records only structural counts, not names or cell values. An
additional isolated local Chrome probe used a generated administrator,
physician, and workbook in temporary storage. It recorded the rendered 1,440px
and 390px preview results, native tab/Space navigation, visible focus treatment,
and a script-disabled GET as described above. The repository diff check for the
audited revision was clean.

```sh
./scripts/quality-check
```

Historical result: passed. Ruff reported 822 files already formatted, Django
reported no system-check issues, and no migration drift was detected. No external service or
production mutation was performed. The sanitized 2026-08-28 staging result
above was supplied by the operator; this audit did not deploy or mutate staging.
The browser assessment includes direct local rendering but did not retain
screenshots. A staffed visual review remains appropriate if the current bounded
operator preconditions change.
