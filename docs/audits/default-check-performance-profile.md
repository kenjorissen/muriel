# Default Check performance profile

**Status:** Historical source measurement, 2026-08-28. Goal 000132 and Task
000410 below are private pre-export provenance, not required public references.
The initial recommendations and subsequent implementation measurements are
retained separately; they do not define current test selection or Check
behavior. Its publication-presentation measurements predate retirement of that
workflow. The measurements are not verification of a new public revision.

## Scope and method

Measurements were made on the historical development host (machine name
omitted) from the disposable worktree using the main worktree virtual
environment. The first `scripts/check` invocation was allowed
to warm imports and local executable caches but exceeded the original
two-minute execution bound while Django tests were still running. The measured
run followed it without source, dependency, or database-schema changes. A
second Django-only run used the exact Check arguments, except
`--durations=50` rather than `--durations=10`, to identify the full slow tail.

"Warm" therefore means an already-installed environment and warmed executable
caches; Django correctly creates and destroys its test database on every run.
Timings are wall-clock seconds reported by `/usr/bin/time` unless stated
otherwise. No product code, test selection, or Check behavior changed during
this audit.

## Check phase timing

| Phase | Command used by `scripts/check` | Warm wall time | Notes |
| --- | --- | ---: | --- |
| Ruff lint | `ruff check .` | 0.01s | 806 files scanned. |
| Ruff format verification | `ruff format --check .` | 0.01s | 806 files scanned. |
| JavaScript interaction tests | `node --test` over seven listed files | 0.07s | 21 tests; Node reported 52.9ms. |
| Django system check | `manage.py check` with test settings | 0.53s | No issues. |
| Django tests | `manage.py test --parallel=4 --timing --durations=10 --exclude-tag=synthetic-solver-integration` | 131.83s | 1,218 tests, one skipped; runner total includes 0.85s database setup. |
| Complete `scripts/check` | exact script | 132.89s | User CPU 477.57s; system CPU 10.42s; peak RSS 299,600 KiB. |

The second exact Django profile completed 1,218 tests in 130.24s and reported
131.39s total runner time (131.85s external wall time). The 0.44s runner-time
spread is normal measurement variation. Django tests account for more than 99%
of the complete Check wall time; lint, formatting, JavaScript, system checking,
and test database setup are not meaningful optimization targets.

## Parallelism and suite effect

The first exact Check consumed 487.99 CPU seconds over 131.83 runner seconds;
the second used 481.86 CPU seconds over 131.39 seconds. That is 3.70 and 3.67
average active CPU cores respectively against `--parallel=4`. The run is close
timing report does not identify a worker for each test, so it cannot prove an
exact final shard schedule without adding temporary runner instrumentation.

The test runner distributes `TestCase` units rather than independently placing
their individual methods. The following indivisible high-cost bundles create a
real balancing constraint even though the observed aggregate utilization is
good:

| TestCase bundle | Isolated warm time | Relevant full-suite method time | Consequence |
| --- | ---: | ---: | --- |
| `SyntheticScenarioSourceHistoryTests` | 51.22s for five tests | 51.68s total | One worker must process five complete source-history builds. |
| `SyntheticScenarioPreceptingHistoryTests` | 43.16s for five tests | 44.67s total | One worker must process five complete Precepting-history builds. |
| `StagingSyntheticResetTests` | 72.01s for its only test | 75.27s | One non-splittable staging load is nearly 58% of total runner wall time. |
| `ResetModeTests` | 37.28s for its only test | 39.63s | One non-splittable four-database subprocess exercise. |

The individual comparison does not show a material suite-only slowdown. The
four sampled dominant tests were 4-7% slower in the parallel complete suite
than isolated: staging synthetic reset 75.27s versus 72.01s, production
synthetic reset 55.44s versus 53.32s, reset modes 39.63s versus 37.28s, and a
source-history assertion 10.12s versus 9.88s. This is consistent with ordinary
four-process CPU contention, not an order-dependent leak or fixture conflict.
The primary pressure is repeated multi-year data generation and real-database
subprocess work.

