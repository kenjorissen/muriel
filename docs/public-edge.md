# Public edge

Cloudflare Tunnel is the preferred public route for the explicitly selected
secondary hostname (`edge.example.test` in the neutral examples below).
Tailscale Funnel remains a temporary fallback during migration, using the selected
primary tailnet hostname (`funnel.example.test` below). Neither route is Tailscale
identity authentication: every visitor can reach the sign-in page. The public
sites are the configured exact names; Django and Nginx reject other Host values.
Gunicorn, PostgreSQL, and the administrative Nginx listener remain local to the
host.

## Remove Ubuntu default site

Before enabling Funnel, use the checked bootstrap revision to remove only the
Ubuntu stock default-site link. `--check` reports that known link until
`--apply` removes it, and refuses to alter a different site or another occupant
at the same path. Bootstrap validates the configuration but deliberately does
not reload Nginx. After review, reload and verify the intended listeners and
public route explicitly:

```sh
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
sudo nginx -t
sudo systemctl reload nginx
sudo ss -ltn '( sport = :80 )' # Expect no listening socket.
sudo ss -ltn '( sport = :8080 or sport = :8081 or sport = :8083 )' # Expect only loopback listeners.
curl --fail --silent --show-error --connect-timeout 5 https://funnel.example.test/health/
sudo tailscale funnel status
```

The HTTPS health request and Funnel status establish that Funnel remains healthy after
the Nginx reload. Do not remove or change another enabled site merely because it
listens on port 80; identify its owner first.

The installed Nginx site accepts Funnel only at `127.0.0.1:8080` and `::1:8080`.
It requires PROXY protocol v2, so Nginx rate limits by the address supplied by
Funnel rather than by a Tailscale process address. Nginx overwrites all
forwarding headers before proxying to Gunicorn. It serves collected static files
itself, applies request, connection, body, and proxy time bounds, returns 429
for excess requests, and never exposes `/admin/` through the public listener.

## Cloudflare Tunnel

The separately installed `cloudflared` service creates outbound encrypted
connections to Cloudflare; the host does not accept an inbound Internet port for
this route. Its published application must be configured as:

```text
Hostname:     edge.example.test
Service type: HTTP
Service URL:  http://127.0.0.1:8083
```

Cloudflare terminates browser HTTPS and provisions the public certificate. The
plain HTTP hop is confined to loopback between `cloudflared` and Nginx. Nginx
accepts that hop only on `127.0.0.1:8083` and `::1:8083`, requires the exact
custom hostname, trusts `CF-Connecting-IP` only from the loopback peer, replaces
all proxy headers before Gunicorn, and applies the same public path and rate
limits as Funnel.

The tunnel token is a host secret: keep it in the Cloudflare-managed systemd
service configuration and never in this repository. Cloudflare dashboard and
DNS configuration are deliberate operator actions and are not changed by a
Muriel deployment. Before deploying this listener, add these paired values to
the root-owned `/etc/muriel/environment` file:

```text
PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test
PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test
PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=edge.example.test
PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://edge.example.test
```

Use the installation's two actual distinct lowercase ASCII DNS hostnames without
trailing dots and exactly their `https://<host>` origins, not these example names.
Existing operators must supply both matching explicit nonsecret pairs to bootstrap and local
`ops/update-production`; there is no original-domain fallback or automatic
migration. Bootstrap loads an existing protected environment only as `muriel`,
clears all four inherited identity values and refuses disagreement before mutation.
It does not rewrite secrets. First bootstrap may precede environment creation;
create matching protected configuration before deploying. See
[host setup](host-bootstrap.md) for deliberate sudo passing without `sudo -E`.

The repository `ops/nginx/muriel.conf` and `ops/nginx/muriel-admin-proxy.conf`
are source templates. Bootstrap renders named markers for primary Funnel/Admin
server names, Admin's fixed `<primary-host>:8443` Host and the independent
Cloudflare name. It compares/installs rendered bytes; do not copy raw templates
into Nginx. Nginx runtime variables, exact-host guards, trust boundaries and
limits remain unchanged. The same primary selects certificate issuance and
validation. This is identity selection within the existing topology, not route
creation or support for arbitrary hosting topologies.

After deployment and dashboard route creation, verify the independent public
path from a host outside the production server:

```sh
curl --fail --silent --show-error https://edge.example.test/health/
```

Keep Funnel enabled until the custom hostname has been verified. Once the new
route is stable and external monitoring uses it, Funnel may be disabled using
the incident-response command below; private Admin on Tailscale port 8443 is
independent and remains available.

## Local configured verification

`ops/check-public-edge` reads only these four nonsecret process settings (no
Django settings or host files). Both pairs are mandatory for this dual-route
check, even though Django itself permits an absent secondary pair:

```sh
export PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test
export PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=public.example.test
export PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://public.example.test
python3 ops/check-public-edge
```

These are neutral setup examples, not commissioned server names. Use the actual
installation's two distinct exact DNS hostnames and exact `https://<host>`
origins, without ports, paths, padding, wildcard or header characters. Missing
or invalid configuration refuses before any socket connection; there is no
original-installation fallback. `--help` needs no configuration. No password,
cookie or secret key is required for the standalone probe.

