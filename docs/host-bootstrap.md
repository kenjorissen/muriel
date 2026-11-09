# Ubuntu host bootstrap

Run the checked repository revision on the intended host. The bootstrap supports
Ubuntu 26.04 LTS only and requires deliberate sudo access. This single-host
runtime uses Ubuntu's system Python 3.14 and PostgreSQL 18 cluster:

```sh
# Neutral examples: select the installation's actual tailnet and Cloudflare names.
export PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test
export PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=edge.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://edge.example.test
sudo env PRECEPTOR_PRODUCTION_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN" \
  PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --check
sudo env PRECEPTOR_PRODUCTION_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN" \
  PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --apply
```

Both modes require both explicit nonsecret process pairs: each is one lowercase
ASCII DNS hostname without a trailing dot and exactly `https://<host>`. Primary
and secondary hostnames must be distinct. Missing, padded,
unsafe or inconsistent values refuse; there is no original-domain fallback or
SSH-alias inference. Pass only these nonsecret settings deliberately through
sudo as above, not the protected environment or `sudo -E`. Existing operators
must select both actual existing identities; this is not a host rename
or automatic configuration migration. Staging dispatch uses its separate inputs.

`ops/nginx/muriel.conf` and `ops/nginx/muriel-admin-proxy.conf` are **source
templates**, not installable Nginx files. Bootstrap substitutes named markers
for primary Funnel (8080) and private Admin (8081) exact server names, Admin's
fixed `<primary-host>:8443` proxy Host, and the independent Cloudflare name (8083).
Nginx runtime variables and guards are preserved. Check compares rendered bytes;
apply atomically installs them at fixed managed destinations. Rendering scratch
is private, invocation-owned and cleaned. The same primary hostname selects
Tailscale certificate issuance and existing-pair validation; this changes no
ports, trusted peers or hosting topology.

`--check` changes nothing and reports missing or divergent prerequisites.
`--apply` installs only `python3.14-venv`, `postgresql-18`,
`postgresql-client-18`, `nginx`, and `openssl`; it temporarily prevents package
installation from auto-starting a service. Tailscale must already be installed
and usable. The bootstrap does not configure Tailscale, Funnel, firewall policy,
SSH, secrets, or demo data.

It explicitly enables, starts, queries, and verifies the `postgresql@18-main`
service and its `18/main` PostgreSQL cluster. That cluster must own the
`/var/run/postgresql` Unix socket at port
`5432`; bootstrap verifies that endpoint identifies as `18/main` and fails
rather than accepting a conflicting cluster. A retained `16/main` cluster stays
down on port `5433` until its migrated state is verified. Django, deployment
readiness, backups, and restores all use the `18/main` socket and port rather
than a default service, socket, or port. It also creates
the `muriel` service identity and host-owned directories below
`/srv/muriel`, including the service-owned persistent pip cache at
`/srv/muriel/shared/pip-cache`, `/etc/muriel`, and `/var/backups/muriel`. It installs
`muriel.service`, `muriel-backup.service`, and `muriel-backup.timer`; the
`/srv/muriel/deploy-release`, `backup-database`, `restore-database`,
`create-superuser`, `clear-production-login-access`, and
`reset-production-domain` helpers; and the Nginx site and proxy snippets at
`/etc/nginx/sites-available/muriel`, `/etc/nginx/snippets/muriel-proxy.conf`,
and `/etc/nginx/snippets/muriel-admin-proxy.conf`. On explicit `--apply`, it
atomically replaces divergent regular files only at those fixed Muriel-managed
destinations with the declared owner and mode. It
refuses symlinked or non-regular managed-file targets. `--check` reports such
divergence without changing it. It validates Nginx configuration but does not
enable Muriel, the backup timer, or reload Nginx; commission the off-host backup
destination and deploy a release first. It removes only the Ubuntu stock
`sites-enabled/default` link when it points to the stock available-site file;
another occupant at that path is reported and left unchanged. Put production
environment values, including the Django secret, in `/etc/muriel/environment`
with `root:muriel` ownership and mode `0640`.

Before any production `--apply` mutation (including package installation), bootstrap
requires an existing environment to be a readable regular non-symlink file with
that ownership and mode, retaining the parent-chain symlink guard. Unsafe files,
dangling symlinks and nonregular occupants are refused, never repaired or replaced;
`--check`/`--dry-run` reports the same refusal without mutation. Bootstrap loads
a safe file only in a `muriel` shell, clearing all four inherited identity values,
and requires both pairs to equal the explicit selection before mutation.
Omitted or divergent values fail; the file is never sourced as root, rewritten,
or printed. A safe matching file remains untouched during prerequisite
reconciliation. Absence at a safe path is
allowed in both modes, including a converged check: bootstrap creates the service
identity and directory, but the operator creates matching protected application
configuration afterward, before deployment. First bootstrap needs neither an
existing `muriel` account nor an activated release/venv or local production secrets.
Bootstrap success does not certify deployability; release and update verification
retain their own required environment checks. Staging dispatch retains its separate
staging configuration/preflight, without requiring a production environment.

The installed Nginx site remains
loopback-only; follow the [public edge runbook](public-edge.md) only after a
healthy release is deployed. Bootstrap obtains the Tailscale certificate for
the loopback-only break-glass Admin TLS listener only when both fixed files
`/etc/nginx/tailscale/muriel.crt` and `muriel.key` are absent. Issuance uses a
private temporary directory, validates the result before installation, and
cleans only its own scratch on success or failure (reporting cleanup residue).
An issuance exit status alone is not evidence of usable material.

For existing and newly issued pairs, real OpenSSL checks parseability, current
validity (including not-yet-valid certificates), coverage of the selected primary
hostname, and a usable noninteractive private key whose
public key matches the certificate. Missing/unusable OpenSSL fails validation.
These are offline local checks, not external CA trust, revocation or public
reachability certification. Files remain root-owned mode `0600` in a root-owned
`0700` directory. A valid protected pair is reused without rewriting it.

An existing wrong-host, malformed, expired, not-yet-valid, password-protected,
mismatched or partial pair is reported by read-only `--check` and refused by
`--apply`, preserving its bytes and metadata. Symlink/nonregular paths remain
refused. Bootstrap never automatically replaces, deletes or rotates existing
unusable material. Follow [explicit Admin certificate renewal](public-edge.md#admin-certificate-renewal)
for deliberate operator repair; rerunning `--apply` is not renewal. Changing
primary selection with an existing wrong-host pair refuses rather than rotating
it. Coordinate deliberate identity changes with protected configuration,
certificate renewal and actual Tailscale routes; selection alone migrates nothing.
Identity selection does not reload Nginx, activate Muriel or
enable Funnel. Cloudflare DNS/dashboard/tunnel setup remains an explicit operator
action; bootstrap does not provision it.
