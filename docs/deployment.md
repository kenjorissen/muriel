# Deployment and service operation

## Routine production update

Update the one configured production host from a reviewed, committed checkout.
First configure a neutral alias in your private OpenSSH configuration using the
[setup below](#production-service-status), with your installation's actual
hostname, login, credentials, and options. Then explicitly select it locally:

```sh
export MURIEL_SSH_TARGET=production-a
export PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test
export PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=edge.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://edge.example.test
ops/update-production "$MURIEL_SSH_TARGET"
```

`ops/update-production TARGET` requires `MURIEL_SSH_TARGET` and exactly one
argument matching it. Aliases start with an ASCII letter or digit and contain
only ASCII letters, digits, dots, underscores or hyphens. Missing/invalid
settings, wrong argument counts, and mismatches refuse before archive creation,
transfer-state allocation, SSH, SCP, or sudo. Values are not trimmed or
interpreted; user@host, URI and option forms are not aliases. There is no inferred
login, discovery, or original-target fallback.

Existing operators must configure their local alias and SSH identity before
replacing the old fixed-target production-update invocation with the command
above. The alias can address the current commissioned installation without
renaming it or changing its server identity. This is a local configuration
transition, not a live migration. Both host/origin pairs above are mandatory:
use two distinct actual lowercase ASCII DNS hostnames without trailing dots,
each with exactly `https://<host>`. Missing, padded, unsafe or inconsistent values
refuse before Git, temporary allocation or transport. Existing operators must
supply both pairs already maintained in protected `/etc/muriel/environment`;
bootstrap refuses disagreement rather than adopting or rewriting runtime values.
All four values are captured once and passed as literal additional arguments to
the same verified helper and sole sudo session, without sudo environment
preservation. The helper validates using its exact archive's identity guard,
without a release/venv dependency, and explicitly passes both pairs to every
bootstrap check/apply/recheck. Bootstrap renders primary Funnel/Admin names,
Admin's fixed Host authority and independent Cloudflare name; the primary also
selects certificate issuance/validation. An existing wrong-host certificate is
preserved and refused, never silently rotated: see [deliberate renewal](public-edge.md#admin-certificate-renewal).
Selecting an SSH alias or identity does not change DNS or Tailscale routes.

This is the routine production-update command. It refuses a dirty checkout,
archives one exact `HEAD` revision, and uses one interactive remote sudo session
to check and, when needed, reconcile only the bootstrap's fixed Muriel scope.
Production bootstrap refuses an unsafe existing `/etc/muriel/environment` before
mutating prerequisites, including installing packages; it never repairs or
rewrites that file. It sources safe configuration only as `muriel`, clearing
all four inherited identity fields, and requires agreement with both selected
pairs. Bootstrap permits an absent environment for first-install
setup, but deployment and later verification still require it and retain their
own checks. A bootstrap failure stops deployment and reload, retaining owned
artifact cleanup; see the [bootstrap file-safety boundary](host-bootstrap.md).
It then invokes the installed `deploy-release` helper with that same archive,
validates and reloads Nginx, enables the verified backup timer, checks Muriel,
Nginx, PostgreSQL `18/main`, the local public edge, and the exact Funnel and
private Admin Serve mappings. It also permits the exact tailnet-only staging
Serve route on port `10000` when present. It does not create or alter Tailscale
routes. Route verification uses `PRECEPTOR_PRODUCTION_ALLOWED_HOST` and its
matching exact `https://<host>` `PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN` from
fixed `/etc/muriel/environment`, as does the primary local-edge check. The file
must be readable, regular, non-symlink, and `root:muriel` mode `0640`; it is
sourced only as `muriel`, with inherited host/origin settings cleared. Missing,
unsafe, or inconsistent identity fails without an original-host or SSH-alias
fallback. Configure the actual installation identity in that protected file,
and supply matching nonsecret local selections for bootstrap; local inputs do
not override protected runtime state.

After local-edge success, the privileged helper captures only `tailscale serve
status --json`. An unprivileged comparator reads that capture over stdin without
opening access to the private staging directory. It requires the complete exact
route keys, values and types: public TLS on 443 for the configured primary host,
PROXY protocol integer 2 to `127.0.0.1:8080`, private Admin HTTPS on 8443 to
`https+insecure://127.0.0.1:8081`, and optionally private staging HTTPS on 10000
for the same host to `http://127.0.0.1:8082`. Only primary-host port 443 may have
Funnel permission. Partial staging, foreign hosts, extras and malformed JSON
fail verification and retain owned cleanup. Route verification itself does not
render Nginx, provision certificates or configure staging identity. Bootstrap
renders both identities earlier; Cloudflare control-plane setup remains
an explicit operator action.

After edge and complete route verification succeeds, the remote helper reports
three labeled browser destinations from the validated identity captured by that
route comparison: preferred public Cloudflare `https://<secondary-host>/`,
temporary public Funnel fallback `https://<primary-host>/`, and private Admin
`https://<primary-host>:8443/admin/`. The secondary pair is
`PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST` and
`PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN` in the same protected file.
These are not values inferred from the SSH alias or inherited local settings,
and no configuration is reloaded solely for display. Existing SSH stdout carries
the URLs; the local final message names the exact deployed commit only after
successful remote completion. Verification failures report neither these
success destinations nor successful deployment. Verified URLs may appear before
EXIT cleanup finishes. A phase failure names its existing lower-level diagnostic
command.

The same validated alias is used for allocation, transfer, verification,
privileged invocation, and cleanup. The private local archive excludes development
and secret material; the remote helper is extracted and verified from that same
revision's archive, not from another checkout. The configured user needs sudo
permission for the privileged update. Success discharges the remote archive and
helper; local failure or signal handling attempts cleanup only of owned local
and validated remote paths and reports possible residue. Transfer, privilege,
remote-phase, and cleanup failures remain failures, without automatic retries.

For diagnosis or deliberate recovery, the lower-level commands remain available.
For deploy, status, and logs, first configure the explicit SSH alias as described under
[Production service status](#production-service-status):

```sh
sudo env PRECEPTOR_PRODUCTION_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN" \
  PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --check
ops/deploy "$MURIEL_SSH_TARGET"
ops/status "$MURIEL_SSH_TARGET"
ops/logs "$MURIEL_SSH_TARGET"
```

The accepted single-host production runtime is Ubuntu 26.04 LTS with its system
Python 3.14 and PostgreSQL 18 cluster. The release helper creates its virtual
environment with `python3.14`.

### Direct release deployment

`ops/deploy TARGET` requires local `MURIEL_SSH_TARGET` and exactly one argument
matching it. Use the ASCII alias syntax and private OpenSSH setup described under
[Production service status](#production-service-status); OpenSSH owns the login,
hostname, credentials, and options. For example, after configuring your alias:

```sh
MURIEL_SSH_TARGET=production-a ops/deploy production-a
# Or select another explicitly configured installation:
MURIEL_SSH_TARGET=production-b ops/deploy production-b
```

Missing/invalid settings, wrong argument counts, and mismatches refuse before
Git archiving, temporary-file allocation, SSH, SCP, or sudo. Values are not
trimmed or interpreted; there is no implicit login or private-target fallback.
Existing operators must configure their local alias and SSH identity before
adopting this command. This local transition performs no deployment, host rename,
or remote migration by itself.

The lower-level deployment command refuses a dirty checkout and every untracked
file. It archives the exact `HEAD` commit locally, excluding development
databases, virtual environments, secrets, and Cassidy attempt material. It
transfers the private mode-0600 archive to a temporary owner-only file on the
configured host, using the same validated alias for allocation, SCP, activation,
and cleanup. An interactive SSH TTY permits the host's sudo prompt while the
remote shell redirects the uploaded archive to the single installed
`/srv/muriel/deploy-release` invocation. The local SSH stdin remains available
for terminal input, not archive streaming. The configured user needs permission
to run that sudo operation.

Both owned temporary archives are cleaned on success and failure. An unsafe
remote temporary path is rejected without acting on it. Stage, transfer,
activation, and cleanup failures remain failures; cleanup diagnostics identify
possible residue without hiding a primary failure. Deployment never uses a
remote Git host or prints production values.

Routine production update and [staging update](#synthetic-staging) also use
explicit aliases. Server-domain configuration remains separate work; configured
transports preserve the existing fixed service/path topology and public/Admin
routes.

The host bootstrap installs `/srv/muriel/deploy-release`,
`/srv/muriel/backup-database`, `/srv/muriel/restore-database`,
`/srv/muriel/create-superuser`, `/srv/muriel/reset-production-domain`, and
`/srv/muriel/clear-production-login-access`. Before the first deployment,
create `/etc/muriel/environment` as `root:muriel`, mode `0640`. It must contain
`PRECEPTOR_PRODUCTION_SECRET_KEY`,
`PRECEPTOR_PRODUCTION_ALLOWED_HOST`,
`PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN`, `PRECEPTOR_POSTGRES_NAME=muriel`,
and `PRECEPTOR_POSTGRES_USER=muriel`; the host and origin must be the exact
public HTTPS hostname and origin. A second exact public route may be commissioned
with the paired `PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST` and
`PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN` values. Configure both or
neither; wildcards and mismatched origins are rejected. Configure off-host
backup routing before an update because every deployment requires a verified
pre-deploy backup. The
[password recovery runbook](password-recovery.md) defines the optional mail
settings. The host helper stages a commit-specific directory, creates its
virtual environment from `requirements.txt`, reusing downloaded packages from
the service-owned `/srv/muriel/shared/pip-cache`, runs Django deployment and
model-change checks, verifies a pre-deploy PostgreSQL backup, migrates,
collects static files, and runs the production readiness check. Only then does it atomically replace
`/srv/muriel/current` and restart `muriel.service`.

Failures before activation leave the selected release unchanged. Once migration
begins, do not automatically point `current` at old code: a migration can be
irreversible. Inspect the service, fix forward when possible, or restore the
reported pre-deploy backup through the recovery runbook before selecting another
release. Successful deploys keep the three most recently staged releases. The
[backup and recovery runbook](backup-recovery.md) defines the deliberate restore
and scratch-drill procedures.

## Production service status

`ops/status TARGET` and `ops/logs TARGET` require the local process setting
`MURIEL_SSH_TARGET`, and exactly one argument equal to that setting. The argument
alone does not select or authorize a destination. Configure your connection identity in your
private OpenSSH configuration (normally `~/.ssh/config`), for example:

```sshconfig
Host production-a
    HostName app.example.org
    User operator

Host production-b
    HostName app.example.net
    User service-reader
```

Replace the neutral hostnames and users with your installation's actual values;
OpenSSH owns credentials and connection options. Select the one permitted alias
explicitly for each local process, for example:

```sh
MURIEL_SSH_TARGET=production-a ops/status production-a
MURIEL_SSH_TARGET=production-a ops/logs production-a
# Or select another explicitly configured installation:
MURIEL_SSH_TARGET=production-b ops/status production-b
MURIEL_SSH_TARGET=production-b ops/logs production-b

# For repeated inspection of one installation:
export MURIEL_SSH_TARGET=production-a
ops/status "$MURIEL_SSH_TARGET"
ops/logs "$MURIEL_SSH_TARGET"
```

An alias must start with an ASCII letter or digit and contain only ASCII letters,
digits, dots, underscores or hyphens. Empty settings, whitespace, option forms,
wildcards, shell syntax, `user@host`, and SSH URIs are rejected, not trimmed or
interpreted. Missing/invalid settings, argument-count errors, and mismatches
refuse locally before SSH. There is no original-installation fallback.

Status and logs use noninteractive OpenSSH batch mode without a TTY or sudo.
Status runs only `systemctl status --no-pager muriel.service`. Logs runs only
`journalctl --no-pager -u muriel -n 100`: the production journal's last 100 lines,
without a pager or live following. Neither command accepts an arbitrary unit or
remote command. Authentication, host trust, and journal read permissions must
already be configured for noninteractive access. Command output and the
remote/SSH exit status are preserved, including nonzero status for an inactive
service, journal access failure, or connection failure.

Existing operators must configure their alias and SSH identity before using these
revised commands; replace the old fixed-target production-log invocation with
`ops/logs "$MURIEL_SSH_TARGET"` after selecting the configured alias. This is not
an automatic host rename or remote migration and does not change the service
name, server configuration, or public/Admin routes.
Production `ops/status`, `ops/logs`, and `ops/restart`, plus staging
`ops/status-staging`, `ops/logs-staging`, and operator
`ops/create-staging-superuser`, direct `ops/deploy`, and routine
`ops/update-production` and `ops/update-staging`, consume `MURIEL_SSH_TARGET`.

## Production service restart

`ops/restart TARGET` requires the same explicit local `MURIEL_SSH_TARGET`
setting, supported alias syntax, and exactly matching sole argument described
[above](#production-service-status). Missing/invalid settings, wrong argument
counts, and mismatches refuse before SSH; there is no original-installation
fallback. Existing operators must configure their local alias and ordinary
OpenSSH identity before invoking the revised helper, for example:

```sh
MURIEL_SSH_TARGET=production-a ops/restart production-a
# Or, after explicitly selecting the installation in this shell:
export MURIEL_SSH_TARGET=production-a
ops/restart "$MURIEL_SSH_TARGET"
```

Unlike read-only status/logs, restart deliberately opens an interactive SSH TTY
so the operator can answer the remote sudo password prompt. The configured SSH
user must have permission to run the required sudo operations. It runs only
`sudo systemctl restart muriel`, then `sudo systemctl is-active --quiet muriel`
if restart succeeds. Restart, active-check, and SSH failures remain failures;
operation output is preserved for diagnosis, without automatic retries or
recovery. Changing this local helper does not restart a running host, migrate
other helpers, or change server identity or public/Admin routes.

`muriel.service` runs foreground Gunicorn as the locked-down `muriel` identity.
systemd owns startup at boot, the working directory, `/etc/muriel/environment`,
the runtime socket directory, SIGTERM graceful shutdown, restart policy,
resource limit, and journald output.

The public Tailscale Funnel edge, its post-deploy probes, incident shutdown,
and private Tailscale Serve Django Admin path are defined in the
[public edge runbook](public-edge.md). Deploying a release does not enable
Funnel.

## Synthetic staging

The isolated synthetic staging instance has separate release, static, service,
and PostgreSQL state. It never updates production paths or its public/Admin
Tailscale routes. `update-staging` creates the fixed staging environment file
with a generated secret when it is absent. First configure a private OpenSSH
alias with the installation's actual hostname, user, credentials and connection
options as in [staging status setup](#staging-service-status). Explicitly select
that alias and the nonsecret staging host/origin pair locally before invoking
any update mode. Replace the neutral pair below with the physical host's actual
identity, independently of alias spelling:

```sh
export MURIEL_SSH_TARGET=staging-a
export PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test
export PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000

# Deploy the current clean commit only.
ops/update-staging "$MURIEL_SSH_TARGET"

# Deploy, then replace only staging scheduling records with synthetic data.
ops/update-staging "$MURIEL_SSH_TARGET" --reset-and-load

# Deploy the one live, unaccepted Cassidy Candidate without changing staging data.
ops/update-staging "$MURIEL_SSH_TARGET" --candidate

# Restore accepted code after inspection; staging data remains intact.
ops/update-staging "$MURIEL_SSH_TARGET"

# Read-only staging diagnosis.
ops/status-staging "$MURIEL_SSH_TARGET"
ops/logs-staging "$MURIEL_SSH_TARGET"
# Direct bootstrap diagnosis runs ON THE HOST, with the pair selected there:
sudo env PRECEPTOR_STAGING_ALLOWED_HOST="$PRECEPTOR_STAGING_ALLOWED_HOST" \
  PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --instance staging --check

# Create a human login against only the active staging release.
ops/create-staging-superuser "$MURIEL_SSH_TARGET"
```

`ops/update-staging TARGET [--candidate] [--reset-and-load]` requires local
`MURIEL_SSH_TARGET` and exactly one target equal to it. An alias starts with an
ASCII letter or digit and contains only ASCII letters, digits, dots, underscores
or hyphens. Flags may appear before or after the target; duplicate/unknown
flags, duplicate targets, and Candidate plus reset are refused. Missing, invalid
or mismatched selection and invalid invocations refuse before archiving,
transfer-resource allocation, SSH, SCP or sudo. Values are not trimmed or
interpreted; user@host and URI forms are not aliases. There is no inferred
login, discovery or private fallback.

Both `PRECEPTOR_STAGING_ALLOWED_HOST` and
`PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN` are mandatory nonsecret process input.
The host must be one exact lowercase ASCII DNS hostname without a trailing dot;
the origin must be exactly `https://<host>:10000`, without padding, userinfo,
paths, queries or fragments. Invalid or inconsistent identity refuses locally
before any Git/archive/transfer work and independently at remote/bootstrap entry
before host mutation. Only this pair is added to the verified helper's literal
arguments; no secret is exported locally or passed on the command line. Bootstrap
receives the pair explicitly, not through assumed sudo environment preservation.

Existing operators must configure private SSH identity, the explicit alias and
the actual existing staging host/origin before using these commands. This does
not automatically rename a host or migrate configuration. The protected staging
file remains the application's authority: bootstrap refuses a mismatched or
unsafe existing file before prerequisite mutation and never rewrites its secret
or identity. First installation generates the protected file after creating the
service identity and directory, without accessing production configuration.
See [first-install and direct privileged bootstrap setup](staging.md).

The fixed helper, not alias spelling, selects staging even on a shared host.
The same selected pair reaches route verification, the bounded HTTPS health
request and the labeled staging browser URL; success retains the exact commit.
The physical host must have that identity: mismatched observed routes are errors.
Production Nginx identity and certificate bootstrap use separate explicit
production inputs, not this staging workflow.

Route convergence creates or reuses the complete private staging TCP/Web route
on port `10000`, then independently inspects its persisted state. Only TCP
`10000` and the selected staging host's Web/AllowFunnel `:10000` entries are owned;
all other observed mappings and state must remain exactly unchanged, including
nested value types. Staging Funnel permission must be absent or boolean false
before and after mutation. Malformed snapshots, acquisition errors, incomplete
routes or isolation failures prevent success, without retries or rollback. See
[synthetic staging route verification](staging.md) for the exact boundary.

### Staging service status

`ops/status-staging TARGET` requires the explicit local `MURIEL_SSH_TARGET`
setting and exactly one matching argument, using the same alias syntax and
private OpenSSH setup described under [Production service status](#production-service-status).
For example, configure a neutral alias in your private OpenSSH configuration:

```sshconfig
Host staging-a
    HostName app.example.org
    User operator
```

Replace these example values with your installation's connection identity, then
select the alias explicitly in the local process:

```sh
MURIEL_SSH_TARGET=staging-a ops/status-staging staging-a
# Or, for repeated inspection:
export MURIEL_SSH_TARGET=staging-a
ops/status-staging "$MURIEL_SSH_TARGET"
```

Missing/invalid settings, wrong argument counts, and mismatches refuse before
SSH, without trimming, interpreting shell syntax, or an original-installation
fallback. The command uses noninteractive OpenSSH batch mode without a TTY or
sudo, running only `systemctl status --no-pager muriel-staging.service`.
Credentials, host trust, and status read permissions must already permit
noninteractive access. Output and remote/SSH exit status are preserved,
including inactive-service and connection failures.

One installation may host both production and staging: the wrapper's fixed
operation selects the service, not the alias spelling. This command never
queries the production unit or mutates either service. Existing operators must
configure their SSH identity and explicitly select an alias before replacing
the old fixed-target staging-status invocation with
`ops/status-staging "$MURIEL_SSH_TARGET"`.
This is a local operator transition, not a host rename, remote migration, or
installation-wide portability claim. Staging and production updates, direct
deployment, and account administration also use configured targets.

### Staging service logs

`ops/logs-staging TARGET` requires the same explicit local `MURIEL_SSH_TARGET`,
supported ASCII alias syntax, private OpenSSH setup, and exactly matching sole
argument as [staging status](#staging-service-status). For example, after
configuring your alias:

```sh
MURIEL_SSH_TARGET=staging-a ops/logs-staging staging-a
# Or, after explicitly selecting the installation in this shell:
export MURIEL_SSH_TARGET=staging-a
ops/logs-staging "$MURIEL_SSH_TARGET"
```

Missing/invalid settings, wrong argument counts, and mismatches refuse locally
before SSH, without trimming, interpretation, or an original-installation
fallback. OpenSSH owns the username, hostname, credentials, and options. The
command uses noninteractive batch mode without sudo or a TTY, running only
`journalctl --no-pager -u muriel-staging -n 100`: the staging journal's last 100
lines, without a pager or live following. Credentials, host trust, and journal
read permissions must already allow noninteractive access. Output and
journal/SSH failure status are preserved.

The fixed wrapper operation selects staging, regardless of alias spelling; it
cannot read the production journal, accept another unit or command, or mutate
either service. Existing operators must configure their SSH identity and alias
before replacing the old fixed-target staging-log invocation with
`ops/logs-staging "$MURIEL_SSH_TARGET"`. This local transition makes no live
changes, renames no host, and does not convert update helpers or server-runtime
identity.

### Staging account administration

Configure a private OpenSSH alias as in [staging status](#staging-service-status),
with your installation's hostname, username, credentials, and connection options.
Then explicitly select that alias for the operator command:

```sh
MURIEL_SSH_TARGET=staging-a ops/create-staging-superuser staging-a
# Or, after selecting the installation in this shell:
export MURIEL_SSH_TARGET=staging-a
ops/create-staging-superuser "$MURIEL_SSH_TARGET"
```

`TARGET` must be the sole argument and exactly equal local `MURIEL_SSH_TARGET`.
The same ASCII alias syntax applies: start with a letter or digit, followed only
by letters, digits, dots, underscores or hyphens. Missing/invalid configuration,
mismatches, or extra arguments refuse before SSH. There is no trimming,
interpretation, inferred login, or original-installation fallback.

Unlike read-only status/logs, this command retains an interactive SSH TTY for
sudo and Django's account prompts. It runs only
`sudo /srv/muriel-staging/create-superuser`; the configured SSH user needs that
sudo permission. Output and remote/SSH failures propagate. It accepts no account
credentials or arbitrary commands as arguments. The fixed helper and validated
host-local state select staging, even if the same physical-host alias is also
used for production operations.

The bootstrap installs a standalone host-local copy. On the staging host, its
existing usage remains:

```sh
sudo /srv/muriel-staging/create-superuser
```

This no-target mode requires neither `MURIEL_SSH_TARGET` nor a sibling
`require-ssh-target.sh`. The existing `--root DIRECTORY` mode supports isolated
host-root fixtures, not remote destination selection. Local execution still
requires root and validates the staging installation, contained current release,
Python/manage.py, protected environment, and exact staging database and role
before running Django with staging settings as `muriel_staging`.

Existing operators must configure their private SSH identity and explicit alias
before replacing the old fixed-target staging-account invocation with the
configured command above. This local transition performs no live
account creation, host rename, or remote migration. Server-runtime identity
configuration remains separate work.

### Staging update behavior

`update-staging` refuses dirty or untracked source and archives exact clean
`HEAD` in ordinary mode, retaining development/secret exclusions and private
archive permissions. Every SSH allocation, SCP transfer, helper extraction,
interactive sudo session and cleanup uses the same validated alias. The remote
helper comes from that exact archive, with owned-path validation and regular-file/
symlink guards. Existing owners remove the local archive and remote archive/helper
on success or failure; primary failures and signal/disconnection outcomes remain
failures, cleanup diagnostics report possible residue, and there is no automatic
retry or recovery. A failed session never reports deployment success.

`update-staging` opens one remote sudo session. Its remote helper converges
only the staging bootstrap and `/srv/muriel-staging` release, runs migrations,
reuses downloaded packages from `/srv/muriel-staging/shared/pip-cache`, collects
static assets, and performs the staging readiness check before it
atomically selects the release and restarts `muriel-staging.service`. A
pre-activation failure leaves the previous staging release selected.

After activation it converges `tailscale serve --https=10000` to
`http://127.0.0.1:8082`, rejects any Funnel permission for that route, confirms
that all non-staging Tailscale mappings retain exact keys, values and types, and
probes `<selected-origin>/health/` with the bounded curl timeout and no redirects.
It never uses `tailscale serve reset` or changes production's Funnel and Admin mappings. On a failure,
the command prints the lower-level staging diagnosis command; use the status
and logs commands above for service failures.

Candidate mode accepts `--candidate` before or after the configured target. It reads the
one live `refs/cassidy/attempts/<attempt-id>/candidate` commit only when it is
unaccepted work based on the current clean `HEAD`, prominently identifies that
commit, and uses the ordinary staging release path. It rejects ambiguous,
malformed, stale, or unrelated Candidate refs before opening a remote session.
Candidate mode always preserves retained staging data and rejects
`--reset-and-load`; run the ordinary update command above to restore accepted
code after inspection.

The synthetic loader creates or repairs the fixed `staging-synthetic-audit`
account for generated mutation history. It has an unusable password and cannot
authenticate. Human staging logins are created separately with
`create-staging-superuser`, which validates the active staging release and
staging environment before running Django's interactive command as
`muriel_staging`.
