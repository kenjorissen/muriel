# Django web and presentation health audit

**Status:** Current-source audit, 2026-08-26. This records an assessment of the
implemented Django application, not an accepted refactoring plan.

**Terminology note:** Master Schedule clinic labels and identifiers below have
been anonymized to the current neutral terminology after the audit. These renamed
references do not imply that spelling existed on the audit date; the original
findings and dispositions are unchanged.

## Conclusion

Muriel uses conventional server-rendered Django boundaries. URL configuration
selects domain views; views authenticate, parse bounded request context, bind
forms, call named services, and render domain templates. Models and services
remain the owners of scheduling facts and policy. The application has a small,
concrete shared presentation layer rather than a generic page, form, calendar,
or schedule framework.

The strongest reuse contracts are the immutable calendar and table presentation
values, the common workspace shell, ordinary field fragments, and a single CSS
asset. The variants that remain are meaningful: a Master Schedule is a
slot-first table, not a lane calendar; source schedules retain distinct
recurrence, action, history, and status vocabulary; and Schedule Bot retains a
proposal-review surface. No frontend framework or generalized renderer is
indicated by the reviewed code.

The main maintainability pressure is view-local projection assembly. Large
domain view modules combine HTTP handling with construction of calendar, table,
navigation, and return-URL values. That remains policy-light, but makes a
shared presentation change costly to trace. A second, narrower coupling uses
literal request-path prefixes to select primary navigation. Neither issue
demonstrates a current behavior failure.

## Dependency and ownership map

| Concern | Owner and principal contract | Consumers and boundary |
| --- | --- | --- |
| Project routes and errors | `preceptor_project/urls.py`, `preceptor_project/security.py` | One root URL configuration names all routes and error handlers. Domain route prefixes and named URLs are stable enough for templates and redirects; it is intentionally a single-app project rather than a namespaced reusable app. |
| HTTP orchestration | `schedules/views/<domain>.py`; `views/_shared.py` | Views apply `login_required`, parse request context, bind forms, call named services, build local display values, then `render` or `redirect`. They do not form a service API. |
| Submitted input | `schedules/forms/<domain>.py`; `forms/shared.py` | Django forms own submitted representation, trusted choices, and field errors. Services retain authorization, cross-record rules, mutations, and history. `optional_note_field` is a small shared input convention, not a schema. |
| Domain reads and operations | `schedules/services/<domain>.py`, documented in `docs/scheduling-services.md` | Source services, availability, bounded slot projection, schedule-specific policy, ordinary synthetic/reset history, reports, and proposal operations follow the dependency direction documented there. Views do not write retained scheduling facts directly. |
| Calendar geometry | `calendar_presentation.py`: `CalendarLattice`, `CalendarWeek`, `CalendarLane`, `CalendarItem`; `calendar_lattice.html`; `calendar_frame.html` | The lattice owns columns, dates, lanes, placement, scroll framing, and common status structure. Domain views supply explicit domain partials and content. Holiday closure is the one shared informational lane. |
| Master Schedule projection | `services/master_schedule.py`; `master_schedule_presentation.py`; `views/master_schedule.py`; Master Schedule templates | A read-only, weekly, slot-first projection. Its semantic tables intentionally do not consume `CalendarLattice`. |
| Standard tables | `table_presentation.py`: `DataTable`, `DataTableColumn`, `DataTableRow`, `DataTableCell`; `data_table.html` | The 45 ordinary-table adoptions use typed immutable rows, fixed cell kinds, labels, empty states, and bounded width variants. Domains retain columns, row meaning, links, and tones. The nine maintained raw-table exclusions are intentional contracts listed below, not unrecorded failures to adopt `DataTable`. |
| Workspace and surfaces | `base.html`, `schedule_workspace_base.html`, workspace-header/navigation/period fragments, surface-stack fragments | The shell owns authenticated framing, global navigation, account controls, and the presentation-only physician focus. Workspace fragments own placement and responsive structural framing, not domain navigation policy. |
| Forms and content surfaces | Domain form templates, `_form_field.html`, `_form_checkbox.html`, `_form_choice_group.html`, `physician_picker.html` | Shared fragments render labels, field errors, helper text, choices, and the progressive physician-picker enhancement. Domain templates decide field order, fixed/read-only context, action labels, and validation explanation. |
| CSS | `schedules/static/schedules/application.css` | One application stylesheet defines color, spacing, surface, form, table, calendar, and responsive-scroll tokens and variants. It has no separate component build or cascade layer. |
| JavaScript enhancement | Individual files under `schedules/static/schedules/` | Scripts attach to explicit `data-*` hooks for period submission, range previews, calendar actions, role attributes, physician selection, reports, and Solve feedback. Forms retain an ordinary server submission when JavaScript is absent. |
| Authentication and authorization | Django auth views plus `auth_views.py`, `security.py`, `account_administration.py`, `views/accounts.py` | `PublicBoundaryMiddleware` is default-deny except for the small named anonymous set. Login, password reset, invitations, scheduler administration, and private Admin access retain distinct boundaries. |
| Settings and static assets | `settings.py`, `production_settings.py`, `docs/production.md` | Development defaults are separate from fail-closed production environment validation. Production uses manifest static storage and Nginx serves collected, content-hashed files. |
| Deployment-facing web configuration | `ops/nginx/muriel.conf`, proxy snippets, systemd files, deployment and public-edge runbooks | The public listener denies Admin, serves static files, limits sensitive anonymous paths, and proxies only through Gunicorn's Unix socket. The private listener supplies the Admin assertion consumed by Django. |