## Slow-test inventory and ownership

The table includes every test at or above two seconds in the 50-duration warm
profile. Classification is about why the expensive exercise belongs in a test
taxonomy, not a claim that its protected behavior is unimportant. Times are the
second full-suite profile; the narrow owner names the production unit responsible
for the protected behavior.

| Warm time | Test | Classification | Protected behavior and narrow owner | Evidence-led recommendation |
| ---: | --- | --- | --- | --- |
| 75.27s | `StagingSyntheticResetTests.test_fresh_staging_database_loads_with_a_non_login_audit_actor` | Deployment/platform evidence | A confirmed isolated staging load creates a usable non-login audit actor and retained synthetic scheduling history. Owner: `reset_staging_synthetic.Command` and `reset_production_domain.replace_domain`. | Relocate to an explicit, reproducible staging-load check. Keep a focused default command-guard/actor test, but do not make every candidate build the full multi-year staging scenario. |
| 55.44s | `ProductionDomainCommandTests.test_synthetic_reset_batches_large_historical_validation_and_is_reproducible` | Deployment/platform evidence | Production-domain synthetic reset preserves login records, creates a 2022 history, leaves future work open, and reproduces the same snapshot. Owner: `reset_production_domain` plus `SyntheticScenario`. | Relocate the full two-run large-history reproducibility exercise. Retain a reduced command test for login preservation, a bounded generated history, and expected work boundaries. |
| 39.63s | `ResetModeTests.test_modes_rebuild_real_databases_with_the_requested_login_scope` | Representative integration | The four `reset_demo` mode combinations rebuild an actual SQLite database and enforce their login/domain scope. Owner: `reset_demo.Command`. | Consolidate to one representative real subprocess mode in the default suite and move the four-mode matrix to an explicit reset-tool integration check; preserve focused guard and restoration tests. |
| 11.90s | `SyntheticScenarioSourceHistoryTests.test_later_generation_extends_stable_source_history_without_reshuffling` | Performance/load evidence | Re-running a multi-year generated source history extends dates without altering prior choices. Owner: `SyntheticScenario.generate_source_schedule_history`. | Move the full historical extension case to an explicit synthetic-scenario check. Keep a smaller deterministic extension assertion in the default suite. |
| 11.47s | `SyntheticScenarioPreceptingHistoryTests.test_later_generation_advances_the_boundary_without_reshuffling_completed_history` | Performance/load evidence | Later generation preserves completed Precepting assignments while advancing the completed/open boundary. Owner: `SyntheticScenario.generate_precepting_history`. | Move the multi-month/multi-year exercise to the explicit scenario check; retain a short boundary fixture in default coverage. |
| 10.60s | `SyntheticScenarioSourceHistoryTests.test_source_absences_preserve_faculty_equivalent_coverage` | Performance/load evidence | Generated vacations and events leave two Faculty-equivalent choices for each affected slot. Owner: `SyntheticScenario` source generators and `slot_projection`. | Reduce to a short generated range or a direct slot-projection fixture; keep the capacity invariant. |
| 10.39s | `SyntheticScenarioSourceHistoryTests.test_source_reports_have_meaningful_calendar_and_academic_year_coverage` | Performance/load evidence | Generated source facts produce nonempty, meaningful reports across five schedules. Owner: `SyntheticScenario.generate_source_schedule_history` and `schedule_reports`. | Move broad source-report coverage to the explicit scenario check; retain one representative report integration test with a narrow fixture. |
| 10.31s | `SyntheticScenarioSourceHistoryTests.test_schedules_manually_movable_holidays_within_the_source_and_attention_bounds` | Performance/load evidence | Generated manually schedulable holidays stay inside baseline and configured attention limits. Owner: `SyntheticScenario._schedule_holidays`. | Replace the full-history fixture with direct holiday-generation coverage of the boundary; keep the range invariant. |
| 10.12s | `SyntheticScenarioSourceHistoryTests.test_generates_complete_source_history_and_attention_horizon_facts` | Performance/load evidence | A complete generated source scenario has expected facts through attention horizons. Owner: `SyntheticScenario.generate_source_schedule_history`. | Move this complete-shape assertion with the other source-history cases; retain a minimal baseline/source smoke test. |
| 9.88s | `SyntheticScenarioPreceptingHistoryTests.test_global_work_remaining_contains_every_future_precepting_week_and_nothing_else` | Performance/load evidence | Generated future Precepting work exactly matches global Work Remaining. Owner: `SyntheticScenario._validate_future_work_remaining` and `work_remaining.global_work_remaining`. | Keep the exact set assertion but construct only the short future boundary required for it. |
| 9.59s | `SyntheticScenarioPreceptingHistoryTests.test_rejects_extra_non_precepting_global_work_remaining` | Performance/load evidence | Scenario validation rejects non-Precepting work in its expected future-work set. Owner: `SyntheticScenario._validate_future_work_remaining`. | Replace the full generated history with a focused patched work-summary fixture; the existing injected extra item already supplies the failure stimulus. |
| 9.25s | `ProductionDomainCommandTests.test_demo_uses_selected_existing_superuser_without_creating_demo_logins` | Deployment/platform evidence | A production-domain demo reset retains the selected actor and does not create demo accounts. Owner: `reset_production_domain.replace_domain`. | Keep as a reduced command integration test once the heavyweight synthetic reset is explicit; do not combine it with synthetic data generation. |
| 9.01s | `SyntheticScenarioConfiguredHolidayHorizonTests.test_schedules_holidays_through_the_configured_non_default_attention_horizon` | Ordinary correctness | A changed holiday lead time extends scheduled holidays through its exact horizon. Owner: `SyntheticScenario._schedule_holidays` with `lead_times`. | Reduce the fixture to the holiday owner and a short date range; no complete source history is needed to establish the horizon. |
| 7.64s | `ProductionDomainCommandTests.test_synthetic_replaces_the_domain_without_changing_login_records` | Deployment/platform evidence | A synthetic production-domain replacement retains current login-owned records and lead-time policy. Owner: `reset_production_domain.replace_domain`. | Move beside the full production reset check or reduce to a short synthetic fixture; preserve the login/domain boundary. |
| 7.12s | `SyntheticScenarioPreceptingHistoryTests.test_rerun_rejects_published_future_precepting_without_mutation` | Performance/load evidence | Regeneration refuses to clear a published future week and leaves all scheduling facts intact. Owner: `SyntheticScenario._require_unpublished_future`. | Exercise the rejection with one short future week and direct generated inputs rather than a complete historical build. |
| 6.53s | `SyntheticScenarioPreceptingHistoryTests.test_completes_the_current_month_and_leaves_every_later_initialized_week_open` | Performance/load evidence | Generation completes history while leaving later initialized Precepting and Late Doc work open. Owner: `SyntheticScenario.generate_precepting_history`. | Move the full scenario assertion; retain a one-month complete/open boundary test. |
| 4.64s, 4.58s | `ProductionDomainCommandTests.test_empty_preserves_every_login_owned_record_and_removes_every_domain_model`; `test_ambiguous_demo_actor_fails_before_domain_mutation` | Deployment/platform evidence | Empty production-domain reset preserves login ownership; ambiguous actor fails before a domain write. Owner: `reset_production_domain.replace_domain`. | Keep focused safety assertions, but run them with a small domain fixture and share setup with the production reset integration path. |
| 4.53s | `LeadTimePolicyTests.test_demo_seed_restores_defaults_after_an_empty_database_reset` | Accidental duplication | Demo seeding restores the complete lead-time default policy after flush. Owner: `reset_demo.seed_demo_data` and `lead_times.ensure_lead_time_defaults`. | Consolidate the default-policy assertion into one retained demo-seed integration test, or seed only the lead-time owner in this test. |
| 4.52s, 4.51s, 4.49s, 4.49s | `DemoSeedTests.test_scheduler_credentials_can_sign_in_and_a_fresh_reseed_has_the_same_data_shape`; `LoginPreservationTests.test_seeding_demo_scheduling_administrator_does_not_expand_preserved_group_authority`; `LoginPreservationTests.test_preserved_demo_scheduler_is_not_changed_when_seeding`; `LoginPreservationTests.test_command_output_never_includes_credentials_when_creating_the_demo_scheduler` | Accidental duplication | Each test invokes a complete demo seed principally to inspect one account/authority/output invariant. Owner: `reset_demo.seed_demo_data`, account restoration, and demo-account creation. | Preserve the distinct authority and credential invariants, but share one seeded fixture per compatible class or isolate the account-creation helper so those tests do not each build all scheduling data. |
| 4.12s | `PreceptingSolverWorkspaceBrowserTests.test_browser_selects_month_quarter_and_custom_ranges` | Representative integration | Browser range selection produces reviewable Schedule Bot proposals at the workspace boundary. Owner: `views.solver` and solver workspace templates/scripts. | Retain in the default suite: it is the only materially slow browser interaction and covers distinct request/UI integration, not load evidence. |
| 2.90s | `FormPageIntroBrowserTests.test_workspace_history_and_read_only_pages_share_the_contained_intro_at_all_widths` | Representative integration | Sixteen history/read-only pages retain one accessible, aligned contained intro at desktop and narrow widths. Owner: shared form-page intro presentation. | Retain the shared-surface regression check; if future cost matters, select one page per structural variant rather than remove mobile or keyboard evidence. |
| 2.54s, 2.28s | `PreceptingSolverWorkspaceBrowserTests.test_browser_confirms_and_replaces_late_doc_then_compares_and_applies`; `PreceptingSolverWorkspaceBrowserTests.test_browser_week_places_every_publication_status_in_the_header_gutter` | Representative integration | Browser evidence covers Late Doc review/apply and publication-status placement. Owner: solver workspace presentation and publication view data. | Retain: the two tests protect separate user-visible boundaries and are small relative to the reset/scenario cost. |
| 2.04s | `PreceptingSolverWorkspaceTests.test_month_and_quarter_range_selections_create_reviewable_proposals` | Ordinary correctness | Month and quarter selection constructs reviewable solver proposals. Owner: solver workspace view and proposal services. | Retain in the default suite; it is direct functional coverage, not a load exercise. |

