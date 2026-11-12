# Browser-test static serving

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective and authorization

Repair the browser-test static-serving defect using Django's supported staticfiles test server. The human requested this after seeing unhandled TypeError tracebacks in successful checks. This Task independently delivers working real asset transport with direct regression protection. The separate successor task-000013 adds general browser request-failure detection; staging and dependency upgrades remain on hold until both pass. Do not combine that successor's monitoring policy with this transport repair.

## Cause, contracts and owners

- On accepted revision `1f52fb128aa7253b004cc9f9059c3e07232452ec`, the focused `schedules.tests.test_float_schedule_inputs.FloatScheduleInputBrowserTests` run reproduced five `TypeError: expected str, bytes or os.PathLike object, not NoneType` tracebacks while reporting one test `OK`. Its runner loads real assets but checks document/form semantics rather than asset success.
- `preceptor_project/test_settings.py` inherits `settings.py`, leaving `STATIC_ROOT=None`. Ordinary Django `LiveServerTestCase` uses `_StaticFilesHandler`, which passes that root to filesystem serving. Multiple browser classes declare `serve_static=False`; Django does not consume that attribute. A direct `/static/schedules/application.css` request through this handler reproduces the exception.
- Django owns test-server/staticfiles transport. Its supported `django.contrib.staticfiles.testing.StaticLiveServerTestCase` uses staticfiles finders without collectstatic: https://docs.djangoproject.com/en/5.2/howto/static-files/#testing and https://github.com/django/django/blob/5.2.17/django/contrib/staticfiles/testing.py . Prefer that abstraction rather than a custom server, fabricated production STATIC_ROOT or patch of Django internals.
- Trace all current `LiveServerTestCase` consumers: `schedules/tests/*browser.py`, `test_float_schedule_inputs.py`, and `test_schedule_reports.py`. Existing runners sometimes fulfill selected assets or abort excluded requests intentionally. Preserve justified scenario isolation, no-JavaScript assertions, layout fixtures and existing product behavior; not every layout test needs conversion into an unmocked asset integration test.
- `schedules/tests/browser_tools.py` owns declared tooling discovery; `browser_session.cjs` and `test_browser_session.py` own/protect existing Playwright deadlines and cleanup. This transport repair must not redesign those lifetimes. `scripts/check` owns offline browser/JavaScript/parallel Django verification; `scripts/quality-check` owns fast verification. `README.md` documents their conventions.
- `docs/design.md` section 26.3 preserves pinned Ruff/shared-virtualenv verification; section 27 prefers cohesive explicit framework owners. Section 26 and `docs/staging.md` retain separate commissioned production/staging configuration. Test-server repair does not authorize changing production/static deployment or accessing live instances.

## Acceptance

1. All browser-test servers sharing the faulty assumption use supported Django staticfiles behavior. Actual application CSS and JavaScript are served without collected STATIC_ROOT or the reported TypeError; remove ineffective test configuration. Preserve pins (including accepted Psycopg 3.3.8), runtime settings, application routes, models, semantics and existing browser assertions.
2. Direct real HTTP regression evidence verifies an existing stylesheet and script return successful status, appropriate content type and identifiable content; a nonexistent asset returns normal 404 rather than 500/traceback. The broken predecessor must fail the positive asset assertions. Mocked route fulfillment alone cannot establish this contract. Protect behavior rather than inheritance names, filenames beyond chosen fixture assets, or handler call sequences.
3. The focused Float browser scenario passes without the original tracebacks, and the existing browser suite remains valid. These direct asset/404 guarantees and regressions survive task-000013's additional detection; there is no temporary restriction or interface to retire.
4. `scripts/quality-check` and `scripts/check` pass with browser coverage and exclusions unchanged. Inspect output as well as exit codes; unexplained unexpected errors remain failures to investigate. Do not suppress stderr, remove assertions, abort all assets, skip tests or declare historical checks clean. Attribute any intentional negative-test diagnostics explicitly. Briefly document the static-serving convention in the existing developer-check documentation.

## Scope and verification

Use the shared main-repository `.venv/bin/python` and existing declared Chromium/Node installation. No dependency upgrades, live staging/production access, deployment, pushes, resets, network-dependent Checks or new browser framework. Temporary fixtures use Django test databases and invocation-owned scratch only; remove exact owned resources and report residue. Cassidy owns its worktrees.

Record tested revision, before/after reproduction, affected consumers, real asset/404 regression evidence, focused/full results, unexpected-output assessment and cleanup. Exact Candidate Check and independent Review remain required. The coordinator retains the staging hold until the separate detection Task and exact-revision GitHub verification also pass.

## Execution strategy

Reproduce the focused Float case, then use the standard Django staticfiles boundary consistently across the affected test-server consumers. Add the smallest real HTTP conformance test proving assets are genuinely served and missing assets are not server errors. Preserve deliberate browser interception; broad failure monitoring belongs to the successor, not this Task. Run focused browser evidence before the complete Check and inspect output. A working transport predecessor is independently observable and remains useful when the successor adds detection of future request failures.