## Representative paths and reuse assessment

| Path | Current presentation composition | Classification |
| --- | --- | --- |
| Precepting | `views/preceptors.py` supplies month/list values and `CalendarLattice` assignment lanes to `monthly_workspace.html` and calendar partials. | Shared lattice/workspace structure; the monthly shift list is an intentional table variant. Assignment, finding, and Late Doc semantics remain distinct. |
| Schedule Bot proposal | `views/solver.py`, `forms/solver.py`, and proposal review templates reuse Precepting navigation and week lattice values while retaining proposal, stale, reconciliation, and explicit Late Doc review state. | Intentional variant, not a separate generic solver workspace. Its workload tables use `DataTable`; its grouped change-review and Late Doc decision tables are intentional raw-table variants. |
| Master Schedule | `services.master_schedule.master_schedule_week` produces read-only facts; `views/master_schedule._clinic_grid` and `_supporting_grid` adapt them to semantic tables. | Intentional raw-table variants. Forcing these row-grouped coverage grids into `DataTable` or calendar lanes would hide their span, row-group, and slot meaning. |
| Vacation | `views/vacations.py` supplies inclusive range lanes and a list `DataTable`; `forms/vacations.py` owns source-fact input. | Genuinely shared month geometry and form/table mechanics; domain-owned inclusive coverage, correction, removal, and history. |
| Academic Events | `views/academic_events.py` supplies academic-year navigation, calendar/lists, participant presentation, and definition/occurrence forms. `academic-event-form.js` previews selected definition details only. | Shared shell/lattice, picker, and scheduled-event `DataTable` mechanics; the unscheduled and not-occurring action tables are intentional raw-table variants. Definition, annual decision, and participant vocabulary remain distinct. |
| Interviews | `views/interviews.py` presents academic-year calendar/list projections and a dedicated assignment form. | Shared calendar/table/workspace mechanics; intentionally distinct one-shift candidate and finding policy. |
| Rounding | `views/rounding.py` supplies normal-week spans and casual-occurrence values to the seven-day lattice, plus focused source-maintenance views. | Shared geometry only. Normal-week ownership and casual recurrence must remain separate from Precepting policy. |
| Community Precepting | `views/community_preceptors.py` supplies recurrence commitments to the lattice and list/table surfaces. | Intentionally variant source schedule: expected commitments are not Preceptor Assignments or coverage status. |
| Physicians | `views/physicians.py` supplies an identity-centered directory/detail workspace with role and source-fact forms. | Shared local navigation, table, form, and period-picker mechanics; intentionally distinct effective-role/source facts. |
| Reporting and Accounts | `views/reports.py` turns named report projections into tables/PDF links. `views/accounts.py` renders account/invitation administration with Django forms and an interactive account table. | Reporting uses shared table/form/shell mechanics. Accounts shares the form and shell conventions, but its account table is an intentional raw-table variant because each row combines authority/status state with resend and revoke POST forms. Account authority remains intentionally separate. |

The session-backed physician focus (`physician_scope.py`) is a deliberate
cross-workspace presentation concern. The base shell labels it as presentation
only, views pass its selected identifiers only to display projections, and
`set_physician_scope` is login-required, POST-only, validates identifiers, and
validates the local return URL. It is not a scheduling-policy or mutation
filter. That vocabulary is globally coupled by design, not an accidental
Precepting-only abstraction.