On the production host, an authorized operator maintains both pairs in the
existing `/etc/muriel/environment`; do not copy its secrets into a checkout or
shell command. Before loading it, verify that it is a readable regular file,
not a symlink, owned `root:muriel` with mode `0640` (`sudo stat -c '%F %U:%G:%a'
/etc/muriel/environment` reports metadata, not contents). It must be readable
by `muriel`. Keep it protected and use the service-user invocation below; never
source it as root. Routine `ops/update-production` checks those file safeguards
and loads only that fixed path in a `muriel` shell before invoking the probe.
Missing configuration or probe failure stops update success and subsequent
Tailscale verification, while retaining owned-artifact cleanup. Inherited
caller host/origin values cannot fill missing settings in that file.

The primary Host header goes to Funnel at `127.0.0.1:8080`; the secondary goes
to Cloudflare at `127.0.0.1:8083`. Configuration cannot change either network
destination. The actual Nginx/server identities must already match these values.
This probe does not render Nginx, obtain certificates or alter Tailscale
mappings. Nginx and certificate identities are selected through bootstrap,
not the probe. Different
probe values alone do not make an arbitrary-domain installation work.

Routine update's subsequent read-only route comparison uses the same protected
primary hostname and matching HTTPS origin, not a compiled domain, SSH alias or
hostname discovered from route state. It loads and validates configuration as
`muriel` and receives the privileged `tailscale serve status --json` capture over
stdin; the owned staging directory stays private. The complete state must have
TLS termination for that host on TCP 443 with integer PROXY protocol 2 forwarding
to `127.0.0.1:8080`, private HTTPS Admin on 8443 with exactly the
`https+insecure://127.0.0.1:8081` handler, and only that host's port 443 Funnel
permission. Optional staging is exactly private HTTPS on 10000 for the same host
with `http://127.0.0.1:8082`; partial staging or staging Funnel permission fails.
Extra keys, handlers, listeners, foreign hostnames and wrong types also fail,
as do acquisition errors and malformed JSON. No route is created or changed.
The actual installed routes must already match protected configuration.

Only after edge and complete route verification succeeds does routine update
report the browser destinations using both validated hostnames captured by that
route-comparison invocation: preferred public Cloudflare
`https://<secondary-host>/`, temporary public Funnel fallback
`https://<primary-host>/`, and private Admin `https://<primary-host>:8443/admin/`.
It relays these labeled URLs through existing SSH stdout without reloading
settings for display. Neither the transport alias nor inherited local values
selects them. The local deployed-commit success message remains conditional on
successful remote completion; verified URLs need not wait for EXIT cleanup.
Staging update's separately owned browser output remains unchanged.

## Enable

