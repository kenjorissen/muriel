# Synthetic staging process

Synthetic staging is a second, deliberately isolated Muriel instance on
the installation's single host. It has the fixed service identity and PostgreSQL role
`muriel_staging`, database `muriel_staging`, release root
`/srv/muriel-staging`, environment file `/etc/muriel-staging/environment`, runtime
directory `/run/muriel-staging`,
and journald unit `muriel-staging.service`. None of those paths or identities is
shared with production.

For a first installation or update from a clean checkout, first configure an
explicit private OpenSSH alias with the installation's hostname, user,
credentials and options (see [staging setup](deployment.md#synthetic-staging)).
Then one command prepares the host, deploys the current revision, and replaces
only staging data with the synthetic dataset:

```sh
export MURIEL_SSH_TARGET=staging-a
export PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test
export PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000
ops/update-staging "$MURIEL_SSH_TARGET" --reset-and-load
```

All update modes require exactly one target matching local `MURIEL_SSH_TARGET`.
Aliases start with an ASCII letter or digit, followed only by ASCII letters,
digits, dots, underscores or hyphens. Missing/invalid configuration, mismatches,
duplicate targets and duplicate/unknown flags refuse before archives, transfer
allocation or connection, without trimming, inference or a private fallback.
Replace the neutral pair above with the physical host's actual staging identity,
not the SSH alias. Both nonsecret process values are mandatory in every update
mode and in bootstrap check/apply. The host must be one exact lowercase ASCII
DNS hostname (no trailing dot); the origin must be exactly `https://<host>:10000`.
Padding, wildcards, ports in the host, userinfo, paths, queries, fragments and
control characters are refused before Git, temporary allocation or SSH.

Existing operators must supply their actual existing identity and local alias.
There is no automatic host rename or protected-configuration migration. An
existing protected environment must match the selected pair; it is never adopted
as an implicit default or rewritten to match a new selection. No production
configuration or secret needs to be read, exported locally or passed as an argument.

Omit `--reset-and-load` to deploy exact clean `HEAD` without replacing staging
data. Use `--candidate` to deploy the single valid unaccepted Cassidy Candidate
based on that same clean `HEAD`, then run ordinary update to restore accepted
code. Either flag may precede or follow the target; Candidate plus reset is
always prohibited. Dirty/untracked sources and missing, ambiguous, malformed,
stale or unrelated Candidate refs refuse before archiving or connecting.

The validated alias selects every allocation, transfer, helper verification,
interactive sudo session and cleanup. The exact revision's archive supplies the
remote helper. Existing private archives, validated owned paths, regular-file/
symlink checks and cleanup remain in force; failures remain failures and cleanup
diagnostics report possible residue, without automatic retry. Alias spelling
never selects production versus staging. Only the captured nonsecret pair is
added as literal arguments to the same verified remote helper and sole sudo
session. The helper validates it independently and passes it explicitly to each
bootstrap check/apply/recheck; ordinary sudo environment preservation is not
required. The same bootstrap-confirmed pair selects the route comparison, bounded
HTTPS health request and labeled `Staging: <origin>/` browser destination. Success
still requires successful remote work and reports the exact commit. The physical
host must actually own that identity: mismatched observed routes fail rather
than silently substituting another hostname. Production Nginx and certificate
bootstrap remain separately compiled consumers; this is not generic deployment
or arbitrary-topology support.

The staging bootstrap creates the service identity, database role and database,
release, shared static, and persistent pip-cache directories, systemd unit, and, when absent,
`/etc/muriel-staging/environment`. It generates that file's staging-only Django
secret without printing it and installs it as `root:muriel_staging` mode `0640`.
The first synthetic load creates the fixed `staging-synthetic-audit` account for
generated audit history; it has an unusable password and is not a human login.
Each subsequent synthetic replacement deletes all finding acknowledgements,
including retired and deliberately removed history, with the staging scheduling
domain; it preserves the staging audit account and other login records.
Generation completes Precepting and Late Doc through the current month, retains
ordinary source and assignment history without publications, and leaves later
attention-visible Precepting and Late Doc work open.

For direct privileged bootstrap on the host, select the same nonsecret pair in
that shell, then deliberately pass only those two values through sudo:

```sh
export PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test
export PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000
sudo env PRECEPTOR_STAGING_ALLOWED_HOST="$PRECEPTOR_STAGING_ALLOWED_HOST" \
  PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --instance staging --check
# Use --apply instead of --check to install/reconcile staging prerequisites.
```

`ops/bootstrap-staging-ubuntu --check|--apply` has the same input requirement.
Do not use `sudo -E` or pass secrets. First installation does not require a
preexisting staging environment to obtain its intended identity.

Manual environment management is an advanced option. Start with
`ops/staging-environment.example`, replace its neutral host/origin pair with the
selected identity, retain the fixed database and role, and install it as
`root:muriel_staging` mode `0640` before the update command. Before mutating host prerequisites, bootstrap refuses an existing
file with divergent values, unsafe ownership or permissions, a nonregular file,
or a symlink anywhere along its managed path. It does not repair the file or
source it as root. A valid existing file allows prerequisite reconciliation but
is never rewritten and its secret is never replaced; do not copy the production
environment file or its secret. A safely absent file permits first installation:
the service identity and directories are created before the private environment
is generated. Installation rechecks the file; this preflight is not an atomic
whole-host validation or rollback guarantee. Check/dry-run remains read-only.

Staging binds Gunicorn only to `127.0.0.1:8082`. Its browser route is the
authenticated Tailscale Serve HTTPS listener on port `10000`; do not use
Tailscale Funnel. Unlike production's Unix-only forwarding trust, staging
retains Gunicorn's default loopback forwarded-IP trust and Django's
`X-Forwarded-Proto: https` handling for this local Serve hop. The shared launcher
applies production's empty forwarded-IP allowlist only to production settings;
it must not disable staging's legitimate HTTPS forwarding.
The site uses separate `muriel_staging_sessionid` and
`muriel_staging_csrftoken` cookies, keeps collected assets in
`/srv/muriel-staging/shared/static`, displays `STAGING — SYNTHETIC DATA` on
application and Django Admin pages, and disables all outbound mail.

The update runs only `tailscale serve --bg --https=10000 http://127.0.0.1:8082`,
then starts a separate status command to confirm the persistent private route.
Success requires both the exact TCP `10000` listener (`HTTPS` boolean true) and
the sole `/` Web proxy to `http://127.0.0.1:8082`, without extra listener or
handler fields. Staging Funnel permission must be absent or boolean false both
before and after mutation; numeric or string equivalents are refused.

Only TCP `10000` and the selected staging host's Web/AllowFunnel `:10000` entries
are excluded from before/after preservation comparison. Every other key, value
and nested type must remain exactly as observed, including production Funnel
and Admin mappings. This is not global production commissioning. Invalid JSON,
structure, acquisition or convergence fails without retries or rollback;
invalid preflight state prevents Serve mutation, and invalid post-state prevents
health verification and deployment success. Owned temporary files are cleaned
on failure as on success. An interrupted or disconnected privileged session is
an update failure, not a successful deployment.

After deployment support is installed, inspect staging status and logs from your
local checkout using an explicitly configured private OpenSSH alias. Follow the
[staging status setup](deployment.md#staging-service-status) and
[log operator transition](deployment.md#staging-service-logs)
first, then select that alias locally:

```sh
MURIEL_SSH_TARGET=staging-a ops/status-staging staging-a
MURIEL_SSH_TARGET=staging-a ops/logs-staging staging-a
```

The sole argument must exactly match `MURIEL_SSH_TARGET`; missing, invalid, or
mismatched selection refuses before SSH, with no original-installation fallback.
The noninteractive commands use no sudo or TTY: status reads only
`muriel-staging.service`, and logs runs only
`journalctl --no-pager -u muriel-staging -n 100`, without live following. Both
preserve output and remote/SSH failures; credentials, host trust, and read
permissions must already allow noninteractive access. Even when production and
staging share an SSH alias, the wrapper selects only staging, regardless of
alias spelling. Configured transport does not convert server-runtime identity
to configuration or make any live changes.

Create a human staging administrator only when it is needed, through the
isolated staging release and environment. Configure the private OpenSSH alias
first as above, then explicitly select it locally:

```sh
MURIEL_SSH_TARGET=staging-a ops/create-staging-superuser staging-a
```

The sole target argument must exactly match the configured ASCII alias; missing,
invalid, or mismatched selection refuses before SSH, without a private fallback.
This operator mode retains an interactive TTY and invokes only
`sudo /srv/muriel-staging/create-superuser`, preserving output and failures.
Credentials are entered through the interactive prompts, not command arguments.
See [staging account administration](deployment.md#staging-account-administration)
for setup and the existing-operator transition.

On the host, the separately installed command remains standalone:

```sh
sudo /srv/muriel-staging/create-superuser
```

No-target local mode and the isolated-fixture `--root DIRECTORY` mode need
neither SSH target configuration nor the repository SSH guard. They retain root,
release-containment, environment, and staging database/role checks and run Django
with staging settings as `muriel_staging`. Alias spelling does not choose the
application environment; these safeguards and the fixed remote command do.
Deployment/update commands also require configured targets; server-runtime
identity remains unchanged.

## Performance probes

`scripts/probe-staging-performance` is an opt-in, read-mostly HTTP probe for
one explicitly configured staging origin. It requires both nonsecret local
process environment values `PRECEPTOR_STAGING_ALLOWED_HOST` and
`PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN`, matching the staging installation's
configured hostname and exact `https://<that-host>:10000` origin. There is no
default or original-installation fallback, SSH alias/DNS discovery, or arbitrary
`--url`/`--target` override. Missing, malformed, or mismatched configuration fails
before password prompting or HTTP requests. `--help` needs no configuration.

The host must be one exact lowercase hostname, without a scheme, port, userinfo,
wildcards, or whitespace/control characters. The origin must have no trailing
slash, path, query, fragment, padding, or different scheme/port. Supply only
these two nonsecret identity values locally; do not copy, read, or source the
protected server environment file or bring server secrets to the local process.
The probe verifies the persistent `STAGING — SYNTHETIC DATA` marker before
sending credentials and prompts for the staging password without putting it in
arguments, files, environment, or output. It must never be added to a deployment
hook or Check/test command.

The probe captures that one HTTPS origin for its lifetime. Every outgoing URL
and automatic redirect must keep the same HTTPS scheme, host, and port;
cross-origin destinations, userinfo, and malformed authorities fail before a
request or its cookies, credentials, or probe headers can be sent there. Relative
and same-origin redirects retain urllib's normal bounded redirect handling.
Login and Schedule Bot submissions do not automatically follow redirects, so
their original response checks and marked-proposal cleanup remain in control.
An origin refusal is a probe failure, not a successful timing sample. The
validated pair is captured before prompting and is not reread during login,
measurement, or cleanup. Use the same pair as staging bootstrap/update. The probe
itself does not migrate the server runtime identity; production Nginx and
certificate bootstrap remain separately compiled consumers.

Run it after deploying the revision being measured, using an ordinary staging
scheduler account. Replace this neutral example pair with the nonsecret identity
of that staging installation (an SSH alias is not its DNS hostname):

```sh
export PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test
export PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000
scripts/probe-staging-performance --username STAGING_USERNAME \
  --anchor-month 2026-08 --solver-start-month 2026-09
```

The wrapper uses the release's local `.venv` and also works from an archived
release without Git metadata. Development verification, including from disposable
worktrees, instead uses the main repository's shared virtualenv.

The JSON evidence records wall time, HTTP status, response bytes, and the
request's PostgreSQL query count. Query counts are emitted only for the
probe's marked requests by staging middleware; SQL text and query logging stay
disabled. The fixed workload covers current and five-years-prior Master
Schedule and Precepting calendar/list views, Work Remaining, a selected
Physician detail and history, and each custom schedule-report family spanning
the five years. `--anchor-month` selects the current side of that span. Use the
current completion month printed by the staging synthetic reset as the anchor,
and its following month as `--solver-start-month`. The supported Schedule Bot
probes run over one and three calendar months, starting from the same open
future range. Schedule Bot accepts solve ranges of at most three calendar
months; its balance window may extend to twenty-four calendar months. These
probes use the one-year balance span so the benchmark does not shrink historical
workload context for shorter solves. Their response includes separate setup,
solve, and persistence times.

The in-process regression for the fixed September 2026 one- and three-month
solves is tagged `synthetic-solver-integration` because it runs the real solver
over the complete synthetic scenario. Ordinary `scripts/check` excludes that
long-running evidence. Run it explicitly from a development checkout with:

```sh
DJANGO_SETTINGS_MODULE=preceptor_project.test_settings .venv/bin/python manage.py test \
  schedules.tests.test_staging_performance_probe.StagingPerformanceProposalCleanupTests.test_one_and_three_month_probes_solve_real_scenario_and_clean_up \
  --tag=synthetic-solver-integration
```

The regression creates and resets only its Django test database, retains the
fixed one-year balance context for both supported ranges, and verifies proposal
cleanup.

If a Schedule Bot request returns its setup page instead of a proposal, the
probe prints the server's readiness feedback, including field validation or
named preflight blockers. That response is a failed solve, not a timing sample.

Each Schedule Bot request creates one explicitly marked, unapplied proposal,
then deletes that proposal through its staging-only, CSRF-protected cleanup
route before reporting success. A cleanup failure makes the command fail and
prints the proposal identifier that requires operator cleanup. The probe never
applies a proposal or writes production.
