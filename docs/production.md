# Production process

This runbook defines the application-side contract for a conventional systemd
and reverse-proxy deployment. Prepare the host first with the
[Ubuntu host bootstrap](host-bootstrap.md), then use the
[deployment runbook](deployment.md).

## Runtime installation

The accepted single-host runtime is Ubuntu 26.04 LTS with its system Python 3.14
and PostgreSQL 18 cluster. The routine `ops/update-production TARGET`
command owns release installation; it invokes `/srv/muriel/deploy-release`,
which creates a fresh release virtual environment with Python 3.14 and installs
only the pinned runtime dependencies. Do not install `requirements-dev.txt` on
the production host or edit `/srv/muriel/current` directly.

Configure your private OpenSSH alias and explicitly select it locally as in the
[routine-update runbook](deployment.md#routine-production-update):

```sh
export MURIEL_SSH_TARGET=production-a
export PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test
export PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=edge.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://edge.example.test
ops/update-production "$MURIEL_SSH_TARGET"
```

The sole argument must exactly match the required setting; invalid or missing
selection refuses before local archive or remote work, without a fallback.
Existing operators must configure the alias and explicitly select both actual
host/origin pairs before using this command (the values above are neutral
examples). Each hostname must be lowercase ASCII DNS without a trailing dot;
each origin is exactly `https://<host>`, and the hosts must differ. Both pairs
must agree with the protected environment. Invalid selection refuses before Git,
archive allocation or SSH. All four values travel as literal arguments through
the verified helper's one sudo session, not inherited sudo environment.
Bootstrap renders primary Funnel/Admin server names, Admin's fixed Host on 8443,
and the independent Cloudflare name. The same primary selects certificate
issuance/validation, never inferred from the SSH alias. Existing wrong-host
material is preserved and refused with [operator renewal instructions](public-edge.md#admin-certificate-renewal),
not rotated. For first manual bootstrap and deliberate nonsecret sudo passing,
follow [host setup](host-bootstrap.md).

The active environment file is `/etc/muriel/environment`, owned `root:muriel`
and mode `0640`. Its required settings are
`PRECEPTOR_PRODUCTION_SECRET_KEY`, `PRECEPTOR_PRODUCTION_ALLOWED_HOST`,
`PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN`, `PRECEPTOR_POSTGRES_NAME=muriel`,
and `PRECEPTOR_POSTGRES_USER=muriel`. The production database is always the
PostgreSQL `18/main` Unix socket at `/var/run/postgresql`, port `5432`; optional
`PRECEPTOR_POSTGRES_HOST` and `PRECEPTOR_POSTGRES_PORT` can only repeat those
values. `PRECEPTOR_PRODUCTION_ALLOWED_HOST` is one exact hostname and
`PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN` is exactly its `https://` origin.

The existing dual-route local edge verification, including routine production
updates, additionally requires `PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST`
and `PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN` in this protected file.
The two exact hostnames must be distinct; each origin is exactly `https://<host>`
without a port or trailing slash. This operational requirement does not change
Django's optional-secondary behavior. The update helper validates the fixed
file's regular-file, non-symlink, readability and `root:muriel` mode `0640`
safeguards, then sources it only as `muriel` for the probe. It neither modifies
the file nor prints its secrets; invalid probe configuration stops verification.
Before prerequisite mutation, bootstrap also loads this file only as `muriel`
and requires both pairs to match the local selection, clearing all four inherited
identity values. It never rewrites
the protected file. First manual bootstrap can run without it; the operator must
create matching protected configuration after bootstrap creates the service
account/directory and before deployment. No active release/venv or production
secrets are needed locally for this first bootstrap. Routine update cannot
succeed without valid runtime configuration.
See [local configured verification](public-edge.md#local-configured-verification)
for safe setup and service-user diagnosis. The server's Nginx, certificates and
Tailscale identities must match; configuring the probe does not change them.
`ops/nginx/muriel.conf` and `ops/nginx/muriel-admin-proxy.conf` are source
templates; bootstrap compares/installs rendered bytes, not the raw files. Cloudflare DNS/dashboard/tunnel provisioning remains an
explicit operator action.

For lower-level diagnosis, the release helper is the only supported owner of
the following steps; it runs them under the unprivileged `muriel` identity:

```sh
sudo /srv/muriel/deploy-release COMMIT < release.tar
```

`STATIC_ROOT` is `staticfiles` in the release directory. Production uses
Django's manifest static-file storage, so `collectstatic` writes content-hashed
asset names and the manifest consumed by `{% static %}`. Configure the reverse
proxy to serve that directory directly; Django does not serve static files when
production settings disable `DEBUG`. A missing manifest entry is a production
error rather than a fallback to an unversioned URL.

## Readiness

`deploy-release`, destructive production helpers, and restore drills run this
local, authenticated-only process check themselves. Run it separately only for
diagnosis from the active release:

```sh
sudo -u muriel sh -c 'set -a; . /etc/muriel/environment; set +a; exec /srv/muriel/current/scripts/check-production'
```

It loads production settings, runs Django deployment checks, connects to
PostgreSQL, refuses pending migrations, and verifies manifest-resolved common
CSS, JavaScript, and image assets. It makes no HTTP request and exposes no
application data.

## Service command

Use `scripts/start-production` as the systemd `ExecStart` command. It always
uses `preceptor_project.production_settings` for production (the separate
[staging service](staging.md) explicitly selects its own settings), starts foreground Gunicorn, and
writes access and error output to the standard streams for journald. It refuses
to run as root.

It binds `/run/muriel/gunicorn.sock` with mode `0770` and starts two synchronous
workers with a 60-second request timeout and 30-second graceful shutdown
timeout. Bootstrap adds `www-data` to the `muriel` group, and the systemd unit
creates `/run/muriel` for that group before Gunicorn starts. Nginx is fixed to
this socket, so a commissioned host must not override the socket or socket
umask.

Production explicitly passes `--forwarded-allow-ips ""` to Gunicorn: no TCP
peer, including loopback, may assert HTTPS through Gunicorn's scheme headers
(`X-Forwarded-Proto`, `X-Forwarded-Protocol`, or `X-Forwarded-SSL`). The quoted
empty argument is intentional. Gunicorn's command-line setting takes precedence
over `FORWARDED_ALLOW_IPS` and `GUNICORN_CMD_ARGS`; do not replace it with an
environment default. Unix-socket peers remain trusted by Gunicorn independently
of this IP list. This complements Django's Unix-only forwarded-protocol and
private-Admin marker handling: stripping a header in Django cannot undo a WSGI
scheme already set by Gunicorn. Nginx must still overwrite forwarding headers.
Staging retains its separate loopback forwarding policy for Tailscale Serve;
this production rule does not apply to staging's fixed `127.0.0.1:8082` listener.

## Administrator creation

Create the initial administrator, or a replacement administrator during
recovery, through the fixed host helper:

```sh
sudo /srv/muriel/create-superuser
```

The helper requires sudo, validates the active Muriel release and production
environment, and runs Django's normal interactive `createsuperuser` command as
the unprivileged `muriel` identity against PostgreSQL `18/main`. Enter the
password only at Django's prompt; the helper neither accepts nor records it.

## Replacing scheduling data

`/srv/muriel/reset-production-domain` is the only production operation for
destructively replacing physician and scheduling data. It takes a fresh verified
local and off-host backup, stops Muriel, changes the scheduling domain in one
database transaction, checks readiness, and confirms the service is active.
It never changes Django users, their authority, sessions, invitations, or
account-change records. Run it only under the explicit commissioning direction:

```sh
sudo /srv/muriel/reset-production-domain empty
sudo /srv/muriel/reset-production-domain demo --actor existing-admin
sudo /srv/muriel/reset-production-domain synthetic --actor existing-admin \
  --seed staging-review-2026
```

The replacement deletes every finding acknowledgement, including retired and
deliberately removed acknowledgement history, with the scheduling domain.

An otherwise empty scheduling domain retains the active built-in `admin` option,
named `Admin`, with Not-available impact. Custom schedule options are removed.

`demo` uses an existing active superuser for all audit records. With exactly one
active superuser, `--actor` may be omitted; otherwise the helper refuses before
any mutation and requires an explicit username. It never creates demo logins.
`synthetic` builds the reusable AY 2020-2021 scenario instead of the small demo
seed and retains all login-owned records. It completes Precepting through the
organization-local current month; later
attention-visible Precepting and Late Doc work remains open. Validation uses
actual assignments and ordinary change history, with no publication prerequisite.
Publication storage was removed by Django migration 0059; it is no longer
part of the reset inventory.
The final output reports counts, elapsed time, PostgreSQL database size, direct
historical validation, and the remaining future work.
Before taking the backup, the helper displays the action, host, and database,
then requires the exact one-time phrase shown on the controlling terminal. It
names both `PRODUCTION` and `MURIEL` in that randomized phrase. It does not
accept confirmation from command arguments, standard input, or the environment.

## Clearing login access

`/srv/muriel/clear-production-login-access RECOVERY_USERNAME` is a separate
production operation for revoking ordinary account access while retaining the
specified active superuser as a recovery account. It first verifies that account
has recovery authority, takes a fresh verified local and off-host backup, stops
Muriel, disables every other account, revokes pending invitations, clears active
sessions, and unlinks Physicians with recorded history. It preserves users,
official emails, usernames, passwords, Physician rows, scheduling data, and all
change records so accounts can be reactivated or reinvited later through the UI.

```sh
sudo /srv/muriel/clear-production-login-access existing-admin
```

The helper displays the recovery username and requires the exact one-time phrase
shown on the controlling terminal. It restarts Muriel after any failure once it
has stopped the service, then runs readiness checks and confirms the service is
active before reporting success.

The launcher accepts these bounded tuning values. They are lower-level service
settings, not a substitute for the deployment and Nginx contract above:

```text
PRECEPTOR_GUNICORN_WORKERS=1..4
PRECEPTOR_GUNICORN_TIMEOUT=30..120
PRECEPTOR_GUNICORN_GRACEFUL_TIMEOUT=15..60
```
