# WhiteNoise 6.12.0

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective

Upgrade WhiteNoise from 6.11.0 to 6.12.0, preserving the isolated staging application's collected-static delivery and existing production Nginx ownership. This is one static-serving compatibility outcome, not a server or framework upgrade.

## Contracts and owning evidence

- `docs/design.md` section 26, commissioned deployment and staging isolation direction; `docs/staging.md`, staging settings/static delivery and non-destructive update contract; `docs/production.md`, Runtime installation and Readiness: production uses Nginx with manifest storage, staging uses its separate static root and loopback service.
- `requirements.txt`, `preceptor_project/staging_settings.py`, `preceptor_project/production_settings.py`: WhiteNoise is staging middleware, `DEBUG=False`, with Django ManifestStaticFilesStorage. Preserve security, cookies, middleware behavior, database identity, and dummy mail settings.
- `schedules/tests/test_staging_instance.py`, especially `StagingSettingsTests.test_staging_serves_a_collected_stylesheet`, already exercises actual collectstatic and WhiteNoise delivery under staging settings using a temporary static directory. `schedules/tests/test_production_runtime.py` protects the distinct production owner.
- Target is the WhiteNoise portion of captured Dependabot PR #2 revision `bfb734df6079ecb45b51591eee96ed744d2737a6`, not its other dependencies.
- Official release contract: https://github.com/evansd/whitenoise/blob/6.12.0/docs/changelog.rst and https://github.com/evansd/whitenoise/blob/6.12.0/docs/django.rst . Version 6.12.0 drops Python 3.9 (Muriel uses Python 3.14) and fixes potential unauthorized file access in autorefresh mode. Autorefresh defaults to DEBUG and is unsuitable for production; do not enable it or confuse that upstream fix with proof of exposure in this deployment.

## Acceptance

1. Pin and install WhiteNoise 6.12.0 in the shared main virtualenv for worker and authoritative Check. Preserve every other runtime and development pin, including the accepted Ruff and Actions upgrades.
2. Collected staging assets still serve their expected content and media type under non-debug settings, including manifest-resolved application assets. Missing assets must not be served as unrelated content. Preserve the existing static root, manifest behavior, and security configuration without enabling autorefresh or finders in staging, introducing storage alternatives, or changing production Nginx ownership.
3. Existing quality and complete checks pass without disabling browser checks, loosening assertions, or adding network/install behavior to verification. Make only compatibility changes required by this upgrade; escalate any consequential behavior or framework decision.

## Verification and boundary

Use the existing staging collected-static test as primary owning evidence; extend it only where needed to protect manifest-resolved delivery or an actual compatibility gap, using temporary collected assets rather than a live server directory. Assertions should protect delivered content and HTTP semantics, not freeze version strings, hashed filenames, incidental cache values, or equivalent internal implementation. Record installed version, upstream applicability assessment, focused results, `scripts/quality-check`, and `scripts/check`.

Worker authority excludes remote pushes, PR disposition, deployment, protected server configuration, data replacement, and live service mutation. After acceptance the authorized coordinator owns exact-revision GitHub verification and a non-reset staging update/health and static HTTP check, potentially grouped with another small runtime increment. That operational evidence is not implied by local Check and remains outstanding until observed. No production rollout or authenticated smoke-test claim is authorized by this Task.
