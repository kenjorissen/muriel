import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import SimpleTestCase

from .production_identity_fixtures import INVALID_PRODUCTION_PAIRS, PRODUCTION_IDENTITY
from .staging_identity_fixtures import INVALID_STAGING_PAIRS

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "ops" / "bootstrap-ubuntu"
STAGING_BOOTSTRAP = ROOT / "ops" / "bootstrap-staging-ubuntu"


class UbuntuHostBootstrapTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.fake_root = self.temporary_path / "root"
        self.fake_bin = self.temporary_path / "bin"
        self.state = self.temporary_path / "state"
        self.log = self.temporary_path / "commands.log"
        self.fake_root.mkdir()
        self.fake_bin.mkdir()
        self.state.mkdir()
        self.render_scratch = self.temporary_path / "render-scratch"
        self.render_scratch.mkdir()
        self.write_os_release("ubuntu", "26.04")
        (self.fake_root / "run" / "systemd" / "system").mkdir(parents=True)
        (self.fake_root / "etc" / "systemd" / "system").mkdir(parents=True)
        self.write_dispatcher()
        self.certificate, self.key = self.make_pair("valid")

    def openssl(self, *arguments):
        result = subprocess.run(
            ["openssl", *map(str, arguments)],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def make_pair(self, name, host="funnel.example.test", *, interval=None, rsa=False):
        directory = self.temporary_path / name
        directory.mkdir()
        certificate, key = directory / "certificate.pem", directory / "key.pem"
        self.openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048" if rsa else "ec",
            *([] if rsa else ["-pkeyopt", "ec_paramgen_curve:P-256"]),
            "-nodes",
            "-keyout",
            key,
            "-out",
            certificate,
            "-days",
            "2",
            "-subj",
            "/CN=fixture.test",
            "-addext",
            f"subjectAltName=DNS:{host}",
        )
        if interval:
            # ca supports explicit notBefore/notAfter on older OpenSSL too.
            # Dates derive from the current clock, not a shipped expiry/key.
            request = directory / "request.pem"
            self.openssl("req", "-new", "-key", key, "-out", request, "-subj", "/CN=fixture.test")
            (directory / "index").touch()
            config = directory / "ca.cnf"
            config.write_text(
                "[ca]\ndefault_ca=fixture\n[fixture]\n"
                f"database={directory / 'index'}\nnew_certs_dir={directory}\n"
                f"serial={directory / 'serial'}\n"
                "default_md=sha256\npolicy=names\nx509_extensions=extensions\n"
                f"[names]\ncommonName=supplied\n[extensions]\nsubjectAltName=DNS:{host}\n"
            )
            now = datetime.now(UTC)
            start, end = (
                (now + timedelta(days=days)).strftime("%Y%m%d%H%M%SZ") for days in interval
            )
            dated = directory / "dated.pem"
            self.openssl(
                "ca",
                "-batch",
                "-selfsign",
                "-config",
                config,
                "-keyfile",
                key,
                "-cert",
                certificate,
                "-in",
                request,
                "-out",
                dated,
                "-notext",
                "-rand_serial",
                "-startdate",
                start,
                "-enddate",
                end,
            )
            certificate = dated
        return certificate, key

    def write_os_release(self, operating_system, version):
        os_release = self.fake_root / "etc" / "os-release"
        os_release.parent.mkdir(parents=True, exist_ok=True)
        os_release.write_text(f'ID="{operating_system}"\nVERSION_ID="{version}"\n')

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            """#!/bin/sh
set -eu
command=$(basename "$0")
state=$FAKE_STATE
printf '%s\\0' "$command" "$@" '' >> "$FAKE_COMMANDS"
case "$command" in
    systemctl)
        printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
        case "$*" in
            "enable --now postgresql@18-main") touch "$state/postgresql-18-running" ;;
            "is-enabled muriel.service") printf '%s\\n' "${MURIEL_ENABLED:-disabled}" ;;
            "is-active muriel.service") printf '%s\\n' "${MURIEL_ACTIVE:-inactive}" ;;
        esac
        exit 0
        ;;
    tailscale)
        case "$1" in
            version) printf '1.0.0\\n' ;;
            cert)
                shift
                while [ "$#" -gt 1 ]; do
                    case "$1" in
                        --cert-file) certificate=$2 ;;
                        --key-file) key=$2 ;;
                        *) exit 2 ;;
                    esac
                    shift 2
                done
                [ "$(stat -c '%a' "${key%/*}")" = 700 ] || exit 2
                case "${ISSUANCE_RESULT:-valid}" in
                    empty) touch "$certificate" "$key" ;;
                    failure) cp "$FIXTURE_KEY" "$key"; exit 1 ;;
                    *) cp "$FIXTURE_CERTIFICATE" "$certificate"; cp "$FIXTURE_KEY" "$key" ;;
                esac
                ;;
        esac
        exit 0
        ;;
    dpkg-query)
        for argument; do package=$argument; done
        [ -f "$state/package-$package" ] && printf 'ii '
        exit 0
        ;;
    pg_lsclusters)
        if [ -f "$state/retained-postgresql-16" ]; then
            printf '16  main    5433 down   postgres /var/lib/postgresql/16/main /var/log/postgresql/postgresql-16-main.log\n'
        fi
        printf '18  main    %s online postgres /var/lib/postgresql/18/main /var/log/postgresql/postgresql-18-main.log\n' "${POSTGRES_PORT:-5432}"
        exit 0
        ;;
    apt-get)
        printf 'apt-get %s\\n' "$*" >> "$FAKE_LOG"
        [ -x "$FAKE_ROOT/usr/sbin/policy-rc.d" ] || exit 2
        policy_status=0
        "$FAKE_ROOT/usr/sbin/policy-rc.d" || policy_status=$?
        [ "$policy_status" = 101 ] || exit 2
        [ "${PACKAGE_FAILURE:-no}" = no ] || exit 1
        case "$*" in
            install*)
                if [ -f "$state/package-python3.14-venv" ] && \\
                   [ -f "$state/package-postgresql-18" ] && \\
                   [ -f "$state/package-postgresql-client-18" ] && \\
                   [ -f "$state/package-nginx" ] && \\
                   [ -f "$state/package-openssl" ]; then
                    touch "$state/repeated-package-install"
                fi
                ;;
        esac
        for argument; do
            case "$argument" in
                python3.14-venv|postgresql-18|postgresql-client-18|nginx|openssl)
                    touch "$state/package-$argument"
                    ;;
            esac
        done
        mkdir -p "$FAKE_ROOT/etc/nginx/sites-available" "$FAKE_ROOT/etc/nginx/sites-enabled" "$FAKE_ROOT/etc/nginx/snippets"
        exit 0
        ;;
    getent)
        case "$1:$2" in
            group:muriel)
                [ -f "$state/group" ] || exit 2
                printf 'muriel:x:999:\\n'
                ;;
            passwd:muriel)
                [ -f "$state/user" ] || exit 2
                IFS=: read -r identity group home shell < "$state/user"
                [ "$identity" = muriel ] || exit 2
                printf '%s:x:999:999::%s:%s\\n' "$identity" "$home" "$shell"
                ;;
        esac
        exit 0
        ;;
    groupadd)
        for argument; do group=$argument; done
        [ "$group" = muriel ] || exit 1
        [ ! -f "$state/group" ] || touch "$state/repeated-group-create"
        touch "$state/group"
        exit 0
        ;;
    useradd)
        previous=
        group=
        home=
        shell=
        for argument; do
            case "$previous" in
                --gid) group=$argument; previous=; continue ;;
                --home-dir) home=$argument; previous=; continue ;;
                --shell) shell=$argument; previous=; continue ;;
            esac
            case "$argument" in
                --gid|--home-dir|--shell) previous=$argument ;;
                --gid=*) group=${argument#--gid=} ;;
                --home-dir=*) home=${argument#--home-dir=} ;;
                --shell=*) shell=${argument#--shell=} ;;
                *) user=$argument ;;
            esac
        done
        [ "$user" = muriel ] && [ "$group" = muriel ] || exit 1
        [ ! -f "$state/user" ] || touch "$state/repeated-user-create"
        printf '%s:%s:%s:%s\\n' "$user" "$group" "$home" "$shell" > "$state/user"
        exit 0
        ;;
    usermod)
        previous=
        group=
        for argument; do
            case "$previous" in
                --groups) group=$argument; previous=; continue ;;
            esac
            case "$argument" in
                --groups|-G) previous=$argument ;;
                --groups=*) group=${argument#--groups=} ;;
                *) user=$argument ;;
            esac
        done
        [ "$user" = www-data ] && [ "$group" = muriel ] || exit 1
        touch "$state/www-data-muriel"
        exit 0
        ;;
    id)
        case "${1:-}" in
            -nG)
                [ -f "$state/www-data-muriel" ] && printf 'www-data muriel\\n' || \\
                    printf 'www-data\\n'
                ;;
            *) printf '1000\\n' ;;
        esac
        exit 0
        ;;
    runuser)
        printf 'runuser %s\\n' "$*" >> "$FAKE_LOG"
        if [ "$1:$2:$3" = '-u:muriel:--' ]; then
            shift 3
            BOOTSTRAP_SERVICE_USER=muriel
            export BOOTSTRAP_SERVICE_USER
            exec "$@"
        fi
        cluster=
        host=
        port=
        query=
        option_requires_value=
        for argument; do
            if [ -n "$option_requires_value" ]; then
                case "$option_requires_value" in
                    --cluster) cluster=$argument ;;
                    --host) host=$argument ;;
                    --port) port=$argument ;;
                    -tAc) query=$argument ;;
                esac
                option_requires_value=
                continue
            fi
            case "$argument" in
                --cluster=18/main) cluster=18/main ;;
                --cluster=*) cluster=${argument#--cluster=} ;;
                --host=/var/run/postgresql) host=/var/run/postgresql ;;
                --host=*) host=${argument#--host=} ;;
                --port=5432) port=5432 ;;
                --port=*) port=${argument#--port=} ;;
                --cluster|--host|--port|-tAc) option_requires_value=$argument ;;
            esac
            query=$argument
        done
        [ -z "$option_requires_value" ] || exit 1
        endpoint_cluster=$cluster
        if [ "$host" != /var/run/postgresql ] || [ "$port" != 5432 ]; then
            endpoint_cluster=wrong-endpoint
        fi
        endpoint_cluster=${POSTGRES_ENDPOINT_CLUSTER:-$endpoint_cluster}
        case "$query" in
            "SHOW server_version_num")
                case "$endpoint_cluster" in
                    18/main)
                        [ -f "$state/postgresql-18-running" ] || exit 1
                        printf '180000\\n'
                        ;;
                    16/main)
                        [ -f "$state/retained-postgresql-16" ] || exit 1
                        printf '160000\\n'
                        ;;
                esac
                ;;
            "SHOW port") printf '%s\n' "${POSTGRES_PORT:-5432}" ;;
            "SHOW unix_socket_directories") printf '/var/run/postgresql\n' ;;
            "SHOW config_file") printf '/etc/postgresql/%s/postgresql.conf\n' "$endpoint_cluster" ;;
            *"FROM pg_authid"*)
                if [ -f "$state/role" ]; then
                    if [ -f "$state/role-password" ]; then
                        printf 'true:false:false:false\\n'
                    elif [ -f "$state/role-superuser" ]; then
                        printf 'true:true:true:false\\n'
                    elif [ -f "$state/role-createdb" ]; then
                        printf 'true:true:false:true\\n'
                    else
                        printf 'true:true:false:false\\n'
                    fi
                fi
                ;;
            *"FROM pg_roles"*)
                [ -f "$state/role" ] && printf 'true:true\\n'
                ;;
            *"FROM pg_database"*)
                [ -f "$state/database" ] && printf 'muriel\\n'
                ;;
            "CREATE ROLE muriel LOGIN")
                [ ! -f "$state/role" ] || touch "$state/repeated-role-create"
                touch "$state/role"
                printf 'create-role\\n' >> "$FAKE_LOG"
                ;;
            "CREATE DATABASE muriel OWNER muriel")
                [ ! -f "$state/database" ] || touch "$state/repeated-database-create"
                touch "$state/database"
                printf 'create-database\\n' >> "$FAKE_LOG"
                ;;
        esac
        exit 0
        ;;
    nginx)
        printf 'nginx %s\\n' "$*" >> "$FAKE_LOG"
        exit 0
        ;;
    chown)
        printf 'chown %s\\n' "$*" >> "$FAKE_LOG"
        exit 0
        ;;
    stat)
        for argument; do path=$argument; done
        case "$path" in
            */etc/nginx/tailscale|*/etc/nginx/tailscale/*)
                if [ "$2" = '%a' ]; then
                    exec "$REAL_STAT" "$@"
                fi
                printf 'root:root:%s\\n' "$("$REAL_STAT" -c '%a' "$path")"
                exit 0
                ;;
            */srv/muriel) value=root:muriel:750 ;;
            */srv/muriel/releases|*/srv/muriel/shared) value=muriel:muriel:750 ;;
            */srv/muriel/shared/pip-cache) value=muriel:muriel:700 ;;
            */srv/muriel/shared/static) value=muriel:www-data:750 ;;
            */srv/muriel/deploy-release) value=root:root:755 ;;
            */srv/muriel/backup-database|*/srv/muriel/create-superuser|*/srv/muriel/clear-production-login-access|*/srv/muriel/reset-production-domain|*/srv/muriel/restore-database) value=root:root:755 ;;
            */etc/muriel/environment)
                mode=${ENVIRONMENT_MODE:-$("$REAL_STAT" -c '%a' "$path")}
                printf '%s:%s\\n' "${ENVIRONMENT_OWNER:-root:muriel}" "$mode"
                exit 0
                ;;
            */etc/muriel) value=root:muriel:750 ;;
            */var/backups/muriel) value=muriel:muriel:700 ;;
            *) value=root:root:644 ;;
        esac
        printf '%s\\n' "$value"
        exit 0
        ;;
esac
exit 1
""",
        )
        for command in (
            "apt-get",
            "chown",
            "dpkg-query",
            "getent",
            "groupadd",
            "id",
            "nginx",
            "pg_lsclusters",
            "runuser",
            "stat",
            "systemctl",
            "tailscale",
            "useradd",
            "usermod",
        ):
            (self.fake_bin / command).symlink_to(dispatcher.name)

        for command in ("mkdir", "chmod", "cp", "mv", "mktemp", "rm", "ln", "rmdir"):
            self.write_executable(
                self.fake_bin / command,
                "#!/bin/sh\n"
                f"printf '%s\\0' {command} \"$@\" '' >> \"$FAKE_COMMANDS\"\n"
                f'exec {shlex.quote(shutil.which(command))} "$@"\n',
            )

    def run_bootstrap(self, mode, root=None, *, bootstrap=BOOTSTRAP, **environment):
        root = self.fake_root if root is None else root
        return subprocess.run(
            [str(bootstrap), mode, "--root", str(root)],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "FAKE_LOG": str(self.log),
                "FAKE_COMMANDS": str(self.temporary_path / "commands.bin"),
                "FAKE_ROOT": str(self.fake_root),
                "FAKE_STATE": str(self.state),
                "FIXTURE_CERTIFICATE": str(self.certificate),
                "FIXTURE_KEY": str(self.key),
                "REAL_STAT": shutil.which("stat"),
                "TMPDIR": str(self.render_scratch),
                **PRODUCTION_IDENTITY,
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )

    def received_commands(self):
        command_log = self.temporary_path / "commands.bin"
        if not command_log.exists():
            return []
        return [
            tuple(part.decode() for part in record.split(b"\0") if part)
            for record in command_log.read_bytes().split(b"\0\0")
            if record
        ]

    def received(self, command):
        return [
            tuple(arguments)
            for received, *arguments in self.received_commands()
            if received == command
        ]

    @staticmethod
    def option_value(arguments, option):
        for index, argument in enumerate(arguments):
            if argument.startswith(f"{option}="):
                return argument.removeprefix(f"{option}=")
            if argument == option and index + 1 < len(arguments):
                return arguments[index + 1]
        return None

    @staticmethod
    def managed_unit_identity(argument):
        unit_aliases = {
            "muriel": "muriel.service",
            "muriel.service": "muriel.service",
            "muriel-backup.timer": "muriel-backup.timer",
            "muriel-backup.service": "muriel-backup.service",
            "nginx": "nginx.service",
            "nginx.service": "nginx.service",
        }
        return unit_aliases.get(argument)

    def assert_no_managed_unit_mutations(self, commands):
        mutation_verbs = {
            "disable",
            "enable",
            "freeze",
            "kill",
            "mask",
            "preset",
            "reload",
            "reload-or-restart",
            "reload-or-try-restart",
            "restart",
            "revert",
            "start",
            "stop",
            "thaw",
            "try-restart",
            "unmask",
        }
        for command, *arguments in commands:
            if command != "systemctl":
                continue
            units = {self.managed_unit_identity(argument) for argument in arguments}
            if units - {None} and mutation_verbs.intersection(arguments):
                self.fail(f"unexpected mutation of managed unit(s): {tuple(arguments)!r}")

    def assert_no_nginx_mutations(self, commands):
        mutation_arguments = {"-s", "quit", "reload", "reopen", "stop"}
        for command, *arguments in commands:
            if command == "nginx" and mutation_arguments.intersection(arguments):
                self.fail(f"unexpected Nginx mutation: {tuple(arguments)!r}")

    def run_path_guard(self, root, path):
        # No safe public invocation owns the unique real-root and lookalike-path
        # cases, so retain this narrow extraction of the one root-path guard rather
        # than pretending this focused boundary test parses shell source generally.
        guard = (
            BOOTSTRAP.read_text()
            .split("require_safe_path() {\n", 1)[1]
            .split("\n}\n\nrequire_safe_parent()", 1)[0]
        )
        return subprocess.run(
            [
                "sh",
                "-c",
                f'fail() {{ exit 1; }}\nrequire_safe_path() {{\n{guard}\n}}\nroot=$1\nrequire_safe_path "$2"',
                "bootstrap-path-guard",
                str(root),
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def enable_stock_nginx_site(self, target="../sites-available/default"):
        nginx_enabled = self.fake_root / "etc/nginx/sites-enabled"
        nginx_enabled.mkdir(parents=True, exist_ok=True)
        (nginx_enabled / "default").symlink_to(target)

    @property
    def tls_directory(self):
        return self.fake_root / "etc/nginx/tailscale"

    @staticmethod
    def snapshot(root):
        result = {}
        for path in (root, *root.rglob("*")):
            metadata = path.lstat()
            result[path] = (
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                os.readlink(path)
                if path.is_symlink()
                else path.read_bytes()
                if path.is_file() and os.access(path, os.R_OK)
                else None,
            )
        return result

    def install_pair(self, certificate, key):
        self.tls_directory.mkdir(parents=True, exist_ok=True)
        self.tls_directory.chmod(0o700)
        for source, name in ((certificate, "muriel.crt"), (key, "muriel.key")):
            destination = self.tls_directory / name
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            if source:
                shutil.copyfile(source, destination)
                destination.chmod(0o600)

    @property
    def environment_file(self):
        return self.fake_root / "etc/muriel/environment"

    def write_environment(self, host="edge.example.test", primary="funnel.example.test"):
        self.environment_file.parent.mkdir(parents=True, exist_ok=True)
        self.environment_file.write_text(
            "PRECEPTOR_PRODUCTION_SECRET_KEY=fixture-secret-not-for-diagnostics\n"
            '[ "${BOOTSTRAP_SERVICE_USER:-}" = muriel ] || exit 91\n'
            f"PRECEPTOR_PRODUCTION_ALLOWED_HOST={primary}\n"
            f"PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://{primary}\n"
            f"PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST={host}\n"
            f"PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://{host}\n"
        )
        self.environment_file.chmod(0o640)

    def assert_no_prerequisite_mutations(self, commands):
        for command, *arguments in commands:
            if command == "systemctl":
                self.assertIn(arguments[0], {"--version", "is-enabled", "is-active"})
            elif command == "tailscale":
                self.assertEqual(arguments, ["version"])
            elif command == "runuser":
                if arguments[:3] == ["-u", "muriel", "--"]:
                    self.assertEqual(arguments[3:6], ["sh", "-eu", "-c"])
                else:
                    self.assertTrue(arguments[-1].startswith(("SELECT ", "SHOW ")), arguments)
            elif command == "nginx":
                self.assertEqual(arguments, ["-t"])
            else:
                self.assertIn(command, {"stat", "getent", "id", "dpkg-query", "pg_lsclusters"})

    def assert_environment_refusal(self, diagnostic, *extra_roots, **environment):
        roots = (self.fake_root, self.state, *extra_roots)
        before = {root: self.snapshot(root) for root in roots}
        for mode in ("--apply", "--check", "--dry-run"):
            with self.subTest(mode=mode):
                command_count = len(self.received_commands())
                result = self.run_bootstrap(mode, **environment)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual({root: self.snapshot(root) for root in roots}, before)
                self.assert_no_prerequisite_mutations(self.received_commands()[command_count:])
                self.assertNotIn(
                    "fixture-secret-not-for-diagnostics", result.stdout + result.stderr
                )

    def test_unsafe_environment_metadata_refuses_before_prerequisite_mutation(self):
        self.write_environment()
        # Packages, identity, database and managed files are absent. The directory
        # also needs reconciliation. Owner/group are fixture-selected; mode and
        # readability are real OS observations, not privilege separation evidence.
        self.environment_file.parent.chmod(0o777)
        for owner, mode in (
            ("muriel:muriel", 0o640),
            ("root:other", 0o640),
            ("root:muriel", 0o600),
        ):
            with self.subTest(owner=owner, mode=mode):
                self.environment_file.chmod(mode)
                self.assert_environment_refusal(
                    "production environment file must be root:muriel mode 0640",
                    ENVIRONMENT_OWNER=owner,
                )

    def test_unreadable_environment_refuses_without_reading_or_repair(self):
        self.write_environment()
        contents = self.environment_file.read_bytes()
        self.environment_file.chmod(0)
        try:
            self.assertFalse(os.access(self.environment_file, os.R_OK))
            # Select otherwise-safe metadata to exercise readability independently
            # of the mode check. No fake command supplies the shell's -r result.
            self.assert_environment_refusal(
                "production environment file is unreadable", ENVIRONMENT_MODE="640"
            )
        finally:
            self.environment_file.chmod(0o640)
        self.assertEqual(self.environment_file.read_bytes(), contents)

    def test_unsafe_environment_paths_refuse_before_prerequisite_mutation(self):
        target = self.temporary_path / "environment-target"
        target.mkdir()
        (target / "environment").write_text("fixture-secret-not-for-diagnostics\n")
        self.environment_file.parent.mkdir(parents=True)
        for kind in ("leaf", "dangling", "parent", "missing-via-parent", "directory", "fifo"):
            with self.subTest(kind=kind):
                diagnostic = "production environment file must be a regular non-symlink file"
                if kind in ("parent", "missing-via-parent"):
                    self.environment_file.parent.rmdir()
                    destination = target if kind == "parent" else target / "absent"
                    self.environment_file.parent.symlink_to(destination)
                    diagnostic = "refusing symlinked managed path"
                elif kind in ("leaf", "dangling"):
                    destination = target / ("environment" if kind == "leaf" else "absent")
                    self.environment_file.symlink_to(destination)
                    diagnostic = "refusing symlinked managed path"
                elif kind == "directory":
                    self.environment_file.mkdir()
                else:
                    os.mkfifo(self.environment_file)
                try:
                    self.assert_environment_refusal(diagnostic, target)
                finally:
                    if kind in ("parent", "missing-via-parent"):
                        self.environment_file.parent.unlink()
                        self.environment_file.parent.mkdir()
                    elif kind == "directory":
                        self.environment_file.rmdir()
                    else:
                        self.environment_file.unlink()

    def test_safe_environment_allows_reconciliation_without_rewrite_or_secret_output(self):
        self.write_environment()
        before = self.snapshot(self.environment_file)
        for mode in ("--apply", "--check", "--apply"):
            result = self.run_bootstrap(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(self.environment_file), before)
            self.assertTrue(
                any(args[:3] == ("-u", "muriel", "--") for args in self.received("runuser"))
            )
            self.assertNotIn("fixture-secret-not-for-diagnostics", result.stdout + result.stderr)
        self.assertTrue((self.state / "database").exists())
        self.assertEqual(self.environment_file.parent.stat().st_mode & 0o777, 0o750)

    def test_identity_input_refuses_before_host_work(self):
        for prefix, other_host in (
            ("PRECEPTOR_PRODUCTION", "edge.example.test"),
            ("PRECEPTOR_PRODUCTION_SECONDARY", "funnel.example.test"),
        ):
            for host, origin in (*INVALID_PRODUCTION_PAIRS, (other_host, f"https://{other_host}")):
                for mode in ("--check", "--apply"):
                    with self.subTest(prefix=prefix, host=host, origin=origin, mode=mode):
                        before = self.snapshot(self.fake_root)
                        result = self.run_bootstrap(
                            mode,
                            **{
                                f"{prefix}_ALLOWED_HOST": host or "",
                                f"{prefix}_CSRF_TRUSTED_ORIGIN": origin or "",
                            },
                        )
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("Production", result.stderr)
                        self.assertEqual(self.received_commands(), [])
                        self.assertEqual(self.snapshot(self.fake_root), before)
                        self.assertEqual(list(self.render_scratch.iterdir()), [])

    def test_existing_pairs_must_match_and_cannot_inherit_missing_fields(self):
        self.write_environment()
        contents = self.environment_file.read_text()
        for setting in PRODUCTION_IDENTITY:
            for replacement in (None, "other.test", ""):
                with self.subTest(setting=setting, replacement=replacement):
                    lines = [
                        line for line in contents.splitlines() if not line.startswith(setting + "=")
                    ]
                    if replacement is not None:
                        lines.append(f"{setting}={replacement}")
                    self.environment_file.write_text("\n".join(lines) + "\n")
                    self.assert_environment_refusal("production environment identity")
                    self.assertEqual(list(self.render_scratch.iterdir()), [])

    def test_selected_pairs_render_and_converge_without_rewriting_protected_files(self):
        for primary, host in (
            ("funnel.example.test", "edge.example.test"),
            ("fallback.other.test", "public.other.test"),
        ):
            with self.subTest(primary=primary, secondary=host):
                environment = {
                    "PRECEPTOR_PRODUCTION_ALLOWED_HOST": primary,
                    "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": f"https://{primary}",
                    "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": host,
                    "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": f"https://{host}",
                }
                pair = self.make_pair(primary, primary)
                self.install_pair(*pair)
                self.write_environment(host, primary)
                protected_before = self.snapshot(self.environment_file) | self.snapshot(
                    self.tls_directory
                )
                issuance_before = len(self.received("tailscale"))
                for mode in ("--apply", "--check", "--apply"):
                    before = self.snapshot(self.fake_root)
                    result = self.run_bootstrap(mode, **environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if mode == "--check":
                        self.assertEqual(self.snapshot(self.fake_root), before)
                    # Directory metadata may be reconciled; certificate bytes and
                    # metadata and the protected environment must remain untouched.
                    after = self.snapshot(self.environment_file) | self.snapshot(self.tls_directory)
                    for path in protected_before.keys() - {self.tls_directory}:
                        self.assertEqual(after[path], protected_before[path])
                    self.assertEqual(list(self.render_scratch.iterdir()), [])
                self.assertFalse(
                    any(args[0] == "cert" for args in self.received("tailscale")[issuance_before:])
                )
                site = (self.fake_root / "etc/nginx/sites-available/muriel").read_text()
                for port, suffix in ((8080, " proxy_protocol"), (8081, " ssl")):
                    self.assertRegex(
                        site,
                        rf"listen 127\.0\.0\.1:{port}{suffix};\s+listen \[::1\]:{port}{suffix};\s+server_name {re.escape(primary)};",
                    )
                admin_proxy = (
                    self.fake_root / "etc/nginx/snippets/muriel-admin-proxy.conf"
                ).read_text()
                self.assertIn(f"proxy_set_header Host {primary}:8443;", admin_proxy)
                self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", admin_proxy)
                self.assertIn("proxy_set_header X-Muriel-Admin 1;", admin_proxy)
                self.assertRegex(
                    site,
                    r"listen 127\.0\.0\.1:8083;\s+listen \[::1\]:8083;\s+server_name "
                    + re.escape(host)
                    + ";",
                )
                self.assertEqual(site.count(f"server_name {host};"), 1)
                self.assertIn("$binary_remote_addr", site)
                self.assertIn("try_files $uri =404;", site)
                self.assertNotIn("@PRODUCTION_SECONDARY_HOST@", site)

    def test_changed_primary_preserves_existing_certificate_and_refuses_rotation(self):
        for primary, changed in (
            ("funnel.example.test", "fallback.other.test"),
            ("fallback.other.test", "funnel.example.test"),
        ):
            with self.subTest(primary=primary, changed=changed):
                pair = self.make_pair(primary, primary)
                self.install_pair(*pair)
                before = self.snapshot(self.tls_directory)
                for mode in ("--check", "--apply"):
                    result = self.run_bootstrap(
                        mode,
                        PRECEPTOR_PRODUCTION_ALLOWED_HOST=changed,
                        PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=f"https://{changed}",
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f"repair/renewal for {changed}", result.stderr)
                    self.assertEqual(self.snapshot(self.tls_directory), before)
        self.assertFalse(any(args[0] == "cert" for args in self.received("tailscale")))

    def test_render_failure_never_compares_or_installs_partial_configuration(self):
        checkout = self.temporary_path / "checkout"
        shutil.copytree(ROOT / "ops", checkout / "ops")
        for name, marker in (
            ("muriel.conf", "@PRODUCTION_SECONDARY_HOST@"),
            ("muriel.conf", "@PRODUCTION_PRIMARY_HOST@"),
            ("muriel-admin-proxy.conf", "@PRODUCTION_PRIMARY_HOST@"),
        ):
            template = checkout / "ops/nginx" / name
            original = template.read_text()
            for text in (
                original.replace(marker, ""),
                original + "\n@UNRESOLVED_HOST@\n",
                original + f"\n{marker}\n",
            ):
                template.write_text(text)
                for mode in ("--check", "--apply"):
                    with self.subTest(template=name, text=text[-50:], mode=mode):
                        before = self.snapshot(self.fake_root)
                        result = self.run_bootstrap(
                            mode, bootstrap=checkout / "ops/bootstrap-ubuntu"
                        )
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn("Nginx", result.stderr)
                        self.assertEqual(self.snapshot(self.fake_root), before)
                        self.assertEqual(list(self.render_scratch.iterdir()), [])
                        self.assertFalse((self.state / "group").exists())
            template.write_text(original)

    def test_openssl_hostname_checks_reject_a_wrong_san(self):
        for host, expected in (("funnel.example.test", 0), ("wrong.test", 1)):
            for arguments in (
                ["x509", "-in", self.certificate, "-noout", "-checkhost", host],
                [
                    "verify",
                    "-no-CAfile",
                    "-no-CApath",
                    "-no-CAstore",
                    "-trusted",
                    self.certificate,
                    "-partial_chain",
                    "-verify_hostname",
                    host,
                    self.certificate,
                ],
            ):
                with self.subTest(host=host, command=arguments[0]):
                    result = subprocess.run(
                        ["openssl", *map(str, arguments)],
                        stdin=subprocess.DEVNULL,
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    if expected:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unusable_existing_pairs_are_preserved_without_issuance(self):
        initial = self.run_bootstrap("--apply")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        wrong = self.make_pair("wrong-host", host="wrong.test")
        other = self.make_pair("other-key")
        expired = self.make_pair("expired", interval=(-3, -2))
        future = self.make_pair("future", interval=(2, 3))
        malformed = self.temporary_path / "malformed.pem"
        malformed.write_text("not a certificate or key\n")
        encrypted = self.temporary_path / "encrypted.key"
        self.openssl(
            "pkey", "-in", self.key, "-aes-256-cbc", "-passout", "pass:fixture", "-out", encrypted
        )
        cases = {
            "wrong host": wrong,
            "expired": expired,
            "not yet valid": future,
            "malformed certificate": (malformed, self.key),
            "malformed key": (self.certificate, malformed),
            "mismatched key": (self.certificate, other[1]),
            "password protected key": (self.certificate, encrypted),
            "certificate only": (self.certificate, None),
            "key only": (None, self.key),
        }
        for name, pair in cases.items():
            with self.subTest(case=name):
                self.install_pair(*pair)
                # Refusal must not even normalize metadata of bad material.
                self.tls_directory.chmod(0o750)
                for path in self.tls_directory.iterdir():
                    path.chmod(0o640)
                before = self.snapshot(self.tls_directory)
                issued_before = self.received("tailscale")
                for mode in ("--check", "--apply"):
                    root_before = self.snapshot(self.fake_root)
                    result = self.run_bootstrap(mode)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("explicit operator repair/renewal", result.stderr)
                    self.assertIn("funnel.example.test", result.stderr)
                    self.assertIn("docs/public-edge.md#admin-certificate-renewal", result.stderr)
                    self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(self.tls_directory), before)
                    if mode == "--check":
                        self.assertEqual(self.snapshot(self.fake_root), root_before)
                self.assertEqual(
                    [
                        args
                        for args in self.received("tailscale")[len(issued_before) :]
                        if args[0] == "cert"
                    ],
                    [],
                )

    def test_valid_rsa_and_ec_pairs_converge_without_rewriting_material(self):
        for name, pair in (
            ("EC", (self.certificate, self.key)),
            ("RSA", self.make_pair("rsa", rsa=True)),
        ):
            with self.subTest(algorithm=name):
                self.install_pair(*pair)
                paths = [self.tls_directory / name for name in ("muriel.crt", "muriel.key")]
                before = {path: self.snapshot(path) for path in paths}
                for mode in ("--apply", "--check", "--apply"):
                    result = self.run_bootstrap(mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual({path: self.snapshot(path) for path in paths}, before)
                self.assertFalse(any(args[0] == "cert" for args in self.received("tailscale")))

    def test_missing_pair_is_validated_and_only_owned_scratch_is_cleaned(self):
        self.tls_directory.mkdir(parents=True)
        retained = self.tls_directory / ".operator-material"
        retained.write_text("not bootstrap-owned\n")
        retained_before = self.snapshot(retained)
        wrong, _ = self.make_pair("issued-wrong", host="wrong.test")
        for environment in (
            {"ISSUANCE_RESULT": "failure"},
            {"ISSUANCE_RESULT": "empty"},
            {"FIXTURE_CERTIFICATE": str(wrong)},
        ):
            with self.subTest(environment=environment):
                result = self.run_bootstrap("--apply", **environment)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Admin TLS", result.stderr)
                self.assertNotIn("Bootstrap did not activate", result.stdout)
                self.assertEqual(set(self.tls_directory.iterdir()), {retained})
                self.assertEqual(self.snapshot(retained), retained_before)
                self.assertFalse((self.fake_root / "usr/sbin/policy-rc.d").exists())
                self.assertEqual(self.run_bootstrap("--check").returncode, 1)
        result = self.run_bootstrap("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            set(self.tls_directory.iterdir()),
            {
                retained,
                self.tls_directory / "muriel.crt",
                self.tls_directory / "muriel.key",
            },
        )
        for name, fixture in (("muriel.crt", self.certificate), ("muriel.key", self.key)):
            path = self.tls_directory / name
            self.assertEqual(path.read_bytes(), fixture.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tls_directory.stat().st_mode), 0o700)
        self.assertEqual(self.run_bootstrap("--check").returncode, 0)
        self.assert_no_managed_unit_mutations(self.received_commands())
        self.assert_no_nginx_mutations(self.received_commands())

    def test_scratch_cleanup_failure_reports_residue_and_cannot_succeed(self):
        self.write_executable(self.fake_bin / "rmdir", "#!/bin/sh\nexit 1\n")
        result = self.run_bootstrap("--apply")
        self.assertEqual(result.returncode, 1, result.stderr)
        scratch = [path for path in self.tls_directory.iterdir() if path.is_dir()]
        self.assertEqual(len(scratch), 1)
        self.assertIn(str(scratch[0]), result.stderr)
        self.assertIn("operator cleanup required", result.stderr)
        self.assertEqual(list(scratch[0].iterdir()), [])
        # This test's TemporaryDirectory owns and removes the deliberate residue.

    def test_unusable_openssl_never_accepts_or_issues_a_pair(self):
        self.install_pair(self.certificate, self.key)
        before = self.snapshot(self.tls_directory)
        self.write_executable(self.fake_bin / "openssl", "#!/bin/sh\nexit 127\n")
        for mode in ("--check", "--apply"):
            result = self.run_bootstrap(mode)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("OpenSSL", result.stderr)
            self.assertEqual(self.snapshot(self.tls_directory), before)
        self.assertFalse(any(args[0] == "cert" for args in self.received("tailscale")))

    def test_certificate_paths_refuse_symlinks_and_nonregular_material(self):
        for kind in ("symlink", "directory", "fifo"):
            for name in ("muriel.crt", "muriel.key"):
                with self.subTest(kind=kind, name=name):
                    self.install_pair(self.certificate, self.key)
                    path = self.tls_directory / name
                    path.unlink()
                    if kind == "symlink":
                        path.symlink_to(self.certificate)
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        os.mkfifo(path)
                    before = self.snapshot(self.tls_directory)
                    for mode in ("--check", "--apply"):
                        result = self.run_bootstrap(mode)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(self.snapshot(self.tls_directory), before)
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
        self.assertFalse(any(args[0] == "cert" for args in self.received("tailscale")))

    def test_check_reports_unsafe_certificate_metadata_read_only(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        for path in (
            self.tls_directory,
            self.tls_directory / "muriel.key",
            self.tls_directory / "muriel.crt",
        ):
            with self.subTest(path=path):
                original_mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o755)
                before = self.snapshot(self.fake_root)
                result = self.run_bootstrap("--check")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("unsafe", result.stderr)
                self.assertEqual(self.snapshot(self.fake_root), before)
                path.chmod(original_mode)

    def test_package_failure_cleans_its_policy_and_preserves_existing_policy(self):
        result = self.run_bootstrap("--apply", PACKAGE_FAILURE="yes")
        self.assertEqual(result.returncode, 1, result.stderr)
        policy = self.fake_root / "usr/sbin/policy-rc.d"
        self.assertFalse(policy.exists())
        self.assertEqual(list(self.render_scratch.iterdir()), [])
        policy.write_text("operator-owned policy\n")
        before = self.snapshot(policy)
        result = self.run_bootstrap("--apply")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("refusing to replace existing service-start policy", result.stderr)
        self.assertEqual(self.snapshot(policy), before)

    def test_has_valid_shell_syntax(self):
        result = subprocess.run(["sh", "-n", str(BOOTSTRAP)], text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normalizes_real_host_paths_with_one_leading_slash(self):
        host_path = (
            BOOTSTRAP.read_text().split("host_path() {\n", 1)[1].split("\n}\n\nfail()", 1)[0]
        )
        result = subprocess.run(
            ["sh", "-c", f"host_path() {{\n{host_path}\n}}\nroot=/\nhost_path /srv/muriel"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/srv/muriel\n")

    def test_path_guard_accepts_real_host_paths_and_contains_an_isolated_root(self):
        for path in ("/srv/muriel", "/etc/muriel"):
            result = self.run_path_guard("/", path)

            self.assertEqual(result.returncode, 0, result.stderr)

        for path in (self.fake_root, self.fake_root / "srv/muriel"):
            result = self.run_path_guard(self.fake_root, path)

            self.assertEqual(result.returncode, 0, result.stderr)

        for path in (self.fake_root.parent, Path(f"{self.fake_root}-lookalike/srv/muriel")):
            result = self.run_path_guard(self.fake_root, path)

            self.assertEqual(result.returncode, 1)

    def test_check_rejects_an_unsupported_host_without_mutating_the_fake_root(self):
        self.write_os_release("ubuntu", "24.04")

        result = self.run_bootstrap("--check")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Ubuntu 26.04", result.stderr)
        self.assertFalse(self.log.exists())

    def test_check_reports_missing_prerequisites_without_mutation(self):
        result = self.run_bootstrap("--check")

        self.assertEqual(result.returncode, 1)
        self.assertIn("OS package is missing: nginx", result.stderr)
        self.assertIn("locked-down muriel service identity is missing", result.stderr)
        commands = self.received_commands()
        self.assertIn(("systemctl", "--version"), commands)
        self.assertFalse(
            any(
                command in {"apt-get", "groupadd", "useradd", "usermod"}
                or (arguments and arguments[0] == "enable")
                for command, *arguments in commands
            )
        )
        self.assert_no_managed_unit_mutations(commands)
        self.assert_no_nginx_mutations(commands)
        self.assertFalse((self.fake_root / "srv" / "muriel").exists())

    def test_apply_generates_managed_state_and_second_run_converges(self):
        self.assertFalse(self.environment_file.exists())
        first_result = self.run_bootstrap("--apply")

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assertTrue((self.state / "package-python3.14-venv").is_file())
        self.assertTrue((self.state / "package-postgresql-18").is_file())
        self.assertTrue((self.state / "package-postgresql-client-18").is_file())
        self.assertTrue((self.state / "package-nginx").is_file())
        self.assertTrue((self.state / "group").is_file())
        self.assertTrue((self.state / "user").is_file())
        self.assertTrue((self.state / "www-data-muriel").is_file())
        installed_packages = {
            argument
            for arguments in self.received("apt-get")
            if arguments and arguments[0] == "install"
            for argument in arguments[1:]
            if not argument.startswith("-")
        }
        self.assertEqual(
            installed_packages,
            {"python3.14-venv", "postgresql-18", "postgresql-client-18", "nginx", "openssl"},
        )
        self.assertEqual(self.received("groupadd")[-1][-1], "muriel")
        useradd = self.received("useradd")[-1]
        self.assertEqual(useradd[-1], "muriel")
        self.assertIn("--system", useradd)
        self.assertEqual(useradd[useradd.index("--gid") + 1], "muriel")
        self.assertEqual(useradd[useradd.index("--home-dir") + 1], "/srv/muriel")
        self.assertEqual(useradd[useradd.index("--shell") + 1], "/usr/sbin/nologin")
        usermod = self.received("usermod")[-1]
        self.assertEqual(usermod[-1], "www-data")
        self.assertIn("--append", usermod)
        self.assertEqual(usermod[usermod.index("--groups") + 1], "muriel")
        self.assertTrue((self.fake_root / "srv/muriel/shared/pip-cache").is_dir())
        self.assertTrue((self.fake_root / "srv/muriel/shared/static").is_dir())
        service = self.fake_root / "etc/systemd/system/muriel.service"
        self.assertTrue(service.is_file())
        self.assertTrue((self.fake_root / "etc/systemd/system/muriel-backup.timer").is_file())
        self.assertTrue((self.fake_root / "srv/muriel/backup-database").is_file())
        self.assertTrue((self.fake_root / "srv/muriel/create-superuser").is_file())
        self.assertTrue((self.fake_root / "srv/muriel/clear-production-login-access").is_file())
        self.assertTrue((self.fake_root / "srv/muriel/reset-production-domain").is_file())
        self.assertTrue((self.fake_root / "etc/nginx/snippets/muriel-proxy.conf").is_file())
        self.assertTrue((self.fake_root / "etc/nginx/snippets/muriel-public-app.conf").is_file())
        self.assertTrue((self.fake_root / "etc/nginx/tailscale/muriel.crt").is_file())
        nginx_link = self.fake_root / "etc/nginx/sites-enabled/muriel"
        self.assertEqual(os.readlink(nginx_link), "../sites-available/muriel")
        unit = ConfigParser(interpolation=None)
        unit.read(service)
        self.assertEqual(
            {
                key: unit["Service"][key]
                for key in ("User", "Group", "EnvironmentFile", "ExecStart", "RuntimeDirectory")
            },
            {
                "User": "muriel",
                "Group": "muriel",
                "EnvironmentFile": "/etc/muriel/environment",
                "ExecStart": "/srv/muriel/current/scripts/start-production",
                "RuntimeDirectory": "muriel",
            },
        )
        nginx_site = re.sub(
            r"#.*$",
            "",
            (self.fake_root / "etc/nginx/sites-available/muriel").read_text(),
            flags=re.MULTILINE,
        )
        # These named blocks contain no nested braces, so scoped extraction is
        # sufficient here without treating this fixture assertion as an Nginx parser.
        public_server = re.search(
            r"server\s*\{(?=[^{}]*\blisten\s+127\.0\.0\.1:8080\s+proxy_protocol;)"
            r"(?=[^{}]*\bserver_name\s+funnel\.example\.test;)[^{}]*\}",
            nginx_site,
            re.DOTALL,
        )
        self.assertIsNotNone(public_server)
        static_location = re.search(
            r"location\s+\^~\s+/static/\s*\{[^{}]*\}",
            re.sub(
                r"#.*$",
                "",
                (self.fake_root / "etc/nginx/snippets/muriel-public-app.conf").read_text(),
                flags=re.MULTILINE,
            ),
            re.DOTALL,
        )
        self.assertIsNotNone(static_location)
        self.assertRegex(
            public_server.group(),
            r"\blisten\s+127\.0\.0\.1:8080\s+proxy_protocol;",
        )
        self.assertRegex(
            static_location.group(),
            r"\balias\s+/srv/muriel/current/staticfiles/;",
        )
        self.assertIn(("nginx", "-t"), self.received_commands())
        tailscale_commands = self.received("tailscale")
        self.assertTrue(tailscale_commands)
        self.assertTrue(
            all(arguments[0] in {"cert", "status", "version"} for arguments in tailscale_commands)
        )
        self.assertFalse(any("funnel" in arguments for arguments in tailscale_commands))
        self.assertIn(
            ("systemctl", "enable", "--now", "postgresql@18-main"), self.received_commands()
        )
        self.assert_no_managed_unit_mutations(self.received_commands())
        self.assert_no_nginx_mutations(self.received_commands())
        self.assertIn("disabled and inactive", first_result.stdout)

        managed_paths = (
            self.fake_root / "etc/systemd/system/muriel.service",
            self.fake_root / "etc/nginx/sites-available/muriel",
            self.fake_root / "etc/nginx/snippets/muriel-public-app.conf",
            self.fake_root / "etc/nginx/tailscale/muriel.crt",
            self.fake_root / "etc/nginx/tailscale/muriel.key",
        )
        managed_before_check = {path: path.read_bytes() for path in managed_paths}
        state_before_check = sorted(path.name for path in self.state.iterdir())
        command_count_before_check = len(self.received_commands())
        checked_result = self.run_bootstrap("--check")

        self.assertEqual(checked_result.returncode, 0, checked_result.stderr)
        self.assertEqual({path: path.read_bytes() for path in managed_paths}, managed_before_check)
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), state_before_check)
        check_commands = self.received_commands()[command_count_before_check:]
        self.assert_no_managed_unit_mutations(check_commands)
        self.assert_no_nginx_mutations(check_commands)
        second_result = self.run_bootstrap("--apply")
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        self.assertFalse(self.environment_file.exists())
        for repeated_operation in (
            "repeated-group-create",
            "repeated-user-create",
            "repeated-role-create",
            "repeated-database-create",
        ):
            self.assertFalse((self.state / repeated_operation).exists())

    def test_check_rejects_a_generated_service_with_the_wrong_launch_identity(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        service = self.fake_root / "etc/systemd/system/muriel.service"
        service.write_text(service.read_text().replace("User=muriel", "User=muriel_wrong"))

        result = self.run_bootstrap("--check")

        self.assertEqual(result.returncode, 1)
        self.assertIn(str(service), result.stderr)

    def test_check_detects_a_divergent_sensitive_request_rate(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        site = self.fake_root / "etc/nginx/sites-available/muriel"

        self.assertIn("zone=muriel_sensitive:10m rate=1r/s;", site.read_text())
        site.write_text(site.read_text().replace("rate=1r/s;", "rate=5r/m;"))

        result = self.run_bootstrap("--check")

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"managed file differs or is missing: {site}", result.stderr)

    def test_removes_the_exact_stock_nginx_site_link_and_then_converges(self):
        self.enable_stock_nginx_site()
        stock_link = self.fake_root / "etc/nginx/sites-enabled/default"

        checked_result = self.run_bootstrap("--check")
        self.assertTrue(stock_link.is_symlink())
        applied_result = self.run_bootstrap("--apply")
        converged_result = self.run_bootstrap("--check")

        self.assertEqual(checked_result.returncode, 1)
        self.assertIn("Nginx stock default site is enabled", checked_result.stderr)
        self.assertEqual(applied_result.returncode, 0, applied_result.stderr)
        self.assertFalse(stock_link.exists() or stock_link.is_symlink())
        self.assertEqual(converged_result.returncode, 0, converged_result.stderr)

    def test_removes_the_absolute_stock_nginx_site_link(self):
        self.enable_stock_nginx_site("/etc/nginx/sites-available/default")
        stock_link = self.fake_root / "etc/nginx/sites-enabled/default"

        result = self.run_bootstrap("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stock_link.exists() or stock_link.is_symlink())

    def test_refuses_divergent_nginx_default_site_occupants(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        stock_link = self.fake_root / "etc/nginx/sites-enabled/default"

        for kind in ("regular file", "different symlink", "directory"):
            with self.subTest(kind=kind):
                if kind == "regular file":
                    stock_link.write_text("unrecognized site\n")
                elif kind == "different symlink":
                    stock_link.symlink_to("../sites-available/another-site")
                else:
                    stock_link.mkdir()

                checked_result = self.run_bootstrap("--check")
                applied_result = self.run_bootstrap("--apply")

                self.assertEqual(checked_result.returncode, 1)
                self.assertIn(
                    "Nginx default site is divergent and will not be changed", checked_result.stderr
                )
                self.assertEqual(applied_result.returncode, 1)
                self.assertIn(
                    "Nginx default site is divergent and will not be changed", applied_result.stderr
                )
                if kind == "directory":
                    self.assertTrue(stock_link.is_dir())
                    stock_link.rmdir()
                else:
                    self.assertTrue(stock_link.exists() or stock_link.is_symlink())
                    stock_link.unlink()

    def test_apply_reports_an_enabled_active_muriel_service_after_maintenance(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        helper = self.fake_root / "srv/muriel/restore-database"
        helper.write_text("older Muriel helper\n")

        result = self.run_bootstrap("--apply", MURIEL_ENABLED="enabled", MURIEL_ACTIVE="active")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"managed file differs or is missing: {helper}",
            result.stderr,
        )
        self.assertIn("Muriel is enabled and active", result.stdout)
        self.assert_no_managed_unit_mutations(self.received_commands())
        self.assert_no_nginx_mutations(self.received_commands())
        self.assertEqual(self.run_bootstrap("--check").returncode, 0)

    def test_apply_uses_the_18_main_cluster_when_postgresql_16_is_retained(self):
        (self.state / "retained-postgresql-16").touch()

        result = self.run_bootstrap("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(("enable", "--now", "postgresql@18-main"), self.received("systemctl"))
        postgres_queries = [
            arguments for arguments in self.received("runuser") if "psql" in arguments
        ]
        self.assertTrue(postgres_queries)
        for arguments in postgres_queries:
            self.assertIn("-u", arguments)
            self.assertEqual(arguments[arguments.index("-u") + 1], "postgres")
            self.assertEqual(self.option_value(arguments, "--cluster"), "18/main")
            self.assertEqual(self.option_value(arguments, "--host"), "/var/run/postgresql")
            self.assertEqual(self.option_value(arguments, "--port"), "5432")
        self.assertNotIn(("enable", "--now", "postgresql"), self.received("systemctl"))

    def test_apply_atomically_replaces_a_divergent_regular_managed_file(self):
        first_result = self.run_bootstrap("--apply")
        helper = self.fake_root / "srv/muriel/create-superuser"
        helper.write_text("older Muriel helper\n")

        checked_result = self.run_bootstrap("--check")
        repeated_result = self.run_bootstrap("--apply")

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assertEqual(checked_result.returncode, 1)
        self.assertIn("managed file differs or is missing", checked_result.stderr)
        self.assertEqual(repeated_result.returncode, 0, repeated_result.stderr)
        self.assertEqual(helper.read_text(), (ROOT / "ops/create-superuser").read_text())
        self.assertEqual(self.run_bootstrap("--check").returncode, 0)

    def test_check_rejects_a_postgresql_18_connection_layout_that_conflicts_with_clients(self):
        (self.state / "package-postgresql-18").touch()
        (self.state / "postgresql-18-running").touch()

        result = self.run_bootstrap("--check", POSTGRES_PORT="5433")

        self.assertEqual(result.returncode, 1)
        self.assertIn("/var/run/postgresql port 5432", result.stderr)

    def test_check_rejects_the_socket_endpoint_when_16_main_occupies_it(self):
        (self.state / "package-postgresql-18").touch()
        (self.state / "postgresql-18-running").touch()
        (self.state / "retained-postgresql-16").touch()

        result = self.run_bootstrap("--check", POSTGRES_ENDPOINT_CLUSTER="16/main")

        self.assertEqual(result.returncode, 1)
        self.assertIn("another cluster may occupy this endpoint", result.stderr)

    def test_rejects_an_existing_role_with_a_password_despite_the_masked_public_view(self):
        first_result = self.run_bootstrap("--apply")
        (self.state / "role-password").touch()

        checked_result = self.run_bootstrap("--check")
        repeated_result = self.run_bootstrap("--apply")

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assertEqual(checked_result.returncode, 1)
        self.assertIn(
            "least-privileged PostgreSQL role muriel is missing or divergent", checked_result.stderr
        )
        self.assertEqual(repeated_result.returncode, 1)
        self.assertIn("PostgreSQL role muriel", repeated_result.stderr)
        self.assertEqual(
            sum("CREATE ROLE muriel LOGIN" in arguments for arguments in self.received("runuser")),
            1,
        )

    def test_rejects_an_existing_role_with_database_creation_authority(self):
        first_result = self.run_bootstrap("--apply")
        (self.state / "role-createdb").touch()

        checked_result = self.run_bootstrap("--check")
        repeated_result = self.run_bootstrap("--apply")

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assertEqual(checked_result.returncode, 1)
        self.assertIn("least-privileged PostgreSQL role muriel", checked_result.stderr)
        self.assertEqual(repeated_result.returncode, 1)
        self.assertIn("without CREATEDB", repeated_result.stderr)

    def test_rejects_a_symlinked_fake_root_before_running_commands(self):
        symlink_root = self.temporary_path / "unsafe-root"
        symlink_root.symlink_to(self.fake_root)

        result = self.run_bootstrap("--check", root=symlink_root)

        self.assertEqual(result.returncode, 2)
        self.assertIn("non-symlink directory", result.stderr)
        self.assertFalse(self.log.exists())

    def test_rejects_a_symlinked_managed_path_before_apply_mutation(self):
        for package in (
            "python3.14-venv",
            "postgresql-18",
            "postgresql-client-18",
            "nginx",
            "openssl",
        ):
            (self.state / f"package-{package}").touch()
        escaped_directory = self.temporary_path / "outside-root"
        escaped_directory.mkdir()
        (self.fake_root / "etc" / "nginx").symlink_to(escaped_directory, target_is_directory=True)

        result = self.run_bootstrap("--apply")

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing symlinked managed path", result.stderr)
        self.assertFalse((self.fake_root / "srv" / "muriel").exists())
        self.assertFalse(any(command == "apt-get" for command, *_ in self.received_commands()))

    def test_rejects_a_nonregular_managed_file_before_apply_mutation(self):
        for package in (
            "python3.14-venv",
            "postgresql-18",
            "postgresql-client-18",
            "nginx",
            "openssl",
        ):
            (self.state / f"package-{package}").touch()
        (self.fake_root / "etc/systemd/system/muriel.service").mkdir()

        result = self.run_bootstrap("--apply")

        self.assertEqual(result.returncode, 1)
        self.assertIn("managed file is not a regular file", result.stderr)
        self.assertFalse((self.fake_root / "srv" / "muriel").exists())
        self.assertFalse(any(command == "apt-get" for command, *_ in self.received_commands()))


class StagingHostBootstrapTests(SimpleTestCase):
    def setUp(self):
        self.host = "staging.example.test"
        self.origin = f"https://{self.host}:10000"
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "host"
        self.fake_bin = self.temporary_path / "bin"
        self.state = self.temporary_path / "state"
        self.log = self.temporary_path / "commands.log"
        self.root.mkdir()
        self.fake_bin.mkdir()
        self.state.mkdir()
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.write_dispatcher()

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            """#!/bin/sh
set -eu
command=$(basename "$0")
state=$FAKE_STAGING_STATE
printf '%s\\0' "$command" "$@" '' >> "$FAKE_STAGING_COMMANDS"
case "$command" in
    getent)
        case "$1:$2" in
            group:muriel_staging)
                [ -f "$state/group" ] || exit 2
                printf 'muriel_staging:x:999:\\n'
                ;;
            passwd:muriel_staging)
                [ -f "$state/user" ] || exit 2
                IFS=: read -r identity group home shell < "$state/user"
                [ "$identity" = muriel_staging ] || exit 2
                printf '%s:x:999:999::%s:%s\\n' "$identity" "$home" "$shell"
                ;;
        esac
        ;;
    groupadd)
        for argument; do group=$argument; done
        [ "$group" = muriel_staging ] || exit 1
        touch "$state/group"
        ;;
    useradd)
        previous=
        group=
        home=
        shell=
        for argument; do
            case "$previous" in
                --gid) group=$argument; previous=; continue ;;
                --home-dir) home=$argument; previous=; continue ;;
                --shell) shell=$argument; previous=; continue ;;
            esac
            case "$argument" in
                --gid|--home-dir|--shell) previous=$argument ;;
                --gid=*) group=${argument#--gid=} ;;
                --home-dir=*) home=${argument#--home-dir=} ;;
                --shell=*) shell=${argument#--shell=} ;;
                *) user=$argument ;;
            esac
        done
        [ "$user" = muriel_staging ] && [ "$group" = muriel_staging ] || exit 1
        printf '%s:%s:%s:%s\\n' "$user" "$group" "$home" "$shell" > "$state/user"
        ;;
    runuser)
        for argument; do query=$argument; done
        case "$query" in
            *"FROM pg_authid"*) [ -f "$state/role" ] && printf 'true:true:false:false\\n' ;;
            *"FROM pg_database"*) [ -f "$state/database" ] && printf 'muriel_staging\\n' ;;
            "CREATE ROLE muriel_staging LOGIN") touch "$state/role" ;;
            "CREATE DATABASE muriel_staging OWNER muriel_staging") touch "$state/database" ;;
        esac
        exit 0
        ;;
    systemctl) printf 'systemctl %s\\n' "$*" >> "$FAKE_STAGING_LOG" ;;
    chown) printf 'chown %s\\n' "$*" >> "$FAKE_STAGING_LOG" ;;
    stat)
        for argument; do path=$argument; done
        case "$path" in
            */etc/muriel-staging/environment)
                case "${STAGING_ENVIRONMENT_METADATA:-safe}" in
                    safe) value=root:muriel_staging:640 ;;
                    *) value=$STAGING_ENVIRONMENT_METADATA ;;
                esac
                ;;
            */srv/muriel-staging) value=root:muriel_staging:750 ;;
            */srv/muriel-staging/releases|*/srv/muriel-staging/shared|*/srv/muriel-staging/shared/static) value=muriel_staging:muriel_staging:750 ;;
            */srv/muriel-staging/shared/pip-cache) value=muriel_staging:muriel_staging:700 ;;
            */etc/muriel-staging) value=root:muriel_staging:750 ;;
            */etc/systemd/system/muriel-staging.service) value=root:root:644 ;;
            */srv/muriel-staging/deploy-release|*/srv/muriel-staging/create-superuser) value=root:root:755 ;;
            *) exit 1 ;;
        esac
        printf '%s\\n' "$value"
        ;;
esac
""",
        )
        for command in ("chown", "getent", "groupadd", "runuser", "stat", "systemctl", "useradd"):
            (self.fake_bin / command).symlink_to(dispatcher.name)
        for command in ("mkdir", "chmod", "cp", "mv", "mktemp", "od", "rm"):
            self.write_executable(
                self.fake_bin / command,
                "#!/bin/sh\n"
                f"printf '%s\\0' {command} \"$@\" '' >> \"$FAKE_STAGING_COMMANDS\"\n"
                f'exec {shlex.quote(shutil.which(command))} "$@"\n',
            )

    def run_bootstrap(self, mode, *, dispatch=False, **environment):
        command = (
            [str(BOOTSTRAP), "--instance", "staging"] if dispatch else [str(STAGING_BOOTSTRAP)]
        )
        return subprocess.run(
            [*command, mode, "--root", str(self.root)],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "FAKE_STAGING_LOG": str(self.log),
                "FAKE_STAGING_COMMANDS": str(self.temporary_path / "commands.bin"),
                "FAKE_STAGING_STATE": str(self.state),
                "PRECEPTOR_STAGING_ALLOWED_HOST": self.host,
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": self.origin,
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    @property
    def environment_file(self):
        return self.root / "etc/muriel-staging/environment"

    def received_commands(self):
        command_log = self.temporary_path / "commands.bin"
        if not command_log.exists():
            return []
        return [
            tuple(part.decode() for part in record.split(b"\0") if part)
            for record in command_log.read_bytes().split(b"\0\0")
            if record
        ]

    def write_environment(self, **overrides):
        values = {
            "PRECEPTOR_STAGING_SECRET_KEY": "fixture-secret-" + "x" * 50,
            "PRECEPTOR_STAGING_ALLOWED_HOST": self.host,
            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": self.origin,
            "PRECEPTOR_STAGING_POSTGRES_NAME": "muriel_staging",
            "PRECEPTOR_STAGING_POSTGRES_USER": "muriel_staging",
        }
        values.update(overrides)
        self.environment_file.parent.mkdir(parents=True, exist_ok=True)
        self.environment_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
        )
        self.environment_file.chmod(0o640)

    def filesystem_snapshot(self, *roots):
        snapshot = {}
        for root in roots:
            for path in (root, *root.rglob("*")):
                metadata = path.lstat()
                snapshot[path] = (
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_gid,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    os.readlink(path)
                    if path.is_symlink()
                    else path.read_bytes()
                    if path.is_file()
                    else None,
                )
        return snapshot

    def assert_no_mutation_calls(self, commands):
        for command, *arguments in commands:
            if command == "runuser":
                self.assertEqual(arguments[:3], ["-u", "postgres", "--"])
                self.assertTrue(arguments[-1].startswith("SELECT "), arguments)
            else:
                self.assertIn(command, {"getent", "stat"}, (command, arguments))

    def assert_read_only_refusal(self, mode, diagnostic, *extra_roots, **environment):
        before = self.filesystem_snapshot(self.root, self.state, *extra_roots)
        command_count = len(self.received_commands())
        result = self.run_bootstrap(mode, **environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertEqual(self.filesystem_snapshot(self.root, self.state, *extra_roots), before)
        self.assert_no_mutation_calls(self.received_commands()[command_count:])
        self.assertNotIn("fixture-secret-", result.stdout + result.stderr)
        self.assertNotIn("PRECEPTOR_STAGING_SECRET_KEY=", result.stdout + result.stderr)

    def test_apply_creates_a_secure_isolated_environment_and_converges(self):
        for host in ("staging.example.test", "trial-2.residency.test"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "host"
                self.state = Path(directory) / "state"
                self.root.joinpath("etc/systemd/system").mkdir(parents=True)
                self.state.mkdir()
                self.host = host
                self.origin = f"https://{host}:10000"
                self.assert_configured_bootstrap_converges()

    def assert_configured_bootstrap_converges(self):
        first_result = self.run_bootstrap("--apply", dispatch=True)

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assertIn(("groupadd", "--system", "muriel_staging"), self.received_commands())
        useradd = next(
            arguments for command, *arguments in self.received_commands() if command == "useradd"
        )
        self.assertEqual(useradd[-1], "muriel_staging")
        self.assertIn("--system", useradd)
        self.assertEqual(useradd[useradd.index("--gid") + 1], "muriel_staging")
        self.assertEqual(useradd[useradd.index("--home-dir") + 1], "/srv/muriel-staging")
        self.assertEqual(useradd[useradd.index("--shell") + 1], "/usr/sbin/nologin")
        self.assertNotIn("PRECEPTOR_STAGING_SECRET_KEY=", first_result.stdout + first_result.stderr)
        self.assertEqual(self.environment_file.stat().st_mode & 0o777, 0o640)
        environment = dict(
            line.split("=", 1) for line in self.environment_file.read_text().splitlines() if line
        )
        self.assertGreaterEqual(len(environment["PRECEPTOR_STAGING_SECRET_KEY"]), 50)
        self.assertTrue(
            {
                "PRECEPTOR_STAGING_ALLOWED_HOST": self.host,
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": self.origin,
                "PRECEPTOR_STAGING_POSTGRES_NAME": "muriel_staging",
                "PRECEPTOR_STAGING_POSTGRES_USER": "muriel_staging",
            }.items()
            <= environment.items()
        )
        settings_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; from django.conf import settings; "
                    "print(json.dumps({'allowed_host': settings.STAGING_ALLOWED_HOST, "
                    "'database_name': settings.DATABASES['default']['NAME'], "
                    "'database_user': settings.DATABASES['default']['USER'], "
                    "'csrf_trusted_origin': settings.CSRF_TRUSTED_ORIGINS[0]}, sort_keys=True))"
                ),
            ],
            cwd=ROOT,
            env=os.environ
            | environment
            | {"DJANGO_SETTINGS_MODULE": "preceptor_project.staging_settings"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(settings_probe.returncode, 0, settings_probe.stderr)
        self.assertEqual(
            json.loads(settings_probe.stdout),
            {
                "allowed_host": self.host,
                "csrf_trusted_origin": self.origin,
                "database_name": "muriel_staging",
                "database_user": "muriel_staging",
            },
        )
        self.assertIn("chown root:muriel_staging", self.log.read_text())
        original = self.filesystem_snapshot(self.environment_file)

        before_check = self.filesystem_snapshot(self.root, self.state)
        command_count = len(self.received_commands())
        checked_result = self.run_bootstrap("--check")
        self.assertEqual(checked_result.returncode, 0, checked_result.stderr)
        self.assertEqual(self.filesystem_snapshot(self.root, self.state), before_check)
        self.assert_no_mutation_calls(self.received_commands()[command_count:])

        # Reconcile a missing database and divergent directory permissions while
        # keeping the valid environment, including its inode and metadata, intact.
        (self.state / "database").unlink()
        cache = self.root / "srv/muriel-staging/shared/pip-cache"
        cache.chmod(0o755)
        command_count = len(self.received_commands())
        second_result = self.run_bootstrap("--apply")

        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        self.assertEqual(self.filesystem_snapshot(self.environment_file), original)
        self.assertTrue((self.state / "database").exists())
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("od", [command[0] for command in self.received_commands()[command_count:]])

    def test_explicit_identity_refuses_before_host_operations_including_dispatch(self):
        for host, origin in INVALID_STAGING_PAIRS:
            for dispatch in (False, True):
                for mode in ("--check", "--apply"):
                    with self.subTest(host=host, origin=origin, dispatch=dispatch, mode=mode):
                        before = self.filesystem_snapshot(self.root, self.state)
                        # Empty and absent are equivalent at this shell boundary; the
                        # run helper overrides any operator-inherited selection.
                        result = self.run_bootstrap(
                            mode,
                            dispatch=dispatch,
                            PRECEPTOR_STAGING_ALLOWED_HOST=host or "",
                            PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=origin or "",
                        )
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("Staging identity requires", result.stderr)
                        self.assertEqual(self.received_commands(), [])
                        self.assertEqual(self.filesystem_snapshot(self.root, self.state), before)

    def test_selected_identity_cannot_adopt_or_rewrite_existing_identity(self):
        self.write_environment()
        for mode in ("--check", "--apply"):
            self.assert_read_only_refusal(
                mode,
                "staging environment file is divergent or unsafe",
                PRECEPTOR_STAGING_ALLOWED_HOST="other.test",
                PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN="https://other.test:10000",
            )

    def test_check_reports_missing_environment_without_creating_it(self):
        for mode in ("--check", "--dry-run"):
            with self.subTest(mode=mode):
                self.assert_read_only_refusal(mode, "staging environment file is missing")
                self.assertFalse(self.environment_file.exists())

    def test_existing_environment_is_preserved_when_its_metadata_diverges(self):
        self.write_environment()
        # The fake host has no identity/database/helpers, and a directory that
        # apply would otherwise chmod. Ownership is simulated by the stat adapter.
        self.environment_file.parent.chmod(0o777)
        for metadata in ("root:muriel_staging:600", "nobody:muriel_staging:640", "root:root:640"):
            self.environment_file.chmod(int(metadata.rsplit(":", 1)[1], 8))
            for mode in ("--apply", "--check"):
                with self.subTest(metadata=metadata, mode=mode):
                    self.assert_read_only_refusal(
                        mode,
                        "staging environment file is divergent or unsafe",
                        STAGING_ENVIRONMENT_METADATA=metadata,
                    )

    def test_rejects_divergent_environment_values_before_host_mutation(self):
        cases = {
            "PRECEPTOR_STAGING_POSTGRES_NAME": "muriel",
            "PRECEPTOR_STAGING_POSTGRES_USER": "muriel",
            "PRECEPTOR_STAGING_ALLOWED_HOST": "other.example.org",
            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": f"https://{self.host}",
            "PRECEPTOR_STAGING_SECRET_KEY": "too-short",
        }
        for key, value in cases.items():
            self.write_environment(**{key: value})
            for mode in ("--apply", "--check"):
                with self.subTest(key=key, mode=mode):
                    self.assert_read_only_refusal(
                        mode, "staging environment file is divergent or unsafe"
                    )

    def test_environment_is_not_sourced_as_shell_configuration(self):
        self.write_environment()
        with self.environment_file.open("a") as environment:
            environment.write(f"touch {shlex.quote(str(self.root / 'sourced'))}\n")
        self.assert_read_only_refusal("--apply", "staging environment file is divergent or unsafe")

    def test_refuses_symlinked_and_nonregular_environment_paths_without_mutation(self):
        target = self.temporary_path / "target"
        target.mkdir()
        (target / "environment").write_text("fixture-secret-" + "x" * 50)
        for kind in ("leaf", "dangling", "parent", "missing-via-parent", "directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.environment_file.parent.mkdir(parents=True)
                diagnostic = "staging environment file is divergent or unsafe"
                if kind in ("parent", "missing-via-parent"):
                    self.environment_file.parent.rmdir()
                    destination = target if kind == "parent" else target / "absent"
                    self.environment_file.parent.symlink_to(destination)
                    diagnostic = "refusing symlinked managed path"
                elif kind in ("leaf", "dangling"):
                    destination = target / ("environment" if kind == "leaf" else "absent")
                    self.environment_file.symlink_to(destination)
                    diagnostic = "refusing symlinked managed path"
                elif kind == "directory":
                    self.environment_file.mkdir()
                else:
                    os.mkfifo(self.environment_file)
                for mode in ("--apply", "--check"):
                    self.assert_read_only_refusal(mode, diagnostic, target)