The remaining 23 reported tests were below two seconds. The next most expensive
were migration snapshot tests, narrow browser proposal checks, and a six-month
balance-history solver scenario at 1.53s. The Check already excludes the separately tagged
`synthetic-solver-integration` one- and three-month staging probe, which
is the appropriate explicit route for live representative solver performance.

## Recommended implementation order

These were proposals for Task 000410, not changes made by the initial audit.
The implemented disposition is recorded below; this is not a new work plan:

1. Move the 75s staging load and 55s production synthetic reset to explicit,
   reproducible deployment/synthetic checks. Their full-scale behavior is
   valuable, but it is not ordinary candidate correctness.
2. Move the complete source- and Precepting-history suite to an explicit
   synthetic-scenario check, replacing default coverage only with small fixtures
   that directly preserve each listed invariant. This removes repeated
   multi-year construction, not the invariants.
3. Collapse repeated whole-demo seeding and the four-mode reset matrix only
   where tests share the same reset owner. Keep separate assertions for account
   authority, credential secrecy, and selected-actor behavior.
4. Leave direct ordinary correctness and representative browser tests in the
   default Check unless a smaller fixture demonstrates the same boundary. Do not
   reduce the Check by hiding failures, weakening assertions, or replacing all
   integration paths with mocks.

The proposed follow-up was to repeat the exact phase table and the two
full-suite warm runs after an accepted implementation, recording the same test
count, tag exclusion, test-database setup time, CPU utilization, and slow-test
taxonomy so a reduced time could be attributed to relocated or narrowed evidence
rather than lost coverage. The retained follow-up measurements are below.