### Intentional raw-table inventory

`schedules/tests/test_table_presentation.py` maintains the boundary between the
45 standard `DataTable` adoptions and these nine raw-table exclusions. They are
not a single missing abstraction: each needs markup or interactivity outside
the compact immutable-row contract.

| Raw-table exclusion | Template and contract | Classification |
| --- | --- | --- |
| Accounts | `account_workspace.html` `account-table` | Intentional interactive variant: row-specific authority/status display combines invitation resend/revoke POST forms and account-edit choices. It is not a `DataTable` consumer. |
| Academic Events unscheduled | `academic_event_list.html` unscheduled-events table | Intentional action variant: each definition offers a schedule link and a state-changing "Not occurring this year" form. |
| Academic Events not occurring | `academic_event_list.html` not-occurring-events table | Intentional action variant: each annual decision owns its reversal form. This is separately inventoried because its command differs from the unscheduled table. |
| Schedule Bot change review | `precepting_solver_review.html` `solver-review-week` | Intentional grouped-review variant: expandable week groups combine findings, explanation disclosures, and compare forms with proposal state. |
| Late Doc decisions | `precepting_solver_review.html` `late-doc-review-table` | Intentional decision variant: each retained Late Doc review has state-dependent confirm/replace/view controls. |
| Master Schedule clinic grid | `master_schedule_grids.html` `master-schedule-grid--clinic` | Intentional slot-grid variant: row groups, row spans, day columns, and cell partials express coverage geometry. |
| Master Schedule supporting grid | `master_schedule_grids.html` supporting grid | Intentional grouped-grid variant: schedule groups and row-key labels share the weekly grid but not the clinic row-group structure. |
| Precepting List | `preceptor_schedule_list.html` `preceptor-schedule-list-table` | Intentional schedule-matrix variant: assignment-slot state, finding tones, and week navigation are column-specific. |

The exclusions use the same semantic table elements and scroll framing where
appropriate, but their contracts remain explicit in their domain templates.
Replacing them with conditional `DataTable` cell modes would conceal the
domain-specific grouping and command behavior without a demonstrated repeated
contract.

## Django and web-convention assessment

### HTTP, forms, services, and templates

- Domain services remain the business-operation owners. For example,
  `views/vacations.vacation_create` delegates to `create_vacation`,
  `views/interviews.interview_create` delegates creation and assignment to
  named interview services, and `views/solver.solver_workspace` delegates
  preflight, proposal production, retention, and later apply behavior to
  `services.precepting_solver`.
- Forms own submitted input. Views consistently bind `request.POST`, add
  service `ValidationError` values to the form, return the same template on
  failure, and redirect after successful writes. Fixed fields are explicitly
  replaced from retained context where they must not be trusted, such as the
  physician in `views/vacations.vacation_correct` and an Academic Event
  definition selected by route context.
- Templates primarily render supplied values and use ordinary Django
  conditionals for display state. The consequential exception is the comparison
  form in `precepting_solver_review.html`: when `row.position == 'Late Doc'`,
  it derives `assignment_type=late_doc`, an empty `period_of_day`, and
  `position=late-doc` for the POST command from that display label. The
  `solver_compare` view accepts that command (`views/solver.py`) and the solver
  protocol separately recognizes the same Late Doc key
  (`services/precepting_solver._proposal_edit_key`). This is protocol mapping,
  not merely display conditionality; W5 records its ownership and disposition.
  `views/master_schedule._position_label`, by contrast, adapts stable domain
  labels before template rendering.
- Views are thin with respect to scheduling mutation policy, but not always
  thin with respect to read projection and display shaping. In particular,
  `views/preceptors.py`, `views/solver.py`, `views/rounding.py`, and
  `views/physicians.py` contain local lattice/table builders, navigation and
  return URL construction, and view-specific grouping in addition to handlers.
  The projections are not currently shared enough to justify a generic
  presenter layer; the consequence is captured in finding W2.

### Routes, authentication, errors, and CSRF

- `urls.py` uses named paths and routes templates/views through `{% url %}` or
  `reverse`, rather than hard-coded application URLs. The hierarchy is concrete
  and readable by domain: `vacations/`, `academic-events/`, `interviews/`,
  `rounding/`, `community-preceptors/`, `physicians/`, `reports/`, and
  `accounts/`. Its flat single-file form is acceptable for one Django app, but
  primary-navigation selection has a path-prefix exception (W1).