Do not enable Funnel until a deployed release is healthy, Nginx has been tested
and reloaded, and the local edge probes pass. Bootstrap obtains the Tailscale
certificate used by the private Admin listener only on first installation with
both files absent. Use the [explicit renewal procedure](#admin-certificate-renewal)
when it is nearing expiry or bootstrap refuses an existing pair. After successful
certificate validation, continue with:

```sh
sudo nginx -t
sudo systemctl reload nginx
sudo -u muriel sh -eu -c 'set -a; . /etc/muriel/environment; set +a; exec /srv/muriel/current/.venv/bin/python /srv/muriel/current/ops/check-public-edge'
```

The probe sends synthetic PROXY-v2 requests to the Funnel loopback listener and
plain HTTP with the same synthetic source in `CF-Connecting-IP` to the Cloudflare
loopback listener. Both check health, Admin blocking and dotfile blocking. It has a five-second
per-request timeout (configurable only between zero and ten seconds). It first
checks `/health/` for application and database readiness, then reads the
stylesheet URL rendered by the public sign-in page and verifies that exact
versioned asset, along with blocked Admin and dot-file paths and the sensitive
sign-in limit, without creating or modifying an account. For one synthetic
source address, it requires the stylesheet-rendering sign-in request plus the
five configured `burst` requests to succeed, then requires the next immediate
request to receive 429. Sensitive routes have a `1r/s` rate with that burst
budget. This establishes the configured Nginx leaky-bucket limit at the local
public edge; it does not test public Funnel reachability.

After the probes pass, an operator with Tailscale control-plane authority may
make the HTTPS listener persistent. This is a Tailscale control-plane action,
not a Muriel deployment action:

```sh
sudo tailscale funnel --bg --tls-terminated-tcp=443 --proxy-protocol=2 tcp://127.0.0.1:8080
sudo tailscale funnel status
```

`--tls-terminated-tcp=443` keeps the public connection HTTPS while passing the
decrypted stream and PROXY-v2 preamble to the loopback Nginx listener. `--bg`
saves that configuration for `tailscaled` to restore after a Tailscale or host
restart. Confirm that status names only the expected TLS-terminated HTTPS
listener and target. Funnel must stay disabled until the production environment's
exact host and CSRF origin match the hostname above.

## Incident response

Remove the public route immediately while retaining the local service and data:

```sh
sudo tailscale funnel --tls-terminated-tcp=443 --proxy-protocol=2 off
sudo tailscale funnel status
```

To inspect persistent Serve/Funnel state after any restart, use both:

```sh
sudo tailscale funnel status
sudo tailscale serve status --json
```

Do not treat Funnel as a CDN, WAF, availability service, or a Tailnet login
gate. Its public-edge feature remains beta, and its bandwidth and connection
limits are controlled by Tailscale and can change. The custom domain uses the
separate Cloudflare route; it is not an alias layered over Funnel.

## Admin certificate renewal

The human-selected safety boundary is refusal, not automatic replacement:
`ops/bootstrap-ubuntu --check` is read-only and `--apply` preserves an existing
wrong-host or otherwise unusable/partial pair with its metadata. Do not delete a
file just to make bootstrap issue another pair. Resolve symlink/nonregular paths
and partial material deliberately after identifying their owner. A valid pair
is reused; bootstrap neither renews it nor reloads Nginx.

The intended Admin hostname is the explicit `PRECEPTOR_PRODUCTION_ALLOWED_HOST`
(`funnel.example.test` in these examples), also used for both primary Nginx names
and the fixed Admin proxy authority. Changing it never silently rotates an
existing certificate or migrates protected configuration. Coordinate any deliberate
identity change with both protected pairs and actual Tailscale/DNS state.
Only an authorized operator on the intended host should perform renewal. First
retain any existing material in an operator-owned root-only backup directory
(mode `0700`, private keys `0600`) and diagnose the refusal. Never copy keys into
a checkout or logs. The commands below are deliberate replacement actions, not
commands run automatically by bootstrap or authorization to operate a live host:

```sh
sudo tailscale cert --cert-file /etc/nginx/tailscale/muriel.crt --key-file /etc/nginx/tailscale/muriel.key "$PRECEPTOR_PRODUCTION_ALLOWED_HOST"
sudo chown root:root /etc/nginx/tailscale /etc/nginx/tailscale/muriel.crt /etc/nginx/tailscale/muriel.key
sudo chmod 0700 /etc/nginx/tailscale
sudo chmod 0600 /etc/nginx/tailscale/muriel.crt /etc/nginx/tailscale/muriel.key
# With both installation pairs explicitly selected as above:
sudo env PRECEPTOR_PRODUCTION_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN" \
  PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="$PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST" \
  PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="$PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN" \
  ops/bootstrap-ubuntu --check
```

Stop on any failure. If issuance leaves an unusable or partial pair, investigate
and repair it explicitly; do not activate it or expect `--apply` to repair it.
The check requires real OpenSSL to parse the certificate, verify hostname and
current validity, check the private key without prompting, and compare public
keys (not RSA-only moduli). It trusts the supplied certificate only for this
local check: it does **not** establish external CA trust, revocation status or
network reachability. After a successful check, explicitly run `sudo nginx -t`
and only then `sudo systemctl reload nginx`; verify the private Admin route and
public-edge safeguards below. No renewal daemon or automatic rollback is added.

## Break-glass Admin

Normal superuser account administration is available after ordinary public
authentication through **Accounts**. Django Admin is separate break-glass
authority and is unavailable at the public Funnel edge. It is instead available
to tailnet clients permitted by the tailnet ACLs at
`https://funnel.example.test:8443/admin/` (substitute the selected primary name). Tailnet reachability is
only a network boundary: Django Admin still requires a Django superuser login.

After Nginx has been tested and reloaded, an operator with Tailscale
control-plane authority enables the persistent private route. This is separate
from Funnel and can be enabled or removed without redeploying Muriel:

```sh
sudo tailscale serve --bg --https=8443 https+insecure://127.0.0.1:8081
sudo tailscale serve status
sudo tailscale funnel status
```

`https+insecure` is limited to the loopback Nginx listener, whose Tailscale
certificate is expected for the tailnet hostname. `--bg` saves the Serve route
for `tailscaled` to restore after a Tailscale or host restart. It coexists with
the unchanged TLS-terminated public Funnel listener on 443.

From a tailnet client permitted by the ACLs, verify the Admin login redirect and
the separate public boundary:

```sh
curl --fail --silent --show-error --connect-timeout 5 --output /dev/null --write-out '%{http_code}\n' https://funnel.example.test:8443/admin/
sudo -u muriel sh -eu -c 'set -a; . /etc/muriel/environment; set +a; exec /srv/muriel/current/.venv/bin/python /srv/muriel/current/ops/check-public-edge'
```

The first command must print `302` for the Django Admin login redirect; the
local public edge probe requires `/admin/` to remain 404 through Funnel. The
loopback Nginx listener pins the same HTTPS host and port and sets the Admin
marker before proxying to Django, so CSRF validation and secure session cookies
match the browser URL without a client hosts-file entry or SSH tunnel.

To remove only the private Admin route while preserving Funnel on 443:

```sh
sudo tailscale serve --https=8443 off
sudo tailscale serve status
sudo tailscale funnel status
```