## Portable reproduction guidance

For new verification, run `./scripts/check` from the source worktree. It resolves
the shared main-repository virtual environment itself. Current tests and helper
behavior, not the historical counts or timings above, govern that run.

The following is an adapted current diagnostic example, not a verbatim
historical command or a reproduction of the old revision. From the source
worktree root, replace `/path/to/main-repository` with the absolute main checkout
root containing the shared `.venv`, not the disposable worktree root. The original profile excluded only
`synthetic-solver-integration`; this example uses all current Check exclusions:

```sh
MAIN_REPOSITORY=/path/to/main-repository
DJANGO_SETTINGS_MODULE=preceptor_project.test_settings \
  "$MAIN_REPOSITORY/.venv/bin/python" manage.py test \
  --parallel=4 --timing --durations=50 \
  --exclude-tag=synthetic-solver-integration \
  --exclude-tag=synthetic-scenario-integration --exclude-tag=reset-tool-integration
```

For a coarse indivisible-bundle measurement, use the same interpreter, settings,
and exclusions with either
`schedules.tests.test_synthetic_scenario.SyntheticScenarioSourceHistoryTests`
or `schedules.tests.test_synthetic_scenario.SyntheticScenarioPreceptingHistoryTests`
as the test label and `--timing --durations=10`. These classes now use different
fixtures from the original profile; new measurements cannot stand in for the
historical results. Full-scale integration coverage remains separately opt-in
through the helpers below; source-document cleanup does not require running it.