- Anonymous access is default-deny in
  `security.PublicBoundaryMiddleware.PUBLIC_VIEW_NAMES`. Login, password reset,
  and invitation acceptance are the explicit public workflows; authenticated
  views also retain `login_required` as local defense. Account administration
  checks scheduling-administrator authority in `views/accounts.py`.
- POST forms include Django CSRF tokens. The physician-focus mutation is
  `@require_POST`; invitation resend/revoke reject non-POST requests. Ordinary
  state-changing views use service guards and Django's CSRF middleware.
- Project handlers and `CSRF_FAILURE_VIEW` render application error templates.
  `ProductionErrorPageMiddleware` replaces production 400 responses with the
  safe error page. This is a deliberate security boundary rather than generic
  error-page styling.
- Django Admin remains installed for break-glass account administration and
  retained-record inspection. Most scheduling records use
  `RetainedPhysiciansRecordAdmin` to prohibit add/change/delete. Public Nginx
  returns 404 for `/admin/`; the private listener sets the header required by
  `PublicBoundaryMiddleware` for `admin:` routes.

### Progressive enhancement, accessibility, and responsive behavior

- JavaScript is enhancement, not the source of form authority. Calendar day and
  week actions start as links; period controls retain submit buttons; range,
  report, Solve, role, and physician-picker scripts change visibility or offer
  immediate feedback while their server forms still validate the request.
- The shell sets document language and viewport, provides a skip link and a
  named global navigation region, and uses `aria-current` for selected
  navigation. Shared forms render associated labels, fieldsets/legends for
  choices, server errors, and live regions where selection or Solve state
  changes.
- `data_table.html` uses `table`, `thead`, scoped headers, row headers where a
  `DataTableCell` declares one, labels, and an explicit empty state.
  `table-scroll-frame` retains table structure and makes overflow horizontally
  scrollable rather than reflowing data into a different untested pattern.
- `calendar_lattice.html` gives the shared calendar grid/row/column structure,
  accessible date `time` values, row and lane labels, and explicit action
  labels. CSS gives five-, seven-, assignment-week, interview, and Master
  Schedule surfaces minimum widths inside a horizontal scroll frame. This is
  suitable structural evidence, but actual narrow-viewport and assistive-
  technology behavior remains browser-review work (W4).

### Assets, settings, and deployment

- `settings.py` keeps development SQLite, allowed-host parsing, mail settings,
  proxy trust, and application settings explicit. `production_settings.py`
  requires an adequate production secret, one exact host and HTTPS origin,
  fixed PostgreSQL socket posture, secure cookies, HSTS, and proxy-header
  handling limited to the Unix-socket path.
- Development static paths are conventional `{% static %}` calls. Production
  uses `ManifestStaticFilesStorage`; `docs/production.md` requires a manifest
  entry and Nginx serves `/static/` with a seven-day cache lifetime. Content
  hashes make that cache duration safe for deployed assets.
- The Nginx and systemd configuration is specific to the accepted single-host
  deployment. It is not coupled to the domain presentation layer beyond serving
  the collected static asset root and proxying application requests.

## Shared-component input assessment

`DataTable` has the most compact and enforceable reusable input contract:
identity, label, columns, immutable rows/cells, fixed cell kinds, and a small
width/tone vocabulary are validated in `table_presentation.py` before template
rendering. `CalendarLattice` similarly makes geometry, placement, status, and
actions explicit with frozen dataclasses. Its domain content remains flexible by
design: `CalendarItem.partial` names a domain partial and `content` is an object
owned by that partial. Workspace and form fragments take conventional context
values rather than trying to express a page schema.

This is the correct balance for current variants. The dynamic template names
and CSS status/variant strings are less mechanically constrained than
`DataTable`; they are a bounded source-level contract, not evidence for a
component registry or frontend framework. W3 records the review cost that comes
with that choice.

## Findings and validation needs

