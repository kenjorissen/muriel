import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from configparser import ConfigParser
from pathlib import Path

from django.test import SimpleTestCase

from .production_identity_fixtures import INVALID_PRODUCTION_PAIRS, PRODUCTION_IDENTITY

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "ops" / "deploy"
DEPLOY_RELEASE = ROOT / "ops" / "deploy-release"
UPDATE_PRODUCTION = ROOT / "ops" / "update-production"
UPDATE_PRODUCTION_REMOTE = ROOT / "ops" / "update-production-remote"
RESTORE = ROOT / "ops" / "restore-database"
SERVICE_HELPERS = ("status", "logs", "restart", "status-staging", "logs-staging")


class DeploymentCommandTests(SimpleTestCase):
    commit = "a" * 40

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "checkout"
        self.fake_bin = self.temporary_path / "bin"
        self.log = self.temporary_path / "commands.log"
        self.upload = self.temporary_path / "uploaded.tar"
        self.local_log = self.temporary_path / "local-commands.log"
        self.archives = self.temporary_path / "archives"
        self.archives.mkdir()
        self.remote_deploy_release = self.fake_bin / "deploy-release"
        token = "".join(character for character in self.temporary_path.name if character.isalnum())[
            -6:
        ]
        self.remote_archive = f"/tmp/muriel-{self.commit}.{token}.tar"
        self.addCleanup(Path(self.remote_archive).unlink, missing_ok=True)
        self.root.mkdir()
        self.fake_bin.mkdir()
        (self.root / "ops").mkdir()
        shutil.copy2(DEPLOY, self.root / "ops" / "deploy")
        shutil.copy2(ROOT / "ops" / "require-ssh-target.sh", self.root / "ops")
        self.write_executable(
            self.fake_bin / "mktemp",
            '#!/bin/sh\nprintf "mktemp\\n" >> "$DEPLOY_LOCAL_LOG"\n'
            f'exec {shlex.quote(shutil.which("mktemp"))} "$@"\n',
        )
        self.write_executable(
            self.fake_bin / "git",
            """#!/bin/sh
set -eu
printf '%s\\n' git "$@" >> "$DEPLOY_LOCAL_LOG"
while [ "${1:-}" = -C ]; do shift 2; done
case "$1" in
    status) printf '%s' "${GIT_STATUS:-}" ;;
    rev-parse) printf '%s\\n' "$DEPLOY_COMMIT" ;;
    archive) cat "$DEPLOY_ARTIFACT" ;;
    *) exit 1 ;;
esac
""",
        )
        self.write_executable(
            self.fake_bin / "ssh",
            """#!/bin/sh
set -eu
printf '%s\\n' ssh >> "$DEPLOY_LOG"
for argument; do printf '%s\\n' "$argument" >> "$DEPLOY_LOG"; done
case "$*" in
    *"mktemp --suffix=.tar"*)
        [ "${DEPLOY_FAIL:-}" != stage ] || exit 1
        printf '%s\\n' "$DEPLOY_REMOTE_ARCHIVE"
        ;;
    *"sudo /srv/muriel/deploy-release"*)
        [ "${DEPLOY_FAIL:-}" != activate ] || exit 1
        stdin_bytes=$(wc -c < /dev/stdin)
        printf 'activate-stdin=%s\\n' "$stdin_bytes" >> "$DEPLOY_LOG"
        [ "$stdin_bytes" -eq 0 ] || exit 1
        for argument; do command=$argument; done
        safe_command=$(printf '%s' "$command" | sed "s|/srv/muriel/deploy-release|$DEPLOY_REMOTE_DEPLOY_RELEASE|g")
        sh -c "$safe_command"
        ;;
    *"rm -f --"*)
        [ "${DEPLOY_FAIL:-}" != cleanup ] || exit 1
        for argument; do command=$argument; done
        "$DEPLOY_FIXTURE_PYTHON" -c '
import os
import shlex
import sys
from pathlib import Path

received = shlex.split(sys.argv[1])
expected = ["rm", "-f", "--", os.environ["DEPLOY_REMOTE_ARCHIVE"]]
if received != expected:
    raise SystemExit(1)
Path(expected[-1]).unlink(missing_ok=True)
' "$command"
        ;;
esac
""",
        )
        self.write_executable(
            self.fake_bin / "scp",
            """#!/bin/sh
set -eu
printf '%s\\n' scp >> "$DEPLOY_LOG"
for argument; do printf '%s\\n' "$argument" >> "$DEPLOY_LOG"; done
stat -c 'mode=%a' "$1" >> "$DEPLOY_LOG"
[ "${DEPLOY_FAIL:-}" != transfer ] || exit 1
cp "$1" "$DEPLOY_UPLOAD"
cp "$1" "$DEPLOY_REMOTE_ARCHIVE"
""",
        )
        self.write_executable(
            self.remote_deploy_release,
            """#!/bin/sh
set -eu
[ "$#" -eq 1 ]
printf 'deploy-release:%s\\n' "$1" >> "$DEPLOY_LOG"
tar -tf - >> "$DEPLOY_LOG"
""",
        )
        self.write_executable(self.fake_bin / "sudo", '#!/bin/sh\nset -eu\nexec "$@"\n')

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def create_artifact(self, *paths):
        source = self.temporary_path / "artifact-source"
        source.mkdir()
        for path in paths:
            file_path = source / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(path)
        artifact = self.temporary_path / "source.tar"
        with tarfile.open(artifact, "w") as tar:
            for path in source.rglob("*"):
                tar.add(path, path.relative_to(source))
        return artifact

    def run_deploy(self, arguments=("release-a",), **environment):
        env = os.environ | {
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "MURIEL_SSH_TARGET": "release-a",
            "TMPDIR": str(self.archives),
            "DEPLOY_COMMIT": self.commit,
            "DEPLOY_LOG": str(self.log),
            "DEPLOY_LOCAL_LOG": str(self.local_log),
            "DEPLOY_FIXTURE_PYTHON": sys.executable,
            "DEPLOY_UPLOAD": str(self.upload),
            "DEPLOY_REMOTE_ARCHIVE": self.remote_archive,
            "DEPLOY_REMOTE_DEPLOY_RELEASE": str(self.remote_deploy_release),
            **environment,
        }
        return subprocess.run(
            [str(self.root / "ops" / "deploy"), *arguments],
            cwd=self.temporary_path,
            env={key: value for key, value in env.items() if value is not None},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_transfers_a_clean_bounded_artifact_then_uses_tty_sudo(self):
        artifact = self.create_artifact("manage.py", "preceptor_project/settings.py")

        for target in ("release-a", "2.release_B"):
            with self.subTest(target=target):
                self.log.unlink(missing_ok=True)
                result = self.run_deploy(
                    (target,), MURIEL_SSH_TARGET=target, DEPLOY_ARTIFACT=str(artifact)
                )
                self.assert_successful_deployment(result, target)

    def assert_successful_deployment(self, result, target):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.commit, result.stdout)
        commands = self.log.read_text().splitlines()
        first_ssh = commands.index("ssh")
        self.assertEqual(commands[first_ssh + 1], target)
        staging_argv = shlex.split(commands[first_ssh + 2])
        self.assertEqual(staging_argv[:3], ["umask", "077", "&&"])
        self.assertIn("mktemp", staging_argv)
        self.assertIn(f"/tmp/muriel-{self.commit}.XXXXXX", staging_argv)
        scp = commands.index("scp")
        local_archive, remote_archive = commands[scp + 1 : scp + 3]
        self.assertTrue(local_archive.endswith(".tar"))
        self.assertEqual(remote_archive, f"{target}:{self.remote_archive}")
        self.assertIn("mode=600", commands)
        self.assertFalse(Path(local_archive).exists())
        activation = commands.index("ssh", scp + 1)
        self.assertEqual(commands[activation + 1 : activation + 3], ["-tt", target])
        activation_argv = shlex.split(commands[activation + 3])
        self.assertEqual(activation_argv[:3], ["sudo", "/srv/muriel/deploy-release", self.commit])
        self.assertEqual(activation_argv[3:], ["<", self.remote_archive])
        self.assertIn("activate-stdin=0", commands)
        self.assertIn(f"deploy-release:{self.commit}", commands)
        self.assertIn("manage.py", commands)
        cleanup = commands.index("ssh", activation + 1)
        self.assertEqual(commands[cleanup + 1], target)
        self.assertEqual(shlex.split(commands[cleanup + 2])[:3], ["rm", "-f", "--"])
        self.assertIn(self.remote_archive, shlex.split(commands[cleanup + 2]))
        self.assertFalse(Path(self.remote_archive).exists())
        with tarfile.open(self.upload) as uploaded:
            self.assertEqual(
                set(uploaded.getnames()),
                {"manage.py", "preceptor_project", "preceptor_project/settings.py"},
            )

    def test_stage_transfer_activation_and_cleanup_failures_remain_failures(self):
        artifact = self.create_artifact("manage.py")

        for failure in ("stage", "transfer", "activate", "cleanup"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result = self.run_deploy(DEPLOY_ARTIFACT=str(artifact), DEPLOY_FAIL=failure)

                self.assertEqual(result.returncode, 1)
                self.assertEqual(list(self.archives.iterdir()), [])
                commands = self.log.read_text().splitlines()
                remote_commands = []
                for index, command in enumerate(commands):
                    if command != "ssh":
                        continue
                    argument_index = index + 1
                    if commands[argument_index] == "-tt":
                        argument_index += 1
                    self.assertEqual(commands[argument_index], "release-a")
                    remote_commands.append(shlex.split(commands[argument_index + 1]))
                self.assertTrue(remote_commands)
                self.assertEqual(remote_commands[0][:3], ["umask", "077", "&&"])
                if failure == "activate":
                    activation = next(
                        command for command in remote_commands if command[0] == "sudo"
                    )
                    self.assertEqual(
                        activation,
                        [
                            "sudo",
                            "/srv/muriel/deploy-release",
                            self.commit,
                            "<",
                            self.remote_archive,
                        ],
                    )
                if failure != "stage":
                    cleanup = next(command for command in remote_commands if command[0] == "rm")
                    self.assertEqual(cleanup[:3], ["rm", "-f", "--"])
                    self.assertEqual(cleanup[3:], [self.remote_archive])
                if failure == "cleanup":
                    self.assertIn("may remain at " + self.remote_archive, result.stderr)
                else:
                    self.assertFalse(Path(self.remote_archive).exists())

    def test_refuses_an_unexpected_remote_temporary_path_without_running_it(self):
        artifact = self.create_artifact("manage.py")

        result = self.run_deploy(
            DEPLOY_ARTIFACT=str(artifact),
            DEPLOY_REMOTE_ARCHIVE="/tmp/muriel-unsafe'; touch /tmp/unexpected; #",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unsafe temporary archive path", result.stderr)
        self.assertNotIn("scp", self.log.read_text())
        self.assertNotIn("rm -f --", self.log.read_text())

    def test_refuses_dirty_or_untracked_source_without_connecting(self):
        for status in (" M manage.py\n", "?? untracked.txt\n"):
            with self.subTest(status=status):
                result = self.run_deploy(GIT_STATUS=status)
                self.assertEqual(result.returncode, 1)
                self.assertIn("dirty source", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertEqual(list(self.archives.iterdir()), [])

    def test_refuses_invalid_target_selection_before_local_or_remote_work(self):
        selections = [
            (None, ("release-a",)),
            ("release-a", ()),
            ("release-a", ("release-a", "extra")),
            ("release-a", ("release-b",)),
            ("release-a", ("-oProxyCommand=bad",)),
        ]
        for invalid in (
            "",
            " release-a",
            "release-a ",
            "release\na",
            "release\ta",
            "-release",
            ".release",
            "_release",
            "user@host",
            "ssh://host",
            "release/a",
            "release*",
            "release;false",
            "$(false)",
            "rélease",
        ):
            selections.append((invalid, (invalid,)))
        for configured, arguments in selections:
            with self.subTest(configured=configured, arguments=arguments):
                result = self.run_deploy(arguments, MURIEL_SSH_TARGET=configured)
                self.assertEqual(result.returncode, 2)
                self.assertIn("MURIEL_SSH_TARGET", result.stderr)
                self.assertFalse(self.local_log.exists())
                self.assertFalse(self.log.exists())
                self.assertEqual(list(self.archives.iterdir()), [])

    def test_refuses_an_archive_that_contains_excluded_material(self):
        artifact = self.create_artifact("manage.py", ".env", "db.sqlite3")

        result = self.run_deploy(DEPLOY_ARTIFACT=str(artifact))

        self.assertEqual(result.returncode, 1)
        self.assertIn("excluded development or secret material", result.stderr)
        self.assertFalse(self.log.exists())


class ProductionUpdateCommandTests(SimpleTestCase):
    commit = "a" * 40
    tailscale_status = """{
  "TCP": {
    "443": {
      "TCPForward": "127.0.0.1:8080",
      "TerminateTLS": "funnel.example.test",
      "ProxyProtocol": 2
    },
    "8443": {"HTTPS": true}
  },
  "Web": {
    "funnel.example.test:8443": {
      "Handlers": {
        "/": {"Proxy": "https+insecure://127.0.0.1:8081"}
      }
    }
  },
  "AllowFunnel": {
    "funnel.example.test:443": true
  }
}"""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "checkout"
        self.fake_bin = self.temporary_path / "bin"
        self.host_root = self.temporary_path / "host"
        self.log = self.temporary_path / "commands.log"
        self.local_log = self.temporary_path / "local-commands.log"
        self.archives = self.temporary_path / "archives"
        self.archives.mkdir()
        self.upload = self.temporary_path / "uploaded.tar"
        token = "".join(character for character in self.temporary_path.name if character.isalnum())[
            -6:
        ]
        self.remote_archive = f"/tmp/muriel-{self.commit}.{token}.tar"
        self.remote_helper = f"/tmp/muriel-{self.commit}.{token}.sh"
        self.root.mkdir()
        self.fake_bin.mkdir()
        self.addCleanup(Path(self.remote_archive).unlink, missing_ok=True)
        self.addCleanup(Path(self.remote_helper).unlink, missing_ok=True)
        (self.root / "ops").mkdir()
        shutil.copy2(UPDATE_PRODUCTION, self.root / "ops" / "update-production")
        shutil.copy2(ROOT / "ops/require-production-identity.sh", self.root / "ops")
        shutil.copy2(ROOT / "ops" / "require-ssh-target.sh", self.root / "ops")
        self.environment_file = self.host_root / "etc/muriel/environment"
        self.environment_file.parent.mkdir(parents=True)
        self.environment_file.write_text(
            '[ "${UPDATE_SERVICE_USER:-}" = muriel ] || exit 91\n'
            'printf "%s\\n" environment-sourced >> "$UPDATE_LOG"\n'
            "PRECEPTOR_PRODUCTION_ALLOWED_HOST=funnel.example.test\n"
            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://funnel.example.test\n"
            "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST=edge.example.test\n"
            "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN=https://edge.example.test\n"
            "PRECEPTOR_PRODUCTION_SECRET_KEY=fixture-secret-not-for-diagnostics\n"
        )
        self.environment_file.chmod(0o640)
        current = self.host_root / "srv/muriel/current"
        (current / ".venv/bin").mkdir(parents=True)
        (current / "ops").mkdir()
        (current / ".venv/bin/python").symlink_to(sys.executable)
        # Execute the real configuration consumer, not live sockets. Fake runuser
        # models identity selection; it does not establish OS privilege separation.
        self.write_executable(
            current / "ops/check-public-edge",
            """import json
import os
import runpy

assert os.environ["UPDATE_SERVICE_USER"] == "muriel"
probe = runpy.run_path(os.environ["UPDATE_EDGE_PROBE"])
configured_hosts = probe["configured_hosts"]
if __name__ == "__main__":
    try:
        hosts = configured_hosts()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    with open(os.environ["UPDATE_LOG"], "a") as log:
        log.write("probe-hosts=" + json.dumps(hosts) + "\\n")
    raise SystemExit(1 if os.environ.get("UPDATE_REMOTE_FAIL") == "probe" else 0)
""",
        )
        deploy_release = self.host_root / "srv/muriel/deploy-release"
        self.write_executable(
            deploy_release,
            """#!/bin/sh
set -eu
printf 'deploy:%s:%s\\n' "$1" "$(tar -xOf - revision)" >> "$UPDATE_LOG"
[ "${UPDATE_REMOTE_FAIL:-}" != deploy ]
""",
        )
        self.write_executable(
            self.fake_bin / "git",
            """#!/bin/sh
set -eu
while [ "${1:-}" = -C ]; do shift 2; done
printf '%s\\n' git "$@" >> "$UPDATE_LOCAL_LOG"
case "$1" in
    status) printf '%s' "${GIT_STATUS:-}" ;;
    rev-parse) printf '%s\\n' "$UPDATE_COMMIT" ;;
    archive) cat "$UPDATE_ARTIFACT" ;;
    *) exit 1 ;;
esac
""",
        )
        self.write_executable(
            self.fake_bin / "ssh",
            """#!/bin/sh
set -eu
printf '%s\\n' ssh >> "$UPDATE_LOG"
for argument; do printf '%s\\n' "$argument" >> "$UPDATE_LOG"; done
case "$*" in
    *"mktemp --suffix=.tar"*)
        [ "${UPDATE_FAIL:-}" != stage ] || exit 1
        printf '%s\\n' "$UPDATE_REMOTE_ARCHIVE"
        ;;
    *"mktemp --suffix=.sh"*)
        [ "${UPDATE_FAIL:-}" != helper ] || exit 1
        : > "$UPDATE_REMOTE_HELPER"
        printf '%s\\n' "$UPDATE_REMOTE_HELPER"
        ;;
    *"tar -xOf"*)
        sh -c "$argument"
        ;;
    *"sudo sh "*)
        stdin_bytes=$(wc -c < /dev/stdin)
        printf 'sudo-stdin=%s\\n' "$stdin_bytes" >> "$UPDATE_LOG"
        [ "$stdin_bytes" -eq 0 ] || exit 1
        eval "set -- $argument"
        helper=$3
        commit=$4
        archive=$5
        [ "${UPDATE_FAIL:-}" != remote ] || exit 1
        printf '%s\\n' remote-session >> "$UPDATE_LOG"
        program=$(sed -e "s|/srv/muriel|$UPDATE_HOST_ROOT/srv/muriel|g" \
            -e "s|/etc/muriel|$UPDATE_HOST_ROOT/etc/muriel|g" "$helper")
        # Model sudo discarding inherited identity; only literal argv survives.
        unset PRECEPTOR_PRODUCTION_ALLOWED_HOST PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN
        unset PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN
        sh -c "$program" "$helper" "$commit" "$archive" "$6" "$7" "$8" "$9"
        ;;
    *"rm -f --"*)
        [ "${UPDATE_CLEANUP_FAIL:-}" != 1 ] || exit 1
        rm -f -- "$UPDATE_REMOTE_ARCHIVE"
        rm -f -- "$UPDATE_REMOTE_HELPER"
        ;;
esac
""",
        )
        self.write_executable(
            self.fake_bin / "scp",
            """#!/bin/sh
set -eu
printf '%s\\n' scp >> "$UPDATE_LOG"
for argument; do printf '%s\\n' "$argument" >> "$UPDATE_LOG"; done
printf 'local-artifact=%s\\n' "$1" >> "$UPDATE_LOG"
stat -c 'mode=%a' "$1" >> "$UPDATE_LOG"
[ "${UPDATE_FAIL:-}" != transfer ] || exit 1
cp "$1" "$UPDATE_UPLOAD"
cp "$1" "$UPDATE_REMOTE_ARCHIVE"
""",
        )
        self.write_executable(
            self.fake_bin / "dispatcher",
            """#!/bin/sh
set -eu
command=$(basename "$0")
printf '%s %s\\n' "$command" "$*" >> "$UPDATE_LOG"
case "$command" in
    nginx)
        [ "${UPDATE_REMOTE_FAIL:-}" != nginx ]
        ;;
    tailscale)
        [ "$*" = "serve status --json" ] || exit 1
        [ "${UPDATE_REMOTE_FAIL:-}" != tailscale ] || exit 1
        if [ -n "${UPDATE_ROUTE_ENVIRONMENT:-}" ]; then
            cp "$UPDATE_ROUTE_ENVIRONMENT" "$UPDATE_HOST_ROOT/etc/muriel/environment"
        fi
        printf '%s\\n' "$UPDATE_TAILSCALE_STATUS"
        ;;
    python3.14)
        exec "$UPDATE_PYTHON" "$@"
        ;;
    runuser)
        [ "$1:$2:$3" = '-u:muriel:--' ] || exit 1
        shift 3
        UPDATE_SERVICE_USER=muriel
        export UPDATE_SERVICE_USER
        exec "$@"
        ;;
    stat)
        if [ "$*" = "-c %U:%G:%a $UPDATE_HOST_ROOT/etc/muriel/environment" ]; then
            mode=$("$UPDATE_SYSTEM_STAT" -c '%a' "$UPDATE_HOST_ROOT/etc/muriel/environment")
            printf '%s:%s\\n' "${UPDATE_ENVIRONMENT_OWNER:-root:muriel}" "$mode"
        else
            exec "$UPDATE_SYSTEM_STAT" "$@"
        fi
        ;;
esac
""",
        )
        for command in ("nginx", "runuser", "systemctl", "tailscale", "python3.14", "stat"):
            (self.fake_bin / command).symlink_to("dispatcher")
        self.write_executable(
            self.fake_bin / "mktemp",
            """#!/bin/sh
set -eu
printf '%s\\n' mktemp "$@" >> "$UPDATE_LOCAL_LOG"
directory=$("$UPDATE_SYSTEM_MKTEMP" "$@")
printf 'staging=%s\\n' "$directory" >> "$UPDATE_LOG"
if [ -d "$directory" ]; then
    "$UPDATE_SYSTEM_STAT" -c 'staging-mode=%a' "$directory" >> "$UPDATE_LOG"
fi
printf '%s\\n' "$directory"
""",
        )

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def artifact(self):
        source = self.temporary_path / "artifact-source"
        source.mkdir(exist_ok=True)
        (source / "manage.py").write_text("placeholder\n")
        (source / "revision").write_text(self.commit + "\n")
        bootstrap = source / "ops/bootstrap-ubuntu"
        bootstrap.parent.mkdir(exist_ok=True)
        shutil.copy2(UPDATE_PRODUCTION_REMOTE, bootstrap.parent / "update-production-remote")
        shutil.copy2(ROOT / "ops/require-production-identity.sh", bootstrap.parent)
        self.write_executable(
            bootstrap,
            """#!/bin/sh
set -eu
. "$(dirname "$0")/require-production-identity.sh"
revision=$(cat "$(dirname "$0")/../revision")
[ "$revision" = "$UPDATE_COMMIT" ]
printf 'bootstrap:%s:%s\\n' "$1" "$revision" >> "$UPDATE_LOG"
printf 'bootstrap-identity:%s:%s:%s:%s\\n' "$primary_host" "$primary_origin" "$secondary_host" "$secondary_origin" >> "$UPDATE_LOG"
case "$1" in
    --check)
        [ "${UPDATE_BOOTSTRAP_DIVERGED:-}" != 1 ] ||
            [ -f "$UPDATE_BOOTSTRAP_APPLIED" ]
        ;;
    --apply)
        [ "${UPDATE_REMOTE_FAIL:-}" != bootstrap ] || exit 1
        : > "$UPDATE_BOOTSTRAP_APPLIED"
        ;;
    *) exit 1 ;;
esac
""",
        )
        if fixture := getattr(self, "actual_bootstrap", None):
            # Keep host commands disposable while exercising the archived guard,
            # renderer, protected loader, OpenSSL boundary and all bootstrap modes.
            shutil.copytree(ROOT / "ops", source / "ops", dirs_exist_ok=True)
            shutil.copy2(bootstrap, source / "ops/bootstrap-real")
            environment = {
                "PATH": f"{fixture.fake_bin}:{os.environ['PATH']}",
                "FAKE_LOG": str(fixture.log),
                "FAKE_COMMANDS": str(fixture.temporary_path / "commands.bin"),
                "FAKE_ROOT": str(fixture.fake_root),
                "FAKE_STATE": str(fixture.state),
                "FIXTURE_CERTIFICATE": str(fixture.certificate),
                "FIXTURE_KEY": str(fixture.key),
                "REAL_STAT": shutil.which("stat"),
                "TMPDIR": str(fixture.render_scratch),
            }
            self.write_executable(
                bootstrap,
                "#!/bin/sh\nexec env "
                + " ".join(shlex.quote(f"{key}={value}") for key, value in environment.items())
                + ' "$(dirname "$0")/bootstrap-real" "$@" --root '
                + shlex.quote(str(fixture.fake_root))
                + "\n",
            )
        archive = self.temporary_path / "source.tar"
        with tarfile.open(archive, "w") as tar:
            for path in source.rglob("*"):
                tar.add(path, path.relative_to(source), recursive=False)
        return archive

    def run_update(self, arguments=("production-a",), **environment):
        env = os.environ | {
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "MURIEL_SSH_TARGET": "production-a",
            "TMPDIR": str(self.archives),
            "UPDATE_ARTIFACT": str(self.artifact()),
            "UPDATE_COMMIT": self.commit,
            "UPDATE_LOG": str(self.log),
            "UPDATE_LOCAL_LOG": str(self.local_log),
            "UPDATE_REMOTE_ARCHIVE": self.remote_archive,
            "UPDATE_REMOTE_HELPER": self.remote_helper,
            "UPDATE_UPLOAD": str(self.upload),
            "UPDATE_HOST_ROOT": str(self.host_root),
            "UPDATE_PYTHON": sys.executable,
            "UPDATE_SYSTEM_MKTEMP": shutil.which("mktemp"),
            "UPDATE_SYSTEM_STAT": shutil.which("stat"),
            "UPDATE_EDGE_PROBE": str(ROOT / "ops/check-public-edge"),
            "UPDATE_BOOTSTRAP_APPLIED": str(self.temporary_path / "bootstrap-applied"),
            "UPDATE_TAILSCALE_STATUS": self.tailscale_status,
            **PRODUCTION_IDENTITY,
            **environment,
        }
        return subprocess.run(
            [str(self.root / "ops" / "update-production"), *arguments],
            cwd=Path.home(),
            env={key: value for key, value in env.items() if value is not None},
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_transport_target(self, commands, target):
        self.assertIn("ssh", commands)
        for index, command in enumerate(commands):
            if command == "ssh":
                destination = index + 1
                if commands[destination] == "-tt":
                    destination += 1
                self.assertEqual(commands[destination], target)
            elif command == "scp":
                self.assertEqual(commands[index + 2], f"{target}:{self.remote_archive}")

    def assert_browser_destinations(self, result, *, cloudflare, funnel, admin):
        self.assertIn(f"deployed commit {self.commit}", result.stdout)
        for role, url in (
            (r"public\b[^\n]*cloudflare", cloudflare),
            (r"public\b[^\n]*temporary[^\n]*funnel[^\n]*fallback", funnel),
            (r"admin\b", admin),
        ):
            self.assertRegex(result.stdout, rf"(?im)^{role}[^\n]*{re.escape(url)}(?=\s|$)")

    def assert_no_update_success(self, result):
        self.assertNotIn("deployed commit", result.stdout)
        self.assertNotRegex(result.stdout, r"(?im)^(?:public|admin)\b[^\n]*https://")
        self.assertNotIn(
            "fixture-secret-not-for-diagnostics",
            result.stdout + result.stderr + self.log.read_text(),
        )

    def test_executes_the_remote_payload_for_converged_and_reconciled_hosts(self):
        for name, target, environment, expected_bootstrap in (
            ("converged", "production-a", {}, [f"bootstrap:--check:{self.commit}"]),
            (
                "reconciled",
                "2.production_B",
                {"UPDATE_BOOTSTRAP_DIVERGED": "1"},
                [
                    f"bootstrap:--check:{self.commit}",
                    f"bootstrap:--apply:{self.commit}",
                    f"bootstrap:--check:{self.commit}",
                ],
            ),
        ):
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                Path(self.remote_archive).unlink(missing_ok=True)
                (self.temporary_path / "bootstrap-applied").unlink(missing_ok=True)
                result = self.run_update((target,), MURIEL_SSH_TARGET=target, **environment)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_browser_destinations(
                    result,
                    cloudflare="https://edge.example.test/",
                    funnel="https://funnel.example.test/",
                    admin="https://funnel.example.test:8443/admin/",
                )
                commands = self.log.read_text().splitlines()
                self.assert_transport_target(commands, target)
                self.assertIn("mode=600", commands)
                self.assertIn("staging-mode=700", commands)
                self.assertEqual(commands.count("-tt"), 1)
                self.assertEqual(commands.count("remote-session"), 1)
                self.assertIn("sudo-stdin=0", commands)
                remote_program = next(
                    command for command in commands if command.startswith("sudo sh ")
                )
                self.assertEqual(
                    shlex.split(remote_program),
                    [
                        "sudo",
                        "sh",
                        self.remote_helper,
                        self.commit,
                        self.remote_archive,
                        "funnel.example.test",
                        "https://funnel.example.test",
                        "edge.example.test",
                        "https://edge.example.test",
                    ],
                )
                self.assertEqual(
                    [command for command in commands if command.startswith("bootstrap:")],
                    expected_bootstrap,
                )
                self.assertEqual(
                    [command for command in commands if command.startswith("bootstrap-identity:")],
                    [
                        "bootstrap-identity:funnel.example.test:https://funnel.example.test:edge.example.test:https://edge.example.test"
                    ]
                    * len(expected_bootstrap),
                )
                self.assertIn(f"deploy:{self.commit}:{self.commit}", commands)
                self.assertLess(
                    commands.index(f"deploy:{self.commit}:{self.commit}"),
                    commands.index("nginx -t"),
                )
                self.assertLess(
                    commands.index("nginx -t"), commands.index("systemctl reload nginx")
                )
                self.assertIn("systemctl enable --now muriel-backup.timer", commands)
                enabled = shlex.split(
                    next(
                        command
                        for command in commands
                        if command.startswith("systemctl is-enabled ")
                    )
                )
                self.assertEqual(enabled[:2], ["systemctl", "is-enabled"])
                self.assertIn("--quiet", enabled)
                self.assertEqual(
                    set(enabled[2:]) - {"--quiet"},
                    {"muriel", "nginx", "muriel-backup.timer"},
                )
                active = shlex.split(
                    next(
                        command
                        for command in commands
                        if command.startswith("systemctl is-active ")
                    )
                )
                self.assertEqual(active[:2], ["systemctl", "is-active"])
                self.assertIn("--quiet", active)
                self.assertEqual(
                    set(active[2:]) - {"--quiet"},
                    {"muriel", "nginx", "postgresql@18-main", "muriel-backup.timer"},
                )
                self.assertIn("environment-sourced", commands)
                self.assertIn('probe-hosts=["funnel.example.test", "edge.example.test"]', commands)
                self.assertLess(
                    commands.index("environment-sourced"),
                    commands.index('probe-hosts=["funnel.example.test", "edge.example.test"]'),
                )
                self.assertNotIn(
                    "fixture-secret-not-for-diagnostics",
                    result.stdout + result.stderr + self.log.read_text(),
                )
                self.assertEqual(
                    [command for command in commands if command.startswith("tailscale ")],
                    ["tailscale serve status --json"],
                )
                self.assertFalse(Path(self.remote_archive).exists())
                self.assertFalse(Path(self.remote_helper).exists())
                for command in commands:
                    if command.startswith("local-artifact="):
                        self.assertFalse(Path(command.removeprefix("local-artifact=")).exists())
                    if command.startswith("staging="):
                        self.assertFalse(Path(command.removeprefix("staging=")).exists())

    def test_local_handoff_runs_real_bootstrap_for_two_independent_identities(self):
        from .test_host_bootstrap import UbuntuHostBootstrapTests

        for primary, secondary in (
            ("funnel.example.test", "edge.example.test"),
            ("fallback.other.test", "public.other.test"),
        ):
            with self.subTest(primary=primary, secondary=secondary):
                fixture = UbuntuHostBootstrapTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                fixture.certificate, fixture.key = fixture.make_pair("selected", primary)
                self.actual_bootstrap = fixture
                selected = {
                    "PRECEPTOR_PRODUCTION_ALLOWED_HOST": primary,
                    "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": f"https://{primary}",
                    "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": secondary,
                    "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": f"https://{secondary}",
                    "UPDATE_TAILSCALE_STATUS": self.tailscale_status.replace(
                        "funnel.example.test", primary
                    ),
                }
                self.environment_file.write_text(
                    '[ "${UPDATE_SERVICE_USER:-}" = muriel ] || exit 91\n'
                    + "".join(
                        f"{key}={shlex.quote(value)}\n"
                        for key, value in selected.items()
                        if key.startswith("PRECEPTOR_")
                    )
                )
                self.assertFalse(fixture.environment_file.exists())
                result = self.run_update(**selected)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_browser_destinations(
                    result,
                    cloudflare=f"https://{secondary}/",
                    funnel=f"https://{primary}/",
                    admin=f"https://{primary}:8443/admin/",
                )
                requests = [args for args in fixture.received("tailscale") if args[0] == "cert"]
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][-1], primary)
                site = (fixture.fake_root / "etc/nginx/sites-available/muriel").read_text()
                for port, suffix, host in (
                    (8080, " proxy_protocol", primary),
                    (8081, " ssl", primary),
                    (8083, "", secondary),
                ):
                    self.assertRegex(
                        site,
                        rf"listen 127\.0\.0\.1:{port}{suffix};\s+listen \[::1\]:{port}{suffix};\s+server_name {re.escape(host)};",
                    )
                admin = (
                    fixture.fake_root / "etc/nginx/snippets/muriel-admin-proxy.conf"
                ).read_text()
                self.assertIn(f"proxy_set_header Host {primary}:8443;", admin)
                self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", admin)
                fixture.write_environment(secondary, primary)
                before = fixture.snapshot(fixture.fake_root)
                result = self.run_update(**selected)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fixture.snapshot(fixture.fake_root), before)
                self.assertEqual(
                    [args for args in fixture.received("tailscale") if args[0] == "cert"], requests
                )
                self.assertEqual(list(fixture.render_scratch.iterdir()), [])
                # Local selection cannot adopt or migrate protected configuration.
                self.log.unlink()
                result = self.run_update(
                    **(
                        selected
                        | {
                            "PRECEPTOR_PRODUCTION_ALLOWED_HOST": "different.test",
                            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "https://different.test",
                        }
                    )
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("production environment identity", result.stderr)
                self.assertEqual(fixture.snapshot(fixture.fake_root), before)
                self.assertNotIn("deploy:", self.log.read_text())
                self.assertFalse(Path(self.remote_archive).exists())
                self.assertFalse(Path(self.remote_helper).exists())

    def assert_edge_failure_stops_update(self, result):
        self.assertNotEqual(result.returncode, 0)
        commands = self.log.read_text().splitlines()
        self.assertNotIn("tailscale serve status --json", commands)
        self.assert_no_update_success(result)
        self.assertFalse(Path(self.remote_archive).exists())
        self.assertFalse(Path(self.remote_helper).exists())
        for command in commands:
            if command.startswith(("staging=", "local-artifact=")):
                self.assertFalse(Path(command.split("=", 1)[1]).exists())
        return commands

    def test_refuses_unsafe_environment_without_sourcing_it(self):
        contents = self.environment_file.read_text()
        target = self.environment_file.with_name("environment-target")
        target.write_text(contents)
        for case in ("missing", "symlink", "directory", "mode", "owner", "group", "unreadable"):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                self.environment_file.unlink()
                environment = {}
                if case == "symlink":
                    self.environment_file.symlink_to(target)
                elif case == "directory":
                    self.environment_file.mkdir()
                elif case != "missing":
                    self.environment_file.write_text(contents)
                    self.environment_file.chmod(0o600 if case == "mode" else 0o640)
                    if case == "owner":
                        environment["UPDATE_ENVIRONMENT_OWNER"] = "muriel:muriel"
                    elif case == "group":
                        environment["UPDATE_ENVIRONMENT_OWNER"] = "root:other"
                    elif case == "unreadable":
                        self.environment_file.chmod(0)
                try:
                    result = self.run_update(**environment)
                    commands = self.assert_edge_failure_stops_update(result)
                    self.assertIn("production environment file", result.stderr)
                    self.assertNotIn("environment-sourced", commands)
                    self.assertFalse(
                        any(command.startswith("probe-hosts=") for command in commands)
                    )
                finally:
                    if case == "directory":
                        self.environment_file.rmdir()
                    else:
                        self.environment_file.unlink(missing_ok=True)
                    self.environment_file.write_text(contents)
                    self.environment_file.chmod(0o640)

    def test_edge_routes_and_browser_urls_use_protected_not_inherited_configuration(self):
        self.environment_file.write_text(
            self.environment_file.read_text()
            .replace("funnel.example.test", "fallback.other.test")
            .replace("edge.example.test", "public.other.test")
        )
        result = self.run_update(
            ("transport-only",),
            MURIEL_SSH_TARGET="transport-only",
            UPDATE_TAILSCALE_STATUS=self.tailscale_status.replace(
                "funnel.example.test", "fallback.other.test"
            ),
            PRECEPTOR_PRODUCTION_ALLOWED_HOST="fallback.other.test",
            PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="https://fallback.other.test",
            PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="public.other.test",
            PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="https://public.other.test",
            UPDATE_BOOTSTRAP_DIVERGED="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'probe-hosts=["fallback.other.test", "public.other.test"]',
            self.log.read_text().splitlines(),
        )
        commands = self.log.read_text().splitlines()
        self.assertEqual(
            [command for command in commands if command.startswith("bootstrap-identity:")],
            [
                "bootstrap-identity:fallback.other.test:https://fallback.other.test:public.other.test:https://public.other.test"
            ]
            * 3,
        )
        self.assert_transport_target(commands, "transport-only")
        self.assert_browser_destinations(
            result,
            cloudflare="https://public.other.test/",
            funnel="https://fallback.other.test/",
            admin="https://fallback.other.test:8443/admin/",
        )
        self.assertNotIn(
            "fixture-secret-not-for-diagnostics",
            result.stdout + result.stderr + self.log.read_text(),
        )

    def test_missing_or_invalid_probe_settings_stop_update_even_with_inherited_values(self):
        contents = self.environment_file.read_text()
        for setting, original in (
            ("PRECEPTOR_PRODUCTION_ALLOWED_HOST", "funnel.example.test"),
            ("PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN", "https://funnel.example.test"),
            ("PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST", "edge.example.test"),
            ("PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN", "https://edge.example.test"),
        ):
            for replacement in ("", f"{setting}='unsafe value'\n"):
                with self.subTest(setting=setting, replacement=replacement):
                    self.log.unlink(missing_ok=True)
                    self.environment_file.write_text(
                        contents.replace(f"{setting}={original}\n", replacement)
                    )
                    result = self.run_update(**{setting: original})
                    commands = self.assert_edge_failure_stops_update(result)
                    self.assertIn("environment-sourced", commands)
                    self.assertIn(setting, result.stderr)
                    self.assertIn("sudo -u muriel sh -eu -c", result.stderr)

    def test_probe_failure_stops_later_verification_and_cleans_artifacts(self):
        result = self.run_update(UPDATE_REMOTE_FAIL="probe")
        commands = self.assert_edge_failure_stops_update(result)
        self.assertIn('probe-hosts=["funnel.example.test", "edge.example.test"]', commands)
        self.assertIn("local public-edge probe failed", result.stderr)

    def assert_route_failure_cleans_update(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assert_no_update_success(result)
        commands = self.log.read_text().splitlines()
        self.assertEqual(
            [command for command in commands if command.startswith("tailscale ")],
            ["tailscale serve status --json"],
        )
        self.assertFalse(Path(self.remote_archive).exists())
        self.assertFalse(Path(self.remote_helper).exists())
        for command in commands:
            if command.startswith(("staging=", "local-artifact=")):
                self.assertFalse(Path(command.split("=", 1)[1]).exists())

    def test_route_acquisition_and_invalid_json_fail_without_success_or_residue(self):
        for environment, diagnostic in (
            ({"UPDATE_REMOTE_FAIL": "tailscale"}, "could not inspect Funnel and Serve state"),
            ({"UPDATE_TAILSCALE_STATUS": "{invalid"}, "invalid captured JSON"),
            ({"UPDATE_TAILSCALE_STATUS": ""}, "invalid captured JSON"),
        ):
            with self.subTest(environment=environment):
                self.log.unlink(missing_ok=True)
                result = self.run_update(**environment)
                self.assert_route_failure_cleans_update(result)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_route_consumer_validates_its_own_protected_identity(self):
        # The edge passes first. Change the fixture file at route acquisition to
        # prove the comparator itself sources and validates protected values.
        contents = self.environment_file.read_text()
        route_environment = self.temporary_path / "route-environment"
        for setting, original, replacement in (
            ("ALLOWED_HOST", "funnel.example.test", None),
            ("ALLOWED_HOST", "funnel.example.test", ""),
            ("ALLOWED_HOST", "funnel.example.test", " funnel.example.test"),
            ("ALLOWED_HOST", "funnel.example.test", "funnel.example.test/path"),
            ("CSRF_TRUSTED_ORIGIN", "https://funnel.example.test", None),
            ("CSRF_TRUSTED_ORIGIN", "https://funnel.example.test", "https://foreign.test"),
        ):
            setting = f"PRECEPTOR_PRODUCTION_{setting}"
            with self.subTest(setting=setting, replacement=replacement):
                self.log.unlink(missing_ok=True)
                self.environment_file.write_text(contents)
                route_environment.write_text(
                    contents.replace(
                        f"{setting}={original}\n",
                        "" if replacement is None else f"{setting}={shlex.quote(replacement)}\n",
                    )
                )
                result = self.run_update(
                    UPDATE_ROUTE_ENVIRONMENT=str(route_environment),
                    PRECEPTOR_PRODUCTION_ALLOWED_HOST="funnel.example.test",
                    PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN="https://funnel.example.test",
                )
                self.assert_route_failure_cleans_update(result)
                self.assertIn(f"route verification: {setting}", result.stderr)
                self.assertIn(
                    'probe-hosts=["funnel.example.test", "edge.example.test"]',
                    self.log.read_text().splitlines(),
                )

    def test_rejects_unsafe_tailscale_routes_without_mutating_tailscale(self):
        expected = json.loads(self.tailscale_status)
        unsafe_states = {
            "complete foreign identity": json.loads(
                self.tailscale_status.replace("funnel.example.test", "foreign.example.test")
            ),
            "additional top-level key": {**expected, "Extra": {}},
            "float PROXY protocol": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {**expected["TCP"]["443"], "ProxyProtocol": 2.0},
                },
            },
            "changed destination": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {**expected["TCP"]["443"], "TCPForward": "127.0.0.1:9999"},
                },
            },
            "missing TLS termination": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {
                        key: value
                        for key, value in expected["TCP"]["443"].items()
                        if key != "TerminateTLS"
                    },
                },
            },
            "wrong hostname": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {**expected["TCP"]["443"], "TerminateTLS": "other.ts.net"},
                },
            },
            "missing PROXY protocol": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {
                        key: value
                        for key, value in expected["TCP"]["443"].items()
                        if key != "ProxyProtocol"
                    },
                },
            },
            "wrong PROXY protocol": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "443": {**expected["TCP"]["443"], "ProxyProtocol": "v1"},
                },
            },
            "missing private listener": {
                **expected,
                "TCP": {"443": expected["TCP"]["443"]},
            },
            "wrong private listener": {
                **expected,
                "TCP": {**expected["TCP"], "8443": {"HTTPS": False}},
            },
            "numeric private listener HTTPS": {
                **expected,
                "TCP": {**expected["TCP"], "8443": {"HTTPS": 1}},
            },
            "changed Admin handler": {
                **expected,
                "Web": {
                    "funnel.example.test:8443": {
                        "Handlers": {"/": {"Proxy": "https://127.0.0.1:8081"}}
                    }
                },
            },
            "missing Admin handler": {
                **expected,
                "Web": {"funnel.example.test:8443": {"Handlers": {}}},
            },
            "additional Admin handler": {
                **expected,
                "Web": {
                    "funnel.example.test:8443": {
                        "Handlers": {
                            **expected["Web"]["funnel.example.test:8443"]["Handlers"],
                            "/extra": {"Proxy": "https+insecure://127.0.0.1:8081"},
                        }
                    }
                },
            },
            "public Admin route": {
                **expected,
                "AllowFunnel": {
                    **expected["AllowFunnel"],
                    "funnel.example.test:8443": True,
                },
            },
            "unexpected Funnel permission": {**expected, "AllowFunnel": {}},
            "numeric Funnel permission": {
                **expected,
                "AllowFunnel": {"funnel.example.test:443": 1},
            },
            "additional route": {
                **expected,
                "TCP": {
                    **expected["TCP"],
                    "9999": {"TCPForward": "127.0.0.1:9999"},
                },
            },
        }

        for name, state in unsafe_states.items():
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                result = self.run_update(UPDATE_TAILSCALE_STATUS=json.dumps(state))

                self.assertEqual(result.returncode, 1)
                self.assertIn("unexpected Funnel or Serve state", result.stderr)
                self.assert_route_failure_cleans_update(result)
                diagnostic_terms = {
                    "complete foreign identity": ("$.AllowFunnel", "foreign.example.test"),
                    "additional top-level key": ("$: expected keys", "Extra"),
                    "float PROXY protocol": ("$.TCP.443.ProxyProtocol", "float"),
                    "changed destination": ("$.TCP.443.TCPForward", "127.0.0.1:9999"),
                    "missing TLS termination": ("$.TCP.443", "TerminateTLS"),
                    "wrong hostname": ("$.TCP.443.TerminateTLS", "other.ts.net"),
                    "missing PROXY protocol": ("$.TCP.443", "ProxyProtocol"),
                    "wrong PROXY protocol": ("$.TCP.443.ProxyProtocol", "v1"),
                    "missing private listener": ("$.TCP", "8443"),
                    "wrong private listener": ("$.TCP.8443.HTTPS", "False"),
                    "numeric private listener HTTPS": ("$.TCP.8443.HTTPS", "int"),
                    "changed Admin handler": ("$.Web", "https://127.0.0.1:8081"),
                    "missing Admin handler": (
                        "$.Web.funnel.example.test:8443.Handlers",
                        "expected keys",
                    ),
                    "additional Admin handler": (
                        "$.Web.funnel.example.test:8443.Handlers",
                        "/extra",
                    ),
                    "public Admin route": ("$.AllowFunnel", "8443"),
                    "unexpected Funnel permission": ("$.AllowFunnel", "443"),
                    "numeric Funnel permission": (
                        "$.AllowFunnel.funnel.example.test:443",
                        "int",
                    ),
                    "additional route": ("$.TCP", "9999"),
                }
                for term in diagnostic_terms[name]:
                    self.assertIn(term, result.stderr)
                self.assertEqual(
                    [
                        command
                        for command in self.log.read_text().splitlines()
                        if command.startswith("tailscale ")
                    ],
                    ["tailscale serve status --json"],
                )

    def test_accepts_the_exact_staging_route_without_mutating_tailscale(self):
        expected = json.loads(self.tailscale_status)
        state = {
            **expected,
            "TCP": {**expected["TCP"], "10000": {"HTTPS": True}},
            "Web": {
                **expected["Web"],
                "funnel.example.test:10000": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8082"}}
                },
            },
        }

        result = self.run_update(UPDATE_TAILSCALE_STATUS=json.dumps(state))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [
                command
                for command in self.log.read_text().splitlines()
                if command.startswith("tailscale ")
            ],
            ["tailscale serve status --json"],
        )

    def test_rejects_unsafe_staging_routes_without_mutating_tailscale(self):
        expected = json.loads(self.tailscale_status)
        staging_route = "funnel.example.test:10000"
        staging_listener = {"HTTPS": True}
        staging_mapping = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8082"}}}
        complete_staging_route = {
            **expected,
            "TCP": {**expected["TCP"], "10000": staging_listener},
            "Web": {**expected["Web"], staging_route: staging_mapping},
        }
        unsafe_states = {
            "staging permission without listener or handler": {
                **expected,
                "AllowFunnel": {**expected["AllowFunnel"], staging_route: True},
            },
            "foreign staging host": {
                **complete_staging_route,
                "Web": {**expected["Web"], "foreign.example.test:10000": staging_mapping},
            },
            "staging listener without handler": {
                **expected,
                "TCP": {**expected["TCP"], "10000": staging_listener},
            },
            "staging handler without listener": {
                **expected,
                "Web": {**expected["Web"], staging_route: staging_mapping},
            },
            "altered staging listener": {
                **complete_staging_route,
                "TCP": {**expected["TCP"], "10000": {"HTTPS": False}},
            },
            "incorrectly typed staging listener": {
                **complete_staging_route,
                "TCP": {**expected["TCP"], "10000": {"HTTPS": 1}},
            },
            "altered staging target": {
                **complete_staging_route,
                "Web": {
                    **expected["Web"],
                    staging_route: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}},
                },
            },
            "incorrectly typed staging handler": {
                **complete_staging_route,
                "Web": {**expected["Web"], staging_route: []},
            },
            "staging Funnel permission": {
                **complete_staging_route,
                "AllowFunnel": {**expected["AllowFunnel"], staging_route: True},
            },
            "unrelated staging listener addition": {
                **complete_staging_route,
                "TCP": {
                    **expected["TCP"],
                    "10000": staging_listener,
                    "10001": {"HTTPS": True},
                },
            },
        }

        for name, state in unsafe_states.items():
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                result = self.run_update(UPDATE_TAILSCALE_STATUS=json.dumps(state))

                self.assertEqual(result.returncode, 1)
                self.assertIn("unexpected Funnel or Serve state", result.stderr)
                self.assert_route_failure_cleans_update(result)
                self.assertEqual(
                    [
                        command
                        for command in self.log.read_text().splitlines()
                        if command.startswith("tailscale ")
                    ],
                    ["tailscale serve status --json"],
                )

    def test_refuses_dirty_or_untracked_source_without_connecting(self):
        for status in (" M manage.py\n", "?? untracked.txt\n"):
            with self.subTest(status=status):
                result = self.run_update(GIT_STATUS=status)
                self.assertEqual(result.returncode, 1)
                self.assertIn("dirty source", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertEqual(list(self.archives.iterdir()), [])

    def test_refuses_invalid_target_selection_before_local_or_remote_work(self):
        selections = [
            (None, ("production-a",)),
            ("production-a", ()),
            ("production-a", ("production-a", "extra")),
            ("production-a", ("production-b",)),
            ("production-a", ("-oProxyCommand=bad",)),
        ]
        for invalid in (
            "",
            " production-a",
            "production-a ",
            "production\na",
            "production\ta",
            "-production",
            ".production",
            "_production",
            "user@host",
            "ssh://host",
            "production/a",
            "production*",
            "production;false",
            "$(false)",
            "pröduction",
        ):
            selections.append((invalid, (invalid,)))
        for configured, arguments in selections:
            with self.subTest(configured=configured, arguments=arguments):
                result = self.run_update(arguments, MURIEL_SSH_TARGET=configured)
                self.assertEqual(result.returncode, 2)
                self.assertIn("MURIEL_SSH_TARGET", result.stderr)
                self.assertFalse(self.local_log.exists())
                self.assertFalse(self.log.exists())
                self.assertEqual(list(self.archives.iterdir()), [])

    def test_invalid_identity_refuses_before_git_allocation_or_transport(self):
        for prefix, other_host in (
            ("PRECEPTOR_PRODUCTION", "edge.example.test"),
            ("PRECEPTOR_PRODUCTION_SECONDARY", "funnel.example.test"),
        ):
            for host, origin in (*INVALID_PRODUCTION_PAIRS, (other_host, f"https://{other_host}")):
                with self.subTest(prefix=prefix, host=host, origin=origin):
                    result = self.run_update(
                        **{
                            f"{prefix}_ALLOWED_HOST": host,
                            f"{prefix}_CSRF_TRUSTED_ORIGIN": origin,
                        }
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("Production", result.stderr)
                    self.assertFalse(self.local_log.exists())
                    self.assertFalse(self.log.exists())
                    self.assertEqual(list(self.archives.iterdir()), [])

    def test_standalone_remote_helper_refuses_bad_arguments_before_bootstrap(self):
        for arguments in (
            [],
            [self.commit],
            [self.commit, self.remote_archive, "edge.test"],
            [self.commit, self.remote_archive, "edge.test", "https://edge.test", "extra"],
            *(
                [self.commit, self.remote_archive, *pairs]
                for host, origin in (
                    *INVALID_PRODUCTION_PAIRS,
                    ("funnel.example.test", "https://funnel.example.test"),
                )
                for pairs in (
                    [
                        host or "",
                        origin or "",
                        "funnel.example.test",
                        "https://funnel.example.test",
                    ],
                    [
                        "funnel.example.test",
                        "https://funnel.example.test",
                        host or "",
                        origin or "",
                    ],
                )
            ),
        ):
            with self.subTest(arguments=arguments):
                shutil.copyfile(self.artifact(), self.remote_archive)
                shutil.copyfile(UPDATE_PRODUCTION_REMOTE, self.remote_helper)
                result = subprocess.run(
                    ["sh", self.remote_helper, *arguments],
                    env=os.environ | {"TMPDIR": str(self.archives)},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("bootstrap:", result.stdout + result.stderr)
                self.assertFalse(self.log.exists())
                # Invalid count is refused before accepting ownership; the fixture
                # owns those paths. Valid-count failures use remote owned cleanup.
                if len(arguments) == 6:
                    self.assertFalse(Path(self.remote_archive).exists())
                    self.assertFalse(Path(self.remote_helper).exists())
                Path(self.remote_archive).unlink(missing_ok=True)
                Path(self.remote_helper).unlink(missing_ok=True)

    def test_stage_transfer_remote_and_cleanup_failures_remain_failures(self):
        for target in ("production-a", "2.production_B"):
            for failure in ("stage", "transfer", "helper", "remote", "cleanup"):
                with self.subTest(target=target, failure=failure):
                    self.log.unlink(missing_ok=True)
                    Path(self.remote_archive).unlink(missing_ok=True)
                    Path(self.remote_helper).unlink(missing_ok=True)
                    environment = {"UPDATE_FAIL": failure}
                    if failure == "cleanup":
                        environment = {"UPDATE_FAIL": "remote", "UPDATE_CLEANUP_FAIL": "1"}
                    result = self.run_update((target,), MURIEL_SSH_TARGET=target, **environment)

                    self.assertEqual(result.returncode, 1)
                    self.assert_no_update_success(result)
                    commands = self.log.read_text().splitlines()
                    self.assert_transport_target(commands, target)
                    self.assertEqual(list(self.archives.iterdir()), [])
                    if failure != "stage":
                        cleanup = next(c for c in commands if c.startswith("rm -f --"))
                        self.assertEqual(
                            shlex.split(cleanup),
                            [
                                "rm",
                                "-f",
                                "--",
                                self.remote_archive,
                                self.remote_helper if failure in ("remote", "cleanup") else "",
                            ],
                        )
                    if failure == "cleanup":
                        self.assertIn("may remain at " + self.remote_archive, result.stderr)
                        self.assertIn(self.remote_helper, result.stderr)
                    else:
                        self.assertFalse(Path(self.remote_archive).exists())
                        self.assertFalse(Path(self.remote_helper).exists())

    def test_bootstrap_refusal_stops_deployment_and_cleans_owned_artifacts(self):
        # The actual file-safety boundary is exercised by UbuntuHostBootstrapTests.
        # This isolated payload seam proves only caller failure propagation/cleanup.
        result = self.run_update(UPDATE_BOOTSTRAP_DIVERGED="1", UPDATE_REMOTE_FAIL="bootstrap")

        commands = self.assert_edge_failure_stops_update(result)
        self.assertIn("bootstrap apply failed", result.stderr)
        self.assertIn("ops/bootstrap-ubuntu --check", result.stderr)
        self.assertIn(f"bootstrap:--apply:{self.commit}", commands)
        self.assertFalse(any(command.startswith("deploy:") for command in commands))
        self.assertFalse(any(command.startswith("systemctl ") for command in commands))
        self.assertNotIn("nginx -t", commands)
        self.assertNotIn("environment-sourced", commands)

    def test_failed_remote_phase_stops_later_phases_and_cleans_temporary_state(self):
        result = self.run_update(UPDATE_REMOTE_FAIL="nginx")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Nginx configuration is invalid", result.stderr)
        self.assert_no_update_success(result)
        commands = self.log.read_text().splitlines()
        self.assertIn(f"deploy:{self.commit}:{self.commit}", commands)
        self.assertIn("nginx -t", commands)
        self.assertNotIn("systemctl reload nginx", commands)
        self.assertNotIn("tailscale serve status --json", commands)
        self.assertFalse(Path(self.remote_archive).exists())
        self.assertFalse(Path(self.remote_helper).exists())
        for command in commands:
            if command.startswith("local-artifact="):
                self.assertFalse(Path(command.removeprefix("local-artifact=")).exists())
            if command.startswith("staging="):
                self.assertFalse(Path(command.removeprefix("staging=")).exists())


class ReleaseDeploymentTests(SimpleTestCase):
    commit = "a" * 40
    old_commit = "b" * 40

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.fake_root = self.temporary_path / "host"
        self.fake_bin = self.temporary_path / "bin"
        self.log = self.temporary_path / "commands.log"
        self.fake_root.mkdir()
        self.fake_bin.mkdir()
        (self.fake_root / "srv/muriel/releases" / self.old_commit).mkdir(parents=True)
        (self.fake_root / "srv/muriel/shared/pip-cache").mkdir(parents=True)
        (self.fake_root / "var/backups/muriel").mkdir(parents=True)
        environment = self.fake_root / "etc/muriel/environment"
        environment.parent.mkdir(parents=True)
        environment.write_text(
            "PRECEPTOR_PRODUCTION_SECRET_KEY=" + "s" * 50 + "\n"
            "PRECEPTOR_PRODUCTION_ALLOWED_HOST=muriel.example\n"
            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://muriel.example\n"
            "PRECEPTOR_POSTGRES_NAME=muriel\n"
            "PRECEPTOR_POSTGRES_USER=muriel\n"
            "MURIEL_BACKUP_OFFSITE_HOST=backup@example.net\n"
            "MURIEL_BACKUP_OFFSITE_DIRECTORY=/srv/backups/muriel\n"
        )
        (self.fake_root / "srv/muriel/current").symlink_to(f"releases/{self.old_commit}")
        shutil.copy2(
            ROOT / "ops" / "backup-database", self.fake_root / "srv/muriel/backup-database"
        )
        self.write_dispatcher()

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def recorded_calls(self):
        """Return NUL-framed fake-executable argv and environment records."""
        return [json.loads(record) for record in self.log.read_bytes().split(b"\0") if record]

    def calls_for(self, command):
        return [call for call in self.recorded_calls() if call["command"] == command]

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            """#!/bin/sh
set -eu
command=$(basename "$0")
record() {
    "$DEPLOY_RECORD_PYTHON" -c '
import json
import os
import sys

record = {
    "command": sys.argv[2],
    "argv": sys.argv[3:],
    "environment": {
        name: os.environ.get(name, "")
        for name in ("DJANGO_SETTINGS_MODULE", "PGCLUSTER", "PIP_CACHE_DIR")
    },
}
with open(sys.argv[1], "ab") as output:
    output.write(json.dumps(record).encode() + b"\\0")
' "$DEPLOY_LOG" "$command" "$@"
}
fail_if_requested() {
    [ "${FAIL_COMMAND:-}" != "$1" ] || exit 1
}
case "$command" in
    runuser)
        record "$@"
        shift 3
        exec "$@"
        ;;
    python3.14)
        record "$@"
        if [ "$1" = -m ] && [ "$2" = venv ]; then
            mkdir -p "$3/bin"
            ln -s "$0" "$3/bin/python"
        fi
        ;;
    python)
        record "$@"
        case "$*" in *"manage.py makemigrations"*) fail_if_requested makemigrations ;; esac
        case "$*" in *"manage.py migrate"*) fail_if_requested migrate ;; esac
        ;;
    pg_dump)
        record "$@"
        fail_if_requested pg_dump
        for argument; do case "$argument" in --file=*) backup=${argument#--file=} ;; esac; done
        : > "$backup"
        ;;
    pg_restore)
        record "$@"
        fail_if_requested pg_restore
        ;;
    id)
        [ "${1:-}" = -u ] && printf '999\n'
        ;;
    systemctl)
        record "$@"
        [ "${1:-}" != is-active ] || fail_if_requested is-active
        ;;
    chown) ;;
esac
""",
        )
        for command in (
            "chown",
            "id",
            "pg_dump",
            "pg_restore",
            "python3.14",
            "runuser",
            "scp",
            "ssh",
            "systemctl",
        ):
            (self.fake_bin / command).symlink_to(dispatcher.name)

    def artifact(self):
        source = self.temporary_path / "source"
        source.mkdir()
        (source / "requirements.txt").write_text("Django==5.2.17\n")
        (source / "manage.py").write_text("placeholder\n")
        archive = self.temporary_path / "release.tar"
        with tarfile.open(archive, "w") as tar:
            for path in source.iterdir():
                tar.add(path, path.name)
        return archive

    def run_release(self, **environment):
        with self.artifact().open("rb") as source:
            return subprocess.run(
                [str(DEPLOY_RELEASE), self.commit, "--root", str(self.fake_root)],
                cwd=Path.home(),
                stdin=source,
                env=os.environ
                | {
                    "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                    "DEPLOY_LOG": str(self.log),
                    "DEPLOY_RECORD_PYTHON": sys.executable,
                    **environment,
                },
                text=False,
                capture_output=True,
                check=False,
            )

    def test_stages_checks_backs_up_and_activates_in_order(self):
        result = self.run_release(DJANGO_SETTINGS_MODULE="preceptor_project.settings")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = self.recorded_calls()
        runuser_calls = self.calls_for("runuser")
        self.assertTrue(runuser_calls)
        self.assertTrue(all(call["argv"][:3] == ["-u", "muriel", "--"] for call in runuser_calls))
        self.assertTrue(
            any(call["argv"][3:6] == ["python3.14", "-m", "venv"] for call in runuser_calls)
        )

        venv_call = next(call for call in self.calls_for("python3.14") if "venv" in call["argv"])
        self.assertEqual(venv_call["argv"][:2], ["-m", "venv"])
        pip_call = next(
            call
            for call in self.calls_for("python")
            if call["argv"][:3] == ["-m", "pip", "install"]
        )
        pip_argv = pip_call["argv"]
        self.assertIn("--disable-pip-version-check", pip_argv)
        self.assertIn("--no-input", pip_argv)
        requirement_index = pip_argv.index("--requirement")
        self.assertEqual(pip_argv[requirement_index + 1], "requirements.txt")
        self.assertEqual(
            pip_call["environment"]["PIP_CACHE_DIR"],
            f"{self.fake_root}/srv/muriel/shared/pip-cache",
        )

        python_calls = self.calls_for("python")
        for management_command in (
            ("check", "--deploy"),
            ("makemigrations", "--check", "--dry-run"),
            ("migrate", "--noinput"),
            ("collectstatic", "--noinput"),
            ("check_production",),
        ):
            call = next(
                call
                for call in python_calls
                if "manage.py" in call["argv"]
                and all(argument in call["argv"] for argument in management_command)
            )
            self.assertEqual(
                call["environment"]["DJANGO_SETTINGS_MODULE"],
                "preceptor_project.production_settings",
            )

        backup_call = self.calls_for("pg_dump")[0]
        restore_validation = self.calls_for("pg_restore")[0]
        self.assertEqual(backup_call["environment"]["PGCLUSTER"], "18/main")
        self.assertEqual(restore_validation["environment"]["PGCLUSTER"], "18/main")
        self.assertIn("--host=/var/run/postgresql", backup_call["argv"])
        self.assertIn("--port=5432", backup_call["argv"])
        self.assertIn("--format=custom", backup_call["argv"])
        self.assertIn("--list", restore_validation["argv"])
        self.assertIn("--host=/var/run/postgresql", restore_validation["argv"])
        self.assertIn("--port=5432", restore_validation["argv"])

        migrate_index = next(
            index
            for index, call in enumerate(calls)
            if call["command"] == "python"
            and "manage.py" in call["argv"]
            and "migrate" in call["argv"]
        )
        backup_index = calls.index(backup_call)
        self.assertLess(backup_index, migrate_index)
        systemctl_calls = self.calls_for("systemctl")
        self.assertTrue(any(call["argv"][:2] == ["enable", "muriel"] for call in systemctl_calls))
        self.assertTrue(any(call["argv"][:2] == ["restart", "muriel"] for call in systemctl_calls))
        self.assertTrue(
            any(call["argv"][:3] == ["is-active", "--quiet", "muriel"] for call in systemctl_calls)
        )
        check_production_index = next(
            index
            for index, call in enumerate(calls)
            if call["command"] == "python"
            and "manage.py" in call["argv"]
            and "check_production" in call["argv"]
        )
        enable_index = next(
            index
            for index, call in enumerate(calls)
            if call["command"] == "systemctl" and call["argv"][:2] == ["enable", "muriel"]
        )
        restart_index = next(
            index
            for index, call in enumerate(calls)
            if call["command"] == "systemctl" and call["argv"][:2] == ["restart", "muriel"]
        )
        self.assertLess(check_production_index, restart_index)
        self.assertLess(enable_index, restart_index)
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.commit}"
        )
        self.assertTrue(
            (self.fake_root / "srv/muriel/releases" / self.commit / "manage.py").is_file()
        )

    def test_requires_the_bootstrap_managed_pip_cache(self):
        shutil.rmtree(self.fake_root / "srv/muriel/shared/pip-cache")

        result = self.run_release()

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"pip cache directory is missing or unsafe", result.stderr)

    def test_pending_model_changes_leave_the_current_release_untouched(self):
        result = self.run_release(FAIL_COMMAND="makemigrations")

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"before activation", result.stderr)
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.old_commit}"
        )
        self.assertNotIn("pg_dump", self.log.read_text())

    def test_unhealthy_service_reports_migration_recovery_guidance(self):
        result = self.run_release(FAIL_COMMAND="is-active")

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"migration", result.stderr.lower())
        self.assertIn(b"backup", result.stderr.lower())
        self.assertTrue(
            any(call["argv"][:2] == ["restart", "muriel"] for call in self.calls_for("systemctl"))
        )
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.commit}"
        )

    def test_migration_failure_reports_a_backup_accepted_by_restore_validation(self):
        result = self.run_release(FAIL_COMMAND="migrate")

        self.assertEqual(result.returncode, 1)
        reported = re.search(rb"(/[^\s]+pre-deploy-[^\s]+\.dump)", result.stderr)
        self.assertIsNotNone(reported, result.stderr.decode())
        reported_backup = reported.group(1).decode()
        backup = Path(reported_backup)
        self.assertTrue(backup.is_file())
        self.assertTrue(Path(f"{backup}.metadata").is_file())
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.old_commit}"
        )
        self.assertFalse(
            any(call["argv"][:2] == ["restart", "muriel"] for call in self.calls_for("systemctl"))
        )
        self.assertFalse(list((self.fake_root / "srv/muriel/releases").glob(".staging-*")))

        restore = subprocess.run(
            [str(RESTORE), "--backup", reported_backup, "--target", "recovery", "--confirm"],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "DEPLOY_LOG": str(self.log),
                "DEPLOY_RECORD_PYTHON": sys.executable,
                "MURIEL_ENVIRONMENT_FILE": str(self.fake_root / "etc/muriel/environment"),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(restore.returncode, 0, restore.stderr)

    def test_backup_failure_keeps_the_current_release_and_cleans_the_staging_release(self):
        result = self.run_release(FAIL_COMMAND="pg_dump")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.old_commit}"
        )
        self.assertFalse(
            any(call["argv"][:2] == ["restart", "muriel"] for call in self.calls_for("systemctl"))
        )
        self.assertFalse(
            any(
                call["command"] == "python"
                and "manage.py" in call["argv"]
                and "migrate" in call["argv"]
                for call in self.recorded_calls()
            )
        )
        self.assertFalse(list((self.fake_root / "srv/muriel/releases").glob(".staging-*")))

    def test_missing_secret_refuses_before_backup_or_activation(self):
        environment = self.fake_root / "etc/muriel/environment"
        environment.write_text("PRECEPTOR_POSTGRES_NAME=muriel\nPRECEPTOR_POSTGRES_USER=muriel\n")

        result = self.run_release()

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"PRECEPTOR_PRODUCTION_SECRET_KEY", result.stderr)
        self.assertNotIn("pg_dump", self.log.read_text() if self.log.exists() else "")
        self.assertEqual(
            os.readlink(self.fake_root / "srv/muriel/current"), f"releases/{self.old_commit}"
        )