## Implemented placement and measurement

Task 000410 retained short, fixed-date source-history and Precepting-history
regressions in the default suite. They exercise generated inputs, holidays,
reports, faculty capacity, completed/open Precepting boundaries, future-work
validation, and refusal to overwrite a published future week without rebuilding
multi-year history for each assertion. The default suite also retains direct
staging audit-actor and empty-reset login-scope tests.

The representative reset-tool matrix, production-domain replacement, staging
synthetic load, source-history extension, and multi-year source plus Precepting
scenario are tagged outside the candidate gate. They remain deterministic,
bounded test-database coverage behind these fixed-purpose commands:

```sh
scripts/check-reset-tool-integration
scripts/check-synthetic-scenario-integration
```

`scripts/check` excludes only `reset-tool-integration`,
`synthetic-scenario-integration`, and the pre-existing
`synthetic-solver-integration` tag. Its shell `set -eu` behavior and each check
phase remain unchanged, so an unavailable tool or a failed retained test stops
the command without modifying source files.

Warm measurements were repeated on the same historical development host on
August 28, 2026 with the shared main-worktree virtual environment. Each run
recreated the Django test database; no source, dependency, or schema changes
occurred between samples.

| Measurement | Before | After |
| --- | ---: | ---: |
| Complete Check wall time, sample 1 | 132.13s | 64.92s |
| Complete Check wall time, sample 2 | 133.11s | 66.00s |
| Django runner total, sample 1 | 130.97s | 63.84s |
| Django runner total, sample 2 | 132.06s | 64.92s |
| Retained Django tests | 1,218 | 1,209 |

The post-change runs passed all retained tests with one expected skip. The
relocated reset-tool command passed 10 tests in 203.90s, and the representative
synthetic-scenario command passed two tests in 86.92s. These explicit durations
are intentionally not candidate-gate budget; they retain the full-scale
deployment and multi-year evidence for changes to those owners.
