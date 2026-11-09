# PostgreSQL backup and recovery

Muriel takes one logical PostgreSQL custom-format backup daily. It retains 14
local daily backups and requires every completed backup to be copied over
authenticated SSH to the commissioned off-host directory. It is not streaming
replication, continuous archiving, or point-in-time recovery.

Backup and restore use PostgreSQL `18/main` explicitly through the
`/var/run/postgresql` Unix socket on port `5432`; they do not use the PostgreSQL
wrapper's default-cluster selection. A retained PostgreSQL 16 cluster therefore
cannot receive Muriel backup or restore traffic.

## Commissioning and routine backup

Choose one off-host account and dedicated directory before enabling the timer.
Configure the `muriel` service identity's SSH key and host key verification,
then add only these non-secret routing values to the protected
`/etc/muriel/environment` (`root:muriel`, mode `0640`). The account and
per-installation directory below are illustrative; replace them with your
installation's actual off-host destination:

```text
MURIEL_BACKUP_OFFSITE_HOST=backup@example.net
MURIEL_BACKUP_OFFSITE_DIRECTORY=/srv/backups/muriel/installation-a
```

The remote directory must be dedicated to this host. Do not put an SSH password,
private key, Django secret, database password, or environment-file contents in
backup commands, logs, or metadata. Test once as the service identity, then
enable the timer:

```sh
sudo -u muriel /srv/muriel/backup-database
sudo systemctl enable --now muriel-backup.timer
systemctl list-timers muriel-backup.timer
```

The timer starts `muriel-backup.service` daily at 02:17 UTC with up to a
15-minute randomized delay and catches up after downtime. Each backup is a
custom-format `.dump` plus a mode-`0600` `.metadata` file with the SHA-256
checksum, database name, UTC creation time, and deployed application revision.
It is first written as a private partial file, checked with `pg_restore --list`,
then renamed into place. A failed dump cannot replace an older backup. If the
off-host copy fails, the command and `muriel-backup.service` fail with
`DEGRADED` while retaining the verified local copy; this is not a complete
protection status. Inspect it with:

```sh
systemctl status muriel-backup.service
journalctl --no-pager -u muriel-backup.service
```

## Restore drill and routine restore

Before relying on a backup, run a scratch drill. It checks the metadata and
checksum, creates the named disposable database, restores it, runs Muriel's
production readiness checks against it, and drops it whether the drill passes
or fails:

```sh
sudo /srv/muriel/restore-database \
  --backup /var/backups/muriel/daily-YYYYMMDDTHHMMSSZ.dump \
  --target muriel_restore_drill --scratch --confirm
```

The drill is deliberately a root operator action. It validates the production
environment and PostgreSQL 18 endpoint before creating the strict
`muriel_restore_` target as local `postgres`; dump inspection, restore, and
Muriel readiness checks run as the unprivileged `muriel` identity. The helper
drops and verifies removal of the scratch database on success, failure, HUP,
INT, and TERM. It does not stop or target the live `muriel` database or service.

For a routine production restore, stop Muriel first and deliberately name both
the production target and the extra production override. The restore itself
runs as `muriel`, not root; `--confirm` alone cannot overwrite production:

```sh
sudo systemctl stop muriel
sudo -u muriel /srv/muriel/restore-database \
  --backup /var/backups/muriel/daily-YYYYMMDDTHHMMSSZ.dump \
  --target muriel --confirm --allow-production
sudo systemctl start muriel
sudo -u muriel sh -c 'set -a; . /etc/muriel/environment; set +a; exec /srv/muriel/current/scripts/check-production'
```

## Incidents and limits

For a failed deployment after migrations begin, do not point the release symlink
back automatically. Use the reported pre-deploy backup, repair forward when
possible, or perform the deliberate production restore above before selecting a
release. For host loss, provision a replacement through the host-bootstrap and
deployment runbooks, restore the newest verified off-host dump, then restore
the application revision recorded in its metadata. For secret loss, recover the
environment values from the separate secret-management process; dumps do not
contain Django's secret or environment file. Rotating a Django secret invalidates
sessions and links but does not change the scheduling rows in a backup.

The dump retains PostgreSQL data present when it ran: schedule drafts, retained
generated snapshots, accounts, and other application rows. Changes after
the most recent completed daily backup can be lost. There is no point-in-time
recovery claim and no recovery between daily logical dumps.