| ID | Consequence | Confidence | Likely owner | Browser review required | Evidence and disposition |
| --- | --- | --- | --- | --- | --- |
| W1 | Low to medium maintainability/accessibility risk: a new route can render an incorrect primary selected state if its literal path prefix is not also added to the base template. | High | Project routing and shared shell | No | `base.html` selects `aria-current` with `request.path` slices for every global destination, while `urls.py` owns the actual hierarchy. Named URLs still own destinations. This is a narrow accidental parallel route map, not a routing correctness or authorization flaw. |
| W2 | Medium maintainability risk: a projection or shared-surface change requires editing and understanding large mixed-purpose view modules, making accidental display regressions easier. | High | Respective domain view modules | Usually, for shared visible surfaces | `views/preceptors.py` and `views/solver.py` build calendars and presentation groupings beside request handlers; `views/rounding.py`, `views/academic_events.py`, and `views/physicians.py` do the same for their variants. Services still own business rules and templates do not recompute them. Do not extract a generic presenter merely for symmetry; extract only a repeated, stable domain projection if a concrete change demonstrates one. |
| W3 | Low maintainability risk: a malformed calendar partial name, status, or CSS variant fails late in rendering or silently loses a style rather than being rejected by a narrow enum-like contract. | High | Shared calendar/template/CSS owners and affected domain view | No, except when changed surface behavior needs it | `CalendarItem.partial` and `content` are intentionally open (`calendar_presentation.py`), and `calendar_lattice.html`, `calendar_frame.html`, and surface-stack fragments dynamically include template names. Calendar status/variant values likewise become CSS class strings. This is acceptable for the small in-repository set, but focused response tests should accompany new variants. A registry would make normal domain partials less direct without observed pressure. |
| W4 | Medium confidence gap, not a source-proven defect: horizontal calendar/table scrolling, grid semantics, focus visibility, and hover/focus-only affordances need real narrow-browser and assistive-technology observation after shared primitive changes. | Medium | Shared presentation owners | Yes | Source shows semantic landmarks, labels, `time`, table headers, focus styles, live regions, and scroll frames. Existing browser tests named in `calendar-presentation-variants.md` cover selected surfaces, but source inspection cannot prove usable touch scrolling, screen-reader grid traversal, zoom/reflow, contrast, or focus behavior across the full variant set. This is the earlier audit's still-current regression risk. |
| W5 | Medium maintainability/correctness risk: changing a display label or the compare command protocol independently can target the wrong assignment class, especially for Late Doc's empty-period key. | High | Solver review view/template and solver command protocol | No | `precepting_solver_review.html` derives three hidden command fields from the displayed `Late Doc` position; `views/solver.solver_compare` receives them and `services/precepting_solver._proposal_edit_key` defines the corresponding valid key. This is a consequential template-boundary exception. Keep the protocol mapping with the view/service-owned command representation when that surface changes; do not introduce a generic presenter or template-command framework solely for this one mapping. |

## Reconciliation with earlier architecture records

`presentation-workspace-architecture.md` remains current for its declared
ownership. Its historical finding of separate workspace wrappers and calendar
markup is **resolved**: the shell, workspace frame, surface stack, calendar
frame/lattice, period controls, ordinary form fragments, and data table now
have the documented concrete owners. Its deferral of a generic workspace
configuration, form schema, table-policy owner, and page builder remains
**current**.

`calendar-presentation-variants.md` is likewise **resolved** for copied
weekday headers, local calendar frames, duplicated picker enhancement, and
domain-specific calendar framing. The single lattice remains implemented and
the documented Master Schedule exception remains intentional. Its requirement
for response and browser/accessibility evidence after shared changes is
**still current** and is restated as W4.

`architecture-roadmap.md` and `scheduling-domain-ownership.md` remain current
for the service/dependency direction: views and forms adapt named domain owners;
shared presentation must not absorb scheduling policy; and generic schedule,
calendar, form, table, or rule abstractions remain deferred. The
`domain-application-health.md` capacity observation is historical and resolved
in the current implementation; it was a service-capacity/policy issue, not a
presentation-layer reason to alter this audit's reuse conclusions.

The earlier records' description of `DataTable` as the ordinary semantic table
owner remains **current with qualification**: the maintained inventory now
makes its nine intentional raw-table contracts explicit, including Accounts and
the Academic Events, Schedule Bot, Master Schedule, and Precepting variants.
The earlier requirement that templates not recompute scheduling
policy remains **current with one recorded exception**: W5 identifies the
solver review's display-label-to-command mapping rather than treating it as a
general presentation convention.

## Evidence limits

This audit inspected current source and the focused response/browser test
inventory. It did not run a browser, a screen reader, production deployment, or
network probe, and it did not change application code, assets, settings, tests,
or production configuration. W4 identifies the exact cases for browser review;
the remaining conclusions are source-verifiable ownership and coupling
assessments.