class ServiceOperationTests(SimpleTestCase):
    def test_systemd_and_operator_helpers_own_the_service_lifecycle(self):
        service = ConfigParser(interpolation=None)
        service.optionxform = str
        service.read(ROOT / "ops/systemd/muriel.service")

        self.assertEqual(service["Service"]["User"], "muriel")
        self.assertEqual(service["Service"]["WorkingDirectory"], "/srv/muriel/current")
        self.assertEqual(service["Service"]["EnvironmentFile"], "/etc/muriel/environment")
        self.assertEqual(
            service["Service"]["ExecStart"], "/srv/muriel/current/scripts/start-production"
        )
        self.assertEqual(service["Service"]["RuntimeDirectory"], "muriel")
        self.assertEqual(service["Service"]["KillSignal"], "SIGTERM")
        self.assertEqual(service["Service"]["StandardOutput"], "journal")
        self.assertEqual(service["Service"]["StandardError"], "journal")
        # The accepted operations specify these directives, but no external consumer
        # establishes their numeric thresholds as a text contract.
        self.assertEqual(service["Service"]["TimeoutStopSec"], "60")
        self.assertEqual(service["Service"]["LimitNOFILE"], "4096")
        self.assertEqual(service["Install"]["WantedBy"], "multi-user.target")

    def service_helper_fixture(self, temporary_path):
        fake_bin = temporary_path / "bin"
        log = temporary_path / "commands.log"
        fake_bin.mkdir()

        def write_executable(name, content):
            path = fake_bin / name
            path.write_text(content)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

        write_executable(
            "ssh",
            """#!/bin/sh
set -eu
if [ "$SERVICE_HELPER_INTERACTIVE" = 1 ]; then
    [ "$1" = -tt ] || exit 1
    printf '%s\\n' ssh-tty >> "$SERVICE_HELPER_LOG"
    shift
else
    [ "$1" = -T ] && [ "$2" = -o ] && [ "$3" = BatchMode=yes ] || exit 1
    shift 3
fi
[ "$1" = "$SERVICE_HELPER_TARGET" ]
printf 'host=%s\\n' "$1" >> "$SERVICE_HELPER_LOG"
shift
[ "$#" -eq 1 ]
if [ "$SERVICE_HELPER_SSH_EXIT" -ne 0 ]; then
    printf '%s\\n' 'ssh diagnostic' >&2
    exit "$SERVICE_HELPER_SSH_EXIT"
fi
exec sh -c "$1"
""",
        )
        write_executable(
            "systemctl",
            """#!/bin/sh
set -eu
printf 'systemctl' >> "$SERVICE_HELPER_LOG"
for argument; do printf ' %s' "$argument" >> "$SERVICE_HELPER_LOG"; done
printf '\\n' >> "$SERVICE_HELPER_LOG"
case "$1" in
    status)
        [ "$#" -eq 3 ] || exit 1
        service=0
        pager=0
        for argument; do
            [ "$argument" != "$SERVICE_HELPER_STATUS_UNIT" ] || service=$((service + 1))
            [ "$argument" != --no-pager ] || pager=$((pager + 1))
        done
        [ "$service" -eq 1 ] && [ "$pager" -eq 1 ]
        printf '%s\\n' 'service status output'
        printf '%s\\n' 'service status diagnostic' >&2
        exit "${SERVICE_HELPER_STATUS_EXIT:-0}"
        ;;
    restart)
        [ "$#" -eq 2 ] && [ "$2" = muriel ] || exit 1
        printf '%s\\n' 'restart output'
        printf '%s\\n' 'restart diagnostic' >&2
        exit "$SERVICE_HELPER_RESTART_EXIT"
        ;;
    is-active)
        printf '%s\\n' 'active check output'
        printf '%s\\n' 'active check diagnostic' >&2
        exit "$SERVICE_HELPER_ACTIVE_EXIT"
        ;;
    *) exit 1 ;;
esac
""",
        )
        write_executable(
            "journalctl",
            """#!/bin/sh
set -eu
printf 'journalctl' >> "$SERVICE_HELPER_LOG"
for argument; do printf ' %s' "$argument" >> "$SERVICE_HELPER_LOG"; done
printf '\\n' >> "$SERVICE_HELPER_LOG"
printf '%s\\n' 'journal output'
printf '%s\\n' 'journal diagnostic' >&2
exit "${SERVICE_HELPER_LOGS_EXIT:-0}"
""",
        )
        write_executable(
            "sudo",
            """#!/bin/sh
set -eu
printf '%s\\n' sudo >> "$SERVICE_HELPER_LOG"
[ "$SERVICE_HELPER_INTERACTIVE" = 1 ]
exec "$@"
""",
        )
        return fake_bin, log

    def run_service_helper(self, helper, fake_bin, log, arguments=None, **environment):
        controlled_environment = os.environ | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SERVICE_HELPER_LOG": str(log),
            "SERVICE_HELPER_TARGET": "",
            "SERVICE_HELPER_INTERACTIVE": "1" if helper == "restart" else "0",
            "SERVICE_HELPER_RESTART_EXIT": "0",
            "SERVICE_HELPER_ACTIVE_EXIT": "0",
            "SERVICE_HELPER_SSH_EXIT": "0",
            "SERVICE_HELPER_STATUS_EXIT": "0",
            "SERVICE_HELPER_STATUS_UNIT": (
                "muriel-staging.service" if helper == "status-staging" else "muriel.service"
            ),
            "SERVICE_HELPER_LOGS_EXIT": "0",
        }
        controlled_environment.pop("MURIEL_SSH_TARGET", None)
        controlled_environment.update(environment)
        return subprocess.run(
            [
                str(ROOT / "ops" / helper),
                *(arguments if arguments is not None else ["production-a"]),
            ],
            cwd=Path.home(),
            env=controlled_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_restart_selects_configured_alias_and_executes_privileged_operations_in_order(self):
        for target in ("production-a", "7prod_B.example"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                fake_bin, log = self.service_helper_fixture(Path(temporary_directory))
                result = self.run_service_helper(
                    "restart",
                    fake_bin,
                    log,
                    arguments=[target],
                    MURIEL_SSH_TARGET=target,
                    SERVICE_HELPER_TARGET=target,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = log.read_text().splitlines()
                self.assertEqual(commands[:3], ["ssh-tty", f"host={target}", "sudo"])
                self.assertEqual(commands[4], "sudo")
                self.assertEqual(len(commands), 6)
                restart, active = map(shlex.split, (commands[3], commands[5]))
                self.assertEqual(restart, ["systemctl", "restart", "muriel"])
                self.assertEqual(active[:2], ["systemctl", "is-active"])
                self.assertCountEqual(active[2:], ["--quiet", "muriel"])
                self.assertEqual(result.stdout, "restart output\nactive check output\n")
                self.assertEqual(result.stderr, "restart diagnostic\nactive check diagnostic\n")

    def test_logs_selects_configured_alias_and_preserves_bounded_journal_result(self):
        for helper, unit in (("logs", "muriel"), ("logs-staging", "muriel-staging")):
            # Alias spelling does not select the journal, even on a shared host.
            for target in ("production-a", "7prod_B.example", "staging-a"):
                for exit_status in (0, 1):
                    with (
                        self.subTest(helper=helper, target=target, exit_status=exit_status),
                        tempfile.TemporaryDirectory() as temporary_directory,
                    ):
                        fake_bin, log = self.service_helper_fixture(Path(temporary_directory))
                        result = self.run_service_helper(
                            helper,
                            fake_bin,
                            log,
                            arguments=[target],
                            MURIEL_SSH_TARGET=target,
                            SERVICE_HELPER_TARGET=target,
                            SERVICE_HELPER_LOGS_EXIT=str(exit_status),
                        )
                        self.assertEqual(result.returncode, exit_status, result.stderr)
                        self.assertEqual(result.stdout, "journal output\n")
                        self.assertEqual(result.stderr, "journal diagnostic\n")
                        commands = log.read_text().splitlines()
                        self.assertEqual(commands[0], f"host={target}")
                        self.assertEqual(len(commands), 2)
                        logs = shlex.split(commands[1])
                        self.assertEqual(logs[0], "journalctl")
                        self.assertCountEqual(logs[1:], ["--no-pager", "-u", unit, "-n", "100"])
                        self.assertEqual(logs[logs.index("-u") + 1], unit)
                        self.assertEqual(logs[logs.index("-n") + 1], "100")

    def test_status_selects_configured_alias_and_preserves_remote_result(self):
        for helper, unit in (
            ("status", "muriel.service"),
            ("status-staging", "muriel-staging.service"),
        ):
            # The same installation alias may host both services; the wrapper
            # must select its fixed unit independently of the alias spelling.
            for target in ("production-a", "7prod_B.example"):
                for exit_status in (0, 3):
                    with (
                        self.subTest(helper=helper, target=target, exit_status=exit_status),
                        tempfile.TemporaryDirectory() as temporary_directory,
                    ):
                        fake_bin, log = self.service_helper_fixture(Path(temporary_directory))
                        result = self.run_service_helper(
                            helper,
                            fake_bin,
                            log,
                            arguments=[target],
                            MURIEL_SSH_TARGET=target,
                            SERVICE_HELPER_TARGET=target,
                            SERVICE_HELPER_STATUS_EXIT=str(exit_status),
                        )
                        self.assertEqual(result.returncode, exit_status, result.stderr)
                        self.assertEqual(result.stdout, "service status output\n")
                        self.assertEqual(result.stderr, "service status diagnostic\n")
                        commands = log.read_text().splitlines()
                        self.assertEqual(commands[0], f"host={target}")
                        self.assertEqual(len(commands), 2)
                        status = shlex.split(commands[1])
                        self.assertEqual(status[:2], ["systemctl", "status"])
                        self.assertCountEqual(status[2:], ["--no-pager", unit])

    def test_service_helpers_preserve_ssh_failure(self):
        for helper in SERVICE_HELPERS:
            with (
                self.subTest(helper=helper),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                fake_bin, log = self.service_helper_fixture(Path(temporary_directory))
                result = self.run_service_helper(
                    helper,
                    fake_bin,
                    log,
                    arguments=["production-a"],
                    MURIEL_SSH_TARGET="production-a",
                    SERVICE_HELPER_TARGET="production-a",
                    SERVICE_HELPER_SSH_EXIT="255",
                )
                self.assertEqual(result.returncode, 255)
                self.assertEqual(result.stderr, "ssh diagnostic\n")
                self.assertEqual(result.stdout, "")
                expected = ["ssh-tty"] if helper == "restart" else []
                self.assertEqual(log.read_text().splitlines(), [*expected, "host=production-a"])

    def test_service_helpers_refuse_unauthorized_targets_before_ssh(self):
        invalid_targets = (
            None,
            "",
            " production-a",
            "production-a ",
            "prod host",
            "prod\thost",
            "production-a\n",
            "prod\rhost",
            "prod\x01host",
            "-production-a",
            ".production-a",
            "_production-a",
            "prod;echo injected",
            "$(echo injected)",
            "`echo injected`",
            "prod*",
            "prod?",
            "prod[ab]",
            "operator@production-a",
            "ssh://production-a",
            "pröd",
        )
        cases = [(target, [target or "production-a"]) for target in invalid_targets]
        cases += [
            ("production-a", []),
            ("production-a", ["production-a", "extra"]),
            ("production-a", ["production-b"]),
            ("production-a", ["PRODUCTION-A"]),
            ("production-a", ["-option"]),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            log = temporary_path / "ssh-called"
            ssh = fake_bin / "ssh"
            ssh.write_text('#!/bin/sh\nprintf called > "$SERVICE_HELPER_LOG"\nexit 99\n')
            ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
            for helper in SERVICE_HELPERS:
                for target, arguments in cases:
                    with self.subTest(helper=helper, target=target, arguments=arguments):
                        environment = {} if target is None else {"MURIEL_SSH_TARGET": target}
                        result = self.run_service_helper(
                            helper, fake_bin, log, arguments=arguments, **environment
                        )
                        self.assertEqual(result.returncode, 2)
                        self.assertIn("MURIEL_SSH_TARGET", result.stderr)
                        self.assertFalse(log.exists(), "Refusal must happen before invoking SSH")

    def test_restart_propagates_operation_failures_and_short_circuits(self):
        for operation, exit_status in (("RESTART", 7), ("ACTIVE", 3)):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                fake_bin, log = self.service_helper_fixture(Path(temporary_directory))
                result = self.run_service_helper(
                    "restart",
                    fake_bin,
                    log,
                    MURIEL_SSH_TARGET="production-a",
                    SERVICE_HELPER_TARGET="production-a",
                    **{f"SERVICE_HELPER_{operation}_EXIT": str(exit_status)},
                )
                self.assertEqual(result.returncode, exit_status)
                commands = log.read_text().splitlines()
                self.assertEqual(
                    commands[:4],
                    ["ssh-tty", "host=production-a", "sudo", "systemctl restart muriel"],
                )
                if operation == "RESTART":
                    self.assertEqual(len(commands), 4)
                    self.assertEqual(result.stdout, "restart output\n")
                    self.assertEqual(result.stderr, "restart diagnostic\n")
                else:
                    self.assertEqual(len(commands), 6)
                    self.assertEqual(commands[4], "sudo")
                    active = shlex.split(commands[5])
                    self.assertEqual(active[:2], ["systemctl", "is-active"])
                    self.assertCountEqual(active[2:], ["--quiet", "muriel"])
                    self.assertEqual(result.stdout, "restart output\nactive check output\n")
                    self.assertEqual(result.stderr, "restart diagnostic\nactive check diagnostic\n")

    def test_helpers_reject_invalid_arguments_without_connecting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_bin = Path(temporary_directory) / "bin"
            fake_bin.mkdir()
            ssh = fake_bin / "ssh"
            ssh.write_text("#!/bin/sh\nexit 99\n")
            ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)

            for helper in SERVICE_HELPERS:
                for arguments in ([], ["other-host"], ["production-a", "extra"]):
                    with self.subTest(helper=helper, arguments=arguments):
                        result = subprocess.run(
                            [str(ROOT / "ops" / helper), *arguments],
                            cwd=Path.home(),
                            env=os.environ
                            | {
                                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                                "MURIEL_SSH_TARGET": "production-a",
                            },
                            text=True,
                            capture_output=True,
                            check=False,
                        )

                        self.assertEqual(result.returncode, 2)
                        self.assertTrue(result.stderr)
                        if len(arguments) != 1:
                            self.assertIn("Usage:", result.stderr)
