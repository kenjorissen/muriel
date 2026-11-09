import copy
import json
import math
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .staging_identity_fixtures import INVALID_STAGING_PAIRS, STAGING_HOST, STAGING_ORIGIN

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_STAGING_RELEASE = ROOT / "ops" / "deploy-staging-release"
UPDATE_STAGING = ROOT / "ops" / "update-staging"
UPDATE_STAGING_REMOTE = ROOT / "ops" / "update-staging-remote"


class StagingReleaseDeploymentTests(SimpleTestCase):
    commit = "a" * 40
    old_commit = "b" * 40

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "host"
        self.fake_bin = self.temporary_path / "bin"
        self.log = self.temporary_path / "commands.log"
        self.root.mkdir()
        self.fake_bin.mkdir()
        (self.root / "srv/muriel-staging/releases" / self.old_commit).mkdir(parents=True)
        (self.root / "srv/muriel-staging/shared/pip-cache").mkdir(parents=True)
        environment = self.root / "etc/muriel-staging/environment"
        environment.parent.mkdir(parents=True)
        environment.write_text(
            "PRECEPTOR_STAGING_SECRET_KEY=" + "s" * 50 + "\n"
            "PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test\n"
            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000\n"
            "PRECEPTOR_STAGING_POSTGRES_NAME=muriel_staging\n"
            "PRECEPTOR_STAGING_POSTGRES_USER=muriel_staging\n"
        )
        (self.root / "srv/muriel-staging/current").symlink_to(f"releases/{self.old_commit}")
        production_release = self.root / "srv/muriel/releases/production-sentinel"
        production_release.mkdir(parents=True)
        (self.root / "srv/muriel/current").symlink_to("releases/production-sentinel")
        production_environment = self.root / "etc/muriel/environment"
        production_environment.parent.mkdir(parents=True)
        production_environment.write_text("production environment sentinel\n")
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
    "$STAGING_DEPLOY_RECORD_PYTHON" -c '
import json
import os
import sys

record = {
    "command": sys.argv[2],
    "argv": sys.argv[3:],
    "environment": {
        name: os.environ.get(name, "")
        for name in ("DJANGO_SETTINGS_MODULE", "PIP_CACHE_DIR")
    },
}
with open(sys.argv[1], "ab") as output:
    output.write(json.dumps(record).encode() + b"\\0")
' "$STAGING_DEPLOY_LOG" "$command" "$@"
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
        case "$*" in *"manage.py makemigrations"*) [ "${STAGING_FAIL:-}" != makemigrations ] ;; esac
        ;;
    systemctl) record "$@" ;;
    chown) ;;
esac
""",
        )
        for command in ("chown", "id", "python3.14", "runuser", "systemctl"):
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
                [str(DEPLOY_STAGING_RELEASE), self.commit, "--root", str(self.root)],
                cwd=Path.home(),
                stdin=source,
                env=os.environ
                | {
                    "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                    "STAGING_DEPLOY_LOG": str(self.log),
                    "STAGING_DEPLOY_RECORD_PYTHON": sys.executable,
                    **environment,
                },
                text=False,
                capture_output=True,
                check=False,
            )

    def test_uses_only_staging_paths_database_settings_and_service(self):
        result = self.run_release()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        runuser_calls = self.calls_for("runuser")
        self.assertTrue(runuser_calls)
        self.assertTrue(
            all(call["argv"][:3] == ["-u", "muriel_staging", "--"] for call in runuser_calls)
        )
        self.assertTrue(
            any(call["argv"][3:6] == ["python3.14", "-m", "venv"] for call in runuser_calls)
        )
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
            f"{self.root}/srv/muriel-staging/shared/pip-cache",
        )
        check_call = next(
            call
            for call in self.calls_for("python")
            if "manage.py" in call["argv"] and "check_staging" in call["argv"]
        )
        self.assertEqual(
            check_call["environment"]["DJANGO_SETTINGS_MODULE"],
            "preceptor_project.staging_settings",
        )
        systemctl_calls = self.calls_for("systemctl")
        self.assertTrue(
            any(call["argv"][:2] == ["enable", "muriel-staging"] for call in systemctl_calls)
        )
        self.assertTrue(
            any(call["argv"][:2] == ["restart", "muriel-staging"] for call in systemctl_calls)
        )
        self.assertTrue(
            all(
                "muriel" not in call["argv"] and "muriel.service" not in call["argv"]
                for call in systemctl_calls
            ),
        )
        self.assertEqual(
            os.readlink(self.root / "srv/muriel-staging/current"), f"releases/{self.commit}"
        )
        self.assertEqual(
            os.readlink(self.root / "srv/muriel/current"), "releases/production-sentinel"
        )
        self.assertEqual(
            (self.root / "etc/muriel/environment").read_text(), "production environment sentinel\n"
        )

    def test_requires_the_bootstrap_managed_pip_cache(self):
        shutil.rmtree(self.root / "srv/muriel-staging/shared/pip-cache")

        result = self.run_release()

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"staging pip cache directory is missing or unsafe", result.stderr)

    def test_pre_activation_failure_keeps_the_previous_staging_release(self):
        result = self.run_release(STAGING_FAIL="makemigrations")

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"before activation", result.stderr)
        self.assertEqual(
            os.readlink(self.root / "srv/muriel-staging/current"), f"releases/{self.old_commit}"
        )
        self.assertEqual(
            os.readlink(self.root / "srv/muriel/current"), "releases/production-sentinel"
        )
        self.assertFalse(
            any(
                call["argv"][:2] == ["restart", "muriel-staging"]
                for call in self.calls_for("systemctl")
            )
        )
        self.assertFalse(list((self.root / "srv/muriel-staging/releases").glob(".staging-*")))


class StagingUpdateTests(SimpleTestCase):
    def setUp(self):
        # Never inherit the operator's installation selection in a fake-host fixture.
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "MURIEL_SSH_TARGET": "staging-a",
                    "PRECEPTOR_STAGING_ALLOWED_HOST": STAGING_HOST,
                    "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": STAGING_ORIGIN,
                },
            )
        )

    def test_invalid_selection_and_invocation_refuse_before_any_work(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            log = temporary_path / "commands.log"
            for name in ("git", "mktemp", "ssh", "scp", "sudo"):
                command = fake_bin / name
                command.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$REFUSAL_LOG"\nexit 99\n')
                command.chmod(command.stat().st_mode | stat.S_IXUSR)

            alias = "node-A_1.test"
            cases = [(None, [alias]), ("", [alias]), (alias, ["another-node"])]
            for invalid in (
                "-option",
                ".node",
                "_node",
                "user@host",
                "ssh://host",
                "host:22",
                " node",
                "node ",
                "node\n",
                "node*",
                "node;id",
                "$(id)",
                "nödé",
            ):
                cases.append((invalid, [invalid]))
            for arguments in (
                [],
                [""],
                [alias, alias],
                ["", alias],
                [alias, ""],
                [alias, "other"],
                [alias, "--unknown"],
                ["--candidate"],
                [alias, "--candidate", "--candidate"],
                ["--reset-and-load", alias, "--reset-and-load"],
            ):
                cases.append((alias, arguments))
            for arguments in (
                [alias, "--candidate", "--reset-and-load"],
                [alias, "--reset-and-load", "--candidate"],
                ["--candidate", alias, "--reset-and-load"],
                ["--reset-and-load", alias, "--candidate"],
                ["--candidate", "--reset-and-load", alias],
                ["--reset-and-load", "--candidate", alias],
            ):
                cases.append((alias, arguments))
            for configuration, arguments in cases:
                with self.subTest(configuration=configuration, arguments=arguments):
                    environment = os.environ | {
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "REFUSAL_LOG": str(log),
                    }
                    environment.pop("MURIEL_SSH_TARGET", None)
                    if configuration is not None:
                        environment["MURIEL_SSH_TARGET"] = configuration
                    result = subprocess.run(
                        [str(UPDATE_STAGING), *arguments],
                        cwd=Path.home(),
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(log.exists(), "invalid invocation performed work")

    def test_invalid_identity_refuses_locally_before_any_work(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            log = Path(directory) / "commands.log"
            for name in ("git", "mktemp", "ssh", "scp"):
                command = fake_bin / name
                command.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$REFUSAL_LOG"\nexit 99\n')
                command.chmod(0o700)
            for host, origin in INVALID_STAGING_PAIRS:
                with self.subTest(host=host, origin=origin):
                    environment = os.environ | {
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "REFUSAL_LOG": str(log),
                    }
                    for key, value in (
                        ("PRECEPTOR_STAGING_ALLOWED_HOST", host),
                        ("PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN", origin),
                    ):
                        environment.pop(key, None)
                        if value is not None:
                            environment[key] = value
                    result = subprocess.run(
                        [str(UPDATE_STAGING), "staging-a"],
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("Staging identity requires", result.stderr)
                    self.assertFalse(log.exists())

    def test_hands_the_verified_staging_helper_to_sudo_for_a_synthetic_load_and_cleans_files(self):
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            checkout = temporary_path / "checkout"
            fake_bin = temporary_path / "bin"
            log = temporary_path / "commands.log"
            upload = temporary_path / "uploaded.tar"
            expected_helper = temporary_path / "expected-helper"
            token = "".join(character for character in temporary_path.name if character.isalnum())[
                -6:
            ]
            remote_archive = f"/tmp/muriel-staging-{commit}.{token}.tar"
            remote_helper = f"/tmp/muriel-staging-{commit}.{token}.sh"
            checkout.joinpath("ops").mkdir(parents=True)
            fake_bin.mkdir()
            shutil.copy2(UPDATE_STAGING, checkout / "ops" / "update-staging")
            shutil.copy2(ROOT / "ops/require-ssh-target.sh", checkout / "ops/require-ssh-target.sh")
            shutil.copy2(
                ROOT / "ops/require-staging-identity.sh",
                checkout / "ops/require-staging-identity.sh",
            )
            shutil.copy2(UPDATE_STAGING_REMOTE, expected_helper)

            source = temporary_path / "source"
            source.joinpath("ops").mkdir(parents=True)
            shutil.copy2(UPDATE_STAGING_REMOTE, source / "ops" / "update-staging-remote")
            artifact = temporary_path / "source.tar"
            with tarfile.open(artifact, "w") as archive:
                archive.add(source / "ops" / "update-staging-remote", "ops/update-staging-remote")
                archive.add(
                    ROOT / "ops/require-staging-identity.sh", "ops/require-staging-identity.sh"
                )

            def write_executable(name, content):
                path = fake_bin / name
                path.write_text(content)
                path.chmod(path.stat().st_mode | stat.S_IXUSR)

            write_executable(
                "git",
                """#!/bin/sh
set -eu
while [ "${1:-}" = -C ]; do shift 2; done
case "$1" in
    status) ;;
    rev-parse) printf '%s\\n' "$UPDATE_COMMIT" ;;
    archive) cat "$UPDATE_ARTIFACT" ;;
    *) exit 1 ;;
esac
""",
            )
            write_executable(
                "scp",
                """#!/bin/sh
set -eu
[ "$#" -eq 2 ] && [ "${2%%:*}" = "$MURIEL_SSH_TARGET" ] || exit 98
printf 'local-artifact=%s\\n' "$1" >> "$UPDATE_LOG"
cp "$1" "$UPDATE_UPLOAD"
cp "$1" "$UPDATE_REMOTE_ARCHIVE"
""",
            )
            write_executable(
                "ssh",
                """#!/bin/sh
set -eu
if [ "$1" = -tt ]; then shift; fi
[ "$#" -eq 2 ] && [ "$1" = "$MURIEL_SSH_TARGET" ] || exit 98
command=$2
case "$command" in
    *"mktemp --suffix=.tar"*)
        printf '%s\\n' "$UPDATE_REMOTE_ARCHIVE"
        ;;
    *"mktemp --suffix=.sh"*)
        : > "$UPDATE_REMOTE_HELPER"
        printf '%s\\n' "$UPDATE_REMOTE_HELPER"
        ;;
    *"tar -xOf"*)
        sh -c "$command"
        ;;
    "sudo sh "*)
        if [ -n "${UPDATE_SESSION_FAILURE:-}" ]; then exit "$UPDATE_SESSION_FAILURE"; fi
        eval "set -- $command"
        helper=$3
        commit=$4
        artifact=$5
        reset=$6
        [ "$commit" = "$UPDATE_COMMIT" ]
        [ "$artifact" = "$UPDATE_REMOTE_ARCHIVE" ]
        [ "$reset" = 1 ]
        [ "$#" -eq 8 ]
        [ "$7" = "$PRECEPTOR_STAGING_ALLOWED_HOST" ]
        [ "$8" = "$PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN" ]
        cmp -s "$helper" "$UPDATE_EXPECTED_HELPER"
        printf 'sudo-helper-matches-archive\\n' >> "$UPDATE_LOG"
        rm -f -- "$artifact" "$helper"
        printf 'cleanup-staging-files\\n' >> "$UPDATE_LOG"
        ;;
    *"rm -f --"*)
        rm -f -- "$UPDATE_REMOTE_ARCHIVE" "$UPDATE_REMOTE_HELPER"
        printf 'cleanup-staging-files\\n' >> "$UPDATE_LOG"
        ;;
    *) exit 1 ;;
esac
""",
            )

            result = subprocess.run(
                [str(checkout / "ops" / "update-staging"), "staging-a", "--reset-and-load"],
                cwd=Path.home(),
                env=os.environ
                | {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "UPDATE_ARTIFACT": str(artifact),
                    "UPDATE_COMMIT": commit,
                    "UPDATE_EXPECTED_HELPER": str(expected_helper),
                    "UPDATE_LOG": str(log),
                    "UPDATE_REMOTE_ARCHIVE": remote_archive,
                    "UPDATE_REMOTE_HELPER": remote_helper,
                    "UPDATE_UPLOAD": str(upload),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text().splitlines()
            self.assertEqual(commands.count("sudo-helper-matches-archive"), 1)
            self.assertIn("cleanup-staging-files", commands)
            self.assertNotIn("tailscale", "\n".join(commands))
            self.assertFalse(Path(remote_archive).exists())
            self.assertFalse(Path(remote_helper).exists())
            for command in commands:
                if command.startswith("local-artifact="):
                    self.assertFalse(Path(command.removeprefix("local-artifact=")).exists())

            for session_failure in ("130", "255"):
                with self.subTest(session_failure=session_failure):
                    result = subprocess.run(
                        [str(checkout / "ops" / "update-staging"), "staging-a", "--reset-and-load"],
                        cwd=Path.home(),
                        env=os.environ
                        | {
                            "PATH": f"{fake_bin}:{os.environ['PATH']}",
                            "UPDATE_ARTIFACT": str(artifact),
                            "UPDATE_COMMIT": commit,
                            "UPDATE_EXPECTED_HELPER": str(expected_helper),
                            "UPDATE_LOG": str(log),
                            "UPDATE_REMOTE_ARCHIVE": remote_archive,
                            "UPDATE_REMOTE_HELPER": remote_helper,
                            "UPDATE_SESSION_FAILURE": session_failure,
                            "UPDATE_UPLOAD": str(upload),
                        },
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, int(session_failure), result.stderr)
                    self.assertNotIn("Staging update deployed", result.stdout)
                    self.assertFalse(Path(remote_archive).exists())
                    self.assertFalse(Path(remote_helper).exists())

    def git(self, checkout, *arguments, input=None):
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            input=input,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def create_checkout(self, temporary_path):
        checkout = temporary_path / "checkout"
        checkout.joinpath("ops").mkdir(parents=True)
        shutil.copy2(UPDATE_STAGING, checkout / "ops/update-staging")
        shutil.copy2(ROOT / "ops/require-ssh-target.sh", checkout / "ops/require-ssh-target.sh")
        shutil.copy2(
            ROOT / "ops/require-staging-identity.sh", checkout / "ops/require-staging-identity.sh"
        )
        shutil.copy2(UPDATE_STAGING_REMOTE, checkout / "ops/update-staging-remote")
        self.git(checkout, "init", "--quiet")
        self.git(checkout, "config", "user.name", "Test User")
        self.git(checkout, "config", "user.email", "test@example.com")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "--quiet", "-m", "accepted base")
        return checkout

    def commit_file(self, checkout, name):
        checkout.joinpath(f"{name}.txt").write_text(f"{name}\n")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "--quiet", "-m", name)
        return self.git(checkout, "rev-parse", "HEAD")

    def create_candidate(self, checkout, name="candidate"):
        accepted_commit = self.git(checkout, "rev-parse", "HEAD")
        self.git(checkout, "switch", "--quiet", "--create", name)
        candidate_commit = self.commit_file(checkout, name)
        self.git(
            checkout, "update-ref", f"refs/cassidy/attempts/{name}/candidate", candidate_commit
        )
        self.git(checkout, "switch", "--quiet", "--detach", accepted_commit)
        return candidate_commit

    def write_remote_stubs(self, temporary_path):
        fake_bin = temporary_path / "bin"
        log = temporary_path / "commands.log"
        upload = temporary_path / "uploaded.tar"
        remote_archive = temporary_path / "remote.tar"
        remote_helper = temporary_path / "remote.sh"
        fake_bin.mkdir()
        stubs = {
            "scp": """#!/bin/sh
set -eu
printf 'archive=%s\\n' "$1" >> "$CANDIDATE_TEST_LOG"
printf 'scp-target=%s\\n' "$2" >> "$CANDIDATE_TEST_LOG"
[ "${2%%:*}" = "$MURIEL_SSH_TARGET" ] || exit 98
cp "$1" "$CANDIDATE_TEST_UPLOAD"
cp "$1" "${2#*:}"
""",
            "ssh": """#!/bin/sh
set -eu
if [ "$1" = -tt ]; then
    printf 'interactive-session\\n' >> "$CANDIDATE_TEST_LOG"
    shift
fi
[ "$#" -eq 2 ] && [ "$1" = "$MURIEL_SSH_TARGET" ] || exit 98
printf 'ssh-target=%s\\n' "$1" >> "$CANDIDATE_TEST_LOG"
command=$2
commit=${command#*/muriel-staging-}
commit=${commit%%.*}
case "$command" in
    *"mktemp --suffix=.tar"*) printf '/tmp/muriel-staging-%s.abc123.tar\\n' "$commit" ;;
    *"mktemp --suffix=.sh"*) helper="/tmp/muriel-staging-$commit.abc123.sh"; : > "$helper"; printf '%s\\n' "$helper" ;;
    *"tar -xOf"*) sh -c "$command" ;;
    "sudo sh "*)
        eval "set -- $command"
        [ "$#" -eq 8 ]
        printf 'sudo commit=%s reset=%s\\n' "$4" "$6" >> "$CANDIDATE_TEST_LOG"
        printf 'identity=%s %s\\n' "$7" "$8" >> "$CANDIDATE_TEST_LOG"
        rm -f -- "$3" "$5"
        ;;
    *"rm -f --"*) : ;;
    *) exit 99 ;;
esac
""",
        }
        for name, content in stubs.items():
            path = fake_bin / name
            path.write_text(content)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return fake_bin, log, upload, remote_archive, remote_helper

    def run_update(
        self,
        checkout,
        fake_bin,
        log,
        upload,
        remote_archive,
        remote_helper,
        *arguments,
        target="staging-a",
    ):
        return subprocess.run(
            [str(checkout / "ops/update-staging"), *arguments],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "MURIEL_SSH_TARGET": target,
                "CANDIDATE_TEST_LOG": str(log),
                "CANDIDATE_TEST_UPLOAD": str(upload),
                "CANDIDATE_TEST_REMOTE_ARCHIVE": str(remote_archive),
                "CANDIDATE_TEST_REMOTE_HELPER": str(remote_helper),
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_configured_alias_is_retained_across_mode_and_argument_order_selection(self):
        for alias in ("node-A_1.test", "7.build-node"):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                temporary_path = Path(directory)
                checkout = self.create_checkout(temporary_path)
                accepted = self.git(checkout, "rev-parse", "HEAD")
                candidate = self.create_candidate(checkout)
                stubs = self.write_remote_stubs(temporary_path)
                _, log, upload, _, _ = stubs
                for arguments, commit, reset in (
                    ([alias, "--candidate"], candidate, 0),
                    (["--candidate", alias], candidate, 0),
                    ([alias], accepted, 0),
                    ([alias, "--reset-and-load"], accepted, 1),
                    (["--reset-and-load", alias], accepted, 1),
                ):
                    with self.subTest(arguments=arguments):
                        result = self.run_update(checkout, *stubs, *arguments, target=alias)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        commands = log.read_text().splitlines()
                        self.assertIn(f"sudo commit={commit} reset={reset}", commands)
                        self.assertEqual(commands.count("interactive-session"), 1)
                        self.assertIn(f"identity={STAGING_HOST} {STAGING_ORIGIN}", commands)
                        self.assertIn(f"Staging: {STAGING_ORIGIN}/", result.stdout)
                        self.assertTrue(any(line.startswith("scp-target=") for line in commands))
                        # Every SSH/SCP stub rejects any other destination, including a
                        # Candidate ref record accidentally left in positional arguments.
                        self.assertIn(f"ssh-target={alias}", commands)
                        self.assertIn(commit, result.stdout)
                        if commit == candidate:
                            self.assertIn(
                                f"unaccepted Cassidy Candidate commit {candidate}", result.stdout
                            )
                        with tarfile.open(upload) as archive:
                            self.assertEqual(
                                "candidate.txt" in archive.getnames(), commit == candidate
                            )
                        for line in commands:
                            if line.startswith("archive="):
                                self.assertFalse(Path(line.removeprefix("archive=")).exists())
                        log.unlink()

    def test_dirty_and_untracked_sources_refuse_in_each_mode(self):
        for dirty in ("tracked", "untracked"):
            for flags in ([], ["--candidate"], ["--reset-and-load"]):
                with (
                    self.subTest(dirty=dirty, flags=flags),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    temporary_path = Path(directory)
                    checkout = self.create_checkout(temporary_path)
                    self.create_candidate(checkout)
                    path = checkout / (
                        "ops/update-staging-remote" if dirty == "tracked" else "new.txt"
                    )
                    with path.open("a") as source:
                        source.write("# local change\n")
                    stubs = self.write_remote_stubs(temporary_path)
                    result = self.run_update(checkout, *stubs, "node-a", *flags, target="node-a")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("dirty source checkout", result.stderr)
                    self.assertFalse(stubs[1].exists())
                    self.assertFalse(stubs[2].exists())

    def test_candidate_guards_fail_before_archiving_or_connecting(self):
        cases = {
            "missing": "found no live Cassidy Candidate",
            "multiple": "multiple live Cassidy Candidates",
            "malformed": "unexpected Cassidy ref",
            "not-a-commit": "does not name a commit",
            "stale": "stale or unrelated Candidate",
            "unrelated": "stale or unrelated Candidate",
        }
        for name, message in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_path = Path(temporary_directory)
                checkout = self.create_checkout(temporary_path)
                if name == "multiple":
                    self.create_candidate(checkout, "one")
                    self.create_candidate(checkout, "two")
                elif name == "malformed":
                    self.git(
                        checkout,
                        "update-ref",
                        "refs/cassidy/attempts/malformed/other",
                        self.git(checkout, "rev-parse", "HEAD"),
                    )
                elif name == "not-a-commit":
                    blob = self.git(
                        checkout, "hash-object", "-w", "--stdin", input="not a commit\n"
                    )
                    self.git(checkout, "update-ref", "refs/cassidy/attempts/blob/candidate", blob)
                elif name == "stale":
                    candidate_commit = self.create_candidate(checkout)
                    self.git(checkout, "switch", "--quiet", "--detach", candidate_commit)
                    self.commit_file(checkout, "accepted-after-candidate")
                elif name == "unrelated":
                    self.create_candidate(checkout, "candidate")
                    self.commit_file(checkout, "accepted-sibling")
                fake_bin, log, upload, remote_archive, remote_helper = self.write_remote_stubs(
                    temporary_path
                )

                result = self.run_update(
                    checkout,
                    fake_bin,
                    log,
                    upload,
                    remote_archive,
                    remote_helper,
                    "--candidate",
                    "staging-a",
                )

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse(log.exists())
                self.assertFalse(upload.exists())

    def test_candidate_rejects_reset_and_load_before_connecting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            checkout = self.create_checkout(temporary_path)
            fake_bin, log, upload, remote_archive, remote_helper = self.write_remote_stubs(
                temporary_path
            )

            result = self.run_update(
                checkout,
                fake_bin,
                log,
                upload,
                remote_archive,
                remote_helper,
                "staging-a",
                "--candidate",
                "--reset-and-load",
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("preserves data", result.stderr)
            self.assertFalse(log.exists())

    @contextmanager
    def remote_update_fixture(self, host=STAGING_HOST):
        origin = f"https://{host}:10000"
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            checkout = temporary_path / "checkout"
            source = temporary_path / "source"
            host_root = temporary_path / "host"
            fake_bin = temporary_path / "bin"
            state = temporary_path / "state"
            log = temporary_path / "commands.log"
            upload = temporary_path / "uploaded.tar"
            token = "".join(character for character in temporary_path.name if character.isalnum())[
                -6:
            ]
            remote_archive = f"/tmp/muriel-staging-{commit}.{token}.tar"
            remote_helper = f"/tmp/muriel-staging-{commit}.{token}.sh"
            checkout.joinpath("ops").mkdir(parents=True)
            source.joinpath("ops/systemd").mkdir(parents=True)
            host_root.joinpath("etc/systemd/system").mkdir(parents=True)
            fake_bin.mkdir()
            state.mkdir()
            production_tailscale_state = {
                "TCP": {
                    "443": {
                        "ProxyProtocol": 2,
                        "TCPForward": "127.0.0.1:8080",
                        "TerminateTLS": "production.example.test",
                    },
                    "8443": {"HTTPS": True},
                },
                "Web": {
                    "production.example.test:8443": {
                        "Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:8081"}}
                    }
                },
                "AllowFunnel": {"production.example.test:443": True},
            }
            persisted_tailscale_state = {
                **production_tailscale_state,
                "TCP": {**production_tailscale_state["TCP"], "10000": {"HTTPS": True}},
                "Web": {
                    **production_tailscale_state["Web"],
                    f"{host}:10000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8082"}}},
                },
            }
            (state / "tailscale-status.json").write_text(json.dumps(production_tailscale_state))
            (state / "tailscale-after.json").write_text(json.dumps(persisted_tailscale_state))
            shutil.copy2(UPDATE_STAGING, checkout / "ops/update-staging")
            shutil.copy2(ROOT / "ops/require-ssh-target.sh", checkout / "ops/require-ssh-target.sh")
            for destination in (checkout, source):
                shutil.copy2(
                    ROOT / "ops/require-staging-identity.sh",
                    destination / "ops/require-staging-identity.sh",
                )
            shutil.copy2(UPDATE_STAGING_REMOTE, source / "ops/update-staging-remote")
            shutil.copy2(ROOT / "ops/bootstrap-ubuntu", source / "ops/bootstrap-ubuntu")
            shutil.copy2(
                ROOT / "ops/bootstrap-staging-ubuntu", source / "ops/bootstrap-staging-ubuntu"
            )
            shutil.copy2(
                ROOT / "ops/create-staging-superuser", source / "ops/create-staging-superuser"
            )
            shutil.copy2(
                ROOT / "ops/systemd/muriel-staging.service",
                source / "ops/systemd/muriel-staging.service",
            )
            shutil.copy2(ROOT / "ops/deploy-staging-release", source / "ops/deploy-staging-release")
            # These children support --root (and ReleaseDeploymentTests covers it
            # directly). update-staging-remote has fixed /srv and /etc invocations,
            # however, with no --root option or forwarding to its children. This fixture
            # maps the child default root into isolated storage; unmodified remote-host
            # coverage remains unavailable at this parent boundary.
            for script in ("bootstrap-staging-ubuntu", "deploy-staging-release"):
                path = source / "ops" / script
                path.write_text(
                    path.read_text().replace("root=/", "root=${STAGING_TEST_HOST_ROOT:?}", 1)
                )
            (source / "requirements.txt").write_text("Django==5.2.17\n")
            (source / "manage.py").write_text("placeholder\n")
            artifact = temporary_path / "source.tar"
            with tarfile.open(artifact, "w") as archive:
                for path in source.rglob("*"):
                    archive.add(path, path.relative_to(source), recursive=False)

            def write_executable(name, content):
                path = fake_bin / name
                path.write_text(content)
                path.chmod(path.stat().st_mode | stat.S_IXUSR)

            write_executable(
                "git",
                """#!/bin/sh
set -eu
while [ "${1:-}" = -C ]; do shift 2; done
case "$1" in
    status) ;;
    rev-parse) printf '%s\\n' "$STAGING_TEST_COMMIT" ;;
    archive) cat "$STAGING_TEST_ARTIFACT" ;;
    *) exit 1 ;;
esac
""",
            )
            write_executable(
                "scp",
                """#!/bin/sh
set -eu
[ "$#" -eq 2 ] && [ "${2%%:*}" = "$MURIEL_SSH_TARGET" ] || exit 98
printf 'local-artifact=%s\\n' "$1" >> "$STAGING_TEST_LOG"
cp "$1" "$STAGING_TEST_UPLOAD"
cp "$1" "$STAGING_TEST_REMOTE_ARCHIVE"
""",
            )
            write_executable(
                "ssh",
                """#!/bin/sh
set -eu
if [ "$1" = -tt ]; then shift; fi
[ "$#" -eq 2 ] && [ "$1" = "$MURIEL_SSH_TARGET" ] || exit 98
command=$2
case "$command" in
    *"mktemp --suffix=.tar"*) printf '%s\\n' "$STAGING_TEST_REMOTE_ARCHIVE" ;;
    *"mktemp --suffix=.sh"*) : > "$STAGING_TEST_REMOTE_HELPER"; printf '%s\\n' "$STAGING_TEST_REMOTE_HELPER" ;;
    *"tar -xOf"*) sh -c "$command" ;;
    "sudo sh "*)
        eval "set -- $command"
        helper=$3 commit=$4 artifact=$5 reset=$6
        # The parent has no --root passthrough, so map its fixed paths while its
        # children use the isolated default root configured above. This does not
        # exercise an unmodified full remote host.
        program=$(sed \
            -e "s|/srv/muriel-staging|$STAGING_TEST_HOST_ROOT/srv/muriel-staging|g" \
            -e "s|/etc/muriel-staging|$STAGING_TEST_HOST_ROOT/etc/muriel-staging|g" \
            "$helper")
        # Deliberately discard local identity, like ordinary sudo. Only the
        # literal arguments may select bootstrap and downstream consumers.
        unset PRECEPTOR_STAGING_ALLOWED_HOST PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN
        sh -c "$program" "$helper" "$commit" "$artifact" "$reset" "$7" "$8"
        ;;
    *"rm -f --"*) rm -f -- "$STAGING_TEST_REMOTE_ARCHIVE" "$STAGING_TEST_REMOTE_HELPER" ;;
    *) exit 1 ;;
esac
""",
            )
            write_executable(
                "dispatcher",
                """#!/bin/sh
set -eu
command=$(basename "$0")
case "$command" in
    getent)
        case "$1:$2" in
            group:muriel_staging) [ -f "$STAGING_TEST_STATE/group" ] && printf 'muriel_staging:x:998:\\n' ;;
            passwd:muriel_staging) [ -f "$STAGING_TEST_STATE/user" ] && printf 'muriel_staging:x:998:998::/srv/muriel-staging:/usr/sbin/nologin\\n' ;;
        esac
        ;;
    groupadd) touch "$STAGING_TEST_STATE/group" ;;
    useradd) touch "$STAGING_TEST_STATE/user" ;;
    chown) printf 'chown %s\\n' "$*" >> "$STAGING_TEST_LOG" ;;
    stat)
        for argument; do path=$argument; done
        case "$path" in
            */etc/muriel-staging/environment) value=root:muriel_staging:640 ;;
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
    runuser)
        for argument; do query=$argument; done
        case "$query" in
            *"FROM pg_authid"*)
                printf 'postgres-check-role\\n' >> "$STAGING_TEST_LOG"
                [ -f "$STAGING_TEST_STATE/role" ] && printf 'true:true:false:false\\n'
                ;;
            *"FROM pg_database"*)
                printf 'postgres-check-database\\n' >> "$STAGING_TEST_LOG"
                [ -f "$STAGING_TEST_STATE/database" ] && printf 'muriel_staging\\n'
                ;;
            "CREATE ROLE muriel_staging LOGIN") touch "$STAGING_TEST_STATE/role" ;;
            "CREATE DATABASE muriel_staging OWNER muriel_staging") touch "$STAGING_TEST_STATE/database" ;;
            *) shift 3; exec "$@" ;;
        esac
        true
        ;;
    systemctl) printf 'systemctl %s\\n' "$*" >> "$STAGING_TEST_LOG" ;;
    python3.14)
        if [ "$1" = -m ] && [ "$2" = venv ]; then
            mkdir -p "$3/bin"
            ln -s "$0" "$3/bin/python"
        else
            exec "$STAGING_TEST_PYTHON" "$@"
        fi
        ;;
    python) printf 'management %s\\n' "$*" >> "$STAGING_TEST_LOG" ;;
    mktemp)
        artifact=$("$STAGING_TEST_SYSTEM_MKTEMP" "$@")
        printf 'temporary-artifact=%s\\n' "$artifact" >> "$STAGING_TEST_LOG"
        printf '%s\\n' "$artifact"
        ;;
    tailscale)
        if [ "$#" -eq 3 ] && [ "$1" = serve ] && [ "$2" = status ] && [ "$3" = --json ]; then
            phase=before
            [ ! -f "$STAGING_TEST_STATE/serve-applied" ] || phase=after
            printf 'tailscale-status %s\\n' "$phase" >> "$STAGING_TEST_LOG"
            [ "${STAGING_TEST_STATUS_FAILURE:-}" != "$phase" ] || exit 1
            cat "$STAGING_TEST_STATE/tailscale-status.json"
        elif [ "$#" -eq 4 ] && [ "$1" = serve ]; then
            received=$*
            shift
            background=0 https_port= endpoint=
            for argument; do
                case "$argument" in
                    --bg) [ "$background" -eq 0 ] || exit 1; background=1 ;;
                    --https=10000) [ -z "$https_port" ] || exit 1; https_port=10000 ;;
                    http://127.0.0.1:8082) [ -z "$endpoint" ] || exit 1; endpoint=$argument ;;
                    *) exit 1 ;;
                esac
            done
            [ "$background" -eq 1 ] && [ "$https_port" = 10000 ] && [ "$endpoint" = http://127.0.0.1:8082 ] || exit 1
            cp "$STAGING_TEST_STATE/tailscale-after.json" "$STAGING_TEST_STATE/tailscale-status.json"
            touch "$STAGING_TEST_STATE/serve-applied"
            printf 'tailscale %s\\n' "$received" >> "$STAGING_TEST_LOG"
        else
            exit 1
        fi
        ;;
    curl)
        printf 'curl %s\\n' "$*" >> "$STAGING_TEST_LOG"
        exit "${STAGING_TEST_HEALTH_FAILURE:-0}"
        ;;
esac
""",
            )
            for command in (
                "chown",
                "curl",
                "getent",
                "groupadd",
                "mktemp",
                "python3.14",
                "runuser",
                "stat",
                "systemctl",
                "tailscale",
                "useradd",
            ):
                (fake_bin / command).symlink_to("dispatcher")

            def run_update(*flags, **environment):
                log.unlink(missing_ok=True)
                (state / "serve-applied").unlink(missing_ok=True)
                return subprocess.run(
                    [str(checkout / "ops/update-staging"), "transport-node", *flags],
                    cwd=Path.home(),
                    env=os.environ
                    | {
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "MURIEL_SSH_TARGET": "transport-node",
                        "STAGING_TEST_ARTIFACT": str(artifact),
                        "STAGING_TEST_COMMIT": commit,
                        "STAGING_TEST_HOST_ROOT": str(host_root),
                        "STAGING_TEST_LOG": str(log),
                        "STAGING_TEST_PYTHON": sys.executable,
                        "STAGING_TEST_REMOTE_ARCHIVE": remote_archive,
                        "STAGING_TEST_REMOTE_HELPER": remote_helper,
                        "STAGING_TEST_STATE": str(state),
                        "STAGING_TEST_UPLOAD": str(upload),
                        "STAGING_TEST_SYSTEM_MKTEMP": shutil.which("mktemp"),
                        "PRECEPTOR_STAGING_ALLOWED_HOST": host,
                        "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": origin,
                        **environment,
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )

            def run_remote(*identity_arguments):
                log.unlink(missing_ok=True)
                shutil.copy2(artifact, remote_archive)
                shutil.copy2(UPDATE_STAGING_REMOTE, remote_helper)
                # Same isolated path mapping as the SSH adapter; the helper still
                # compares its exact on-disk bytes to the archive before extraction.
                program = (
                    UPDATE_STAGING_REMOTE.read_text()
                    .replace("/srv/muriel-staging", f"{host_root}/srv/muriel-staging")
                    .replace("/etc/muriel-staging", f"{host_root}/etc/muriel-staging")
                )
                return subprocess.run(
                    [
                        "sh",
                        "-c",
                        program,
                        remote_helper,
                        commit,
                        remote_archive,
                        "0",
                        *identity_arguments,
                    ],
                    env=os.environ
                    | {
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "STAGING_TEST_HOST_ROOT": str(host_root),
                        "STAGING_TEST_LOG": str(log),
                        "STAGING_TEST_STATE": str(state),
                        "STAGING_TEST_SYSTEM_MKTEMP": shutil.which("mktemp"),
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )

            try:
                yield SimpleNamespace(
                    run=run_update,
                    run_remote=run_remote,
                    host=host,
                    origin=origin,
                    host_root=host_root,
                    log=log,
                    state=state,
                    production=production_tailscale_state,
                    persisted=persisted_tailscale_state,
                    remote_archive=remote_archive,
                    remote_helper=remote_helper,
                    commit=commit,
                )
            finally:
                Path(remote_archive).unlink(missing_ok=True)
                Path(remote_helper).unlink(missing_ok=True)

    def assert_update_artifacts_removed(self, fixture):
        self.assertFalse(Path(fixture.remote_archive).exists())
        self.assertFalse(Path(fixture.remote_helper).exists())
        commands = fixture.log.read_text().splitlines()
        artifacts = [
            line.split("=", 1)[1]
            for line in commands
            if line.startswith(("local-artifact=", "temporary-artifact="))
        ]
        self.assertTrue(artifacts)
        for path in artifacts:
            self.assertFalse(Path(path).exists(), path)

    def test_direct_remote_identity_and_argument_count_refuse_before_host_work(self):
        with self.remote_update_fixture() as fixture:
            original = sorted(fixture.host_root.rglob("*"))
            for arguments in (
                (),
                (STAGING_HOST,),
                (STAGING_HOST, STAGING_ORIGIN, "extra"),
                *((host or "", origin or "") for host, origin in INVALID_STAGING_PAIRS),
            ):
                with self.subTest(arguments=arguments):
                    result = fixture.run_remote(*arguments)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(sorted(fixture.host_root.rglob("*")), original)
                    if len(arguments) == 2:
                        self.assertIn("Staging identity requires", result.stderr)
                        self.assert_update_artifacts_removed(fixture)
                    else:
                        self.assertIn("expected commit", result.stderr)
                        self.assertFalse(fixture.log.exists())

    def test_ordinary_update_uses_selected_identity_through_bootstrap_route_health_and_output(self):
        for host in (STAGING_HOST, "trial-2.residency.test"):
            with self.subTest(host=host), self.remote_update_fixture(host) as fixture:
                result = fixture.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Staging: https://{host}:10000/", result.stdout.splitlines())
                self.assertIn(f"Staging update deployed commit {fixture.commit}.", result.stdout)
                commands = fixture.log.read_text().splitlines()
                curl = shlex.split(next(line for line in commands if line.startswith("curl ")))
                self.assertEqual(curl[-1], f"https://{host}:10000/health/")
                self.assertNotIn("--location", curl)
                self.assertNotIn("-L", curl)
                self.assertFalse(any("reset_staging_synthetic" in line for line in commands))
                self.assertEqual(
                    json.loads((fixture.state / "tailscale-status.json").read_text()),
                    fixture.persisted,
                )
                environment_file = fixture.host_root / "etc/muriel-staging/environment"
                original = environment_file.read_bytes()
                values = dict(line.split("=", 1) for line in original.decode().splitlines())
                self.assertEqual(values["PRECEPTOR_STAGING_ALLOWED_HOST"], host)
                self.assertEqual(values["PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN"], fixture.origin)
                self.assert_update_artifacts_removed(fixture)
                result = fixture.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(environment_file.read_bytes(), original)
                self.assert_update_artifacts_removed(fixture)

    def test_health_failure_reports_selected_destination_and_suppresses_success(self):
        with self.remote_update_fixture("trial-2.residency.test") as fixture:
            result = fixture.run(STAGING_TEST_HEALTH_FAILURE="22")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"curl -v {fixture.origin}/health/", result.stderr)
            self.assertNotIn("Staging update deployed", result.stdout)
            self.assertNotIn("Staging: ", result.stdout)
            self.assert_update_artifacts_removed(fixture)

    def test_wrong_host_route_fails_without_adopting_observed_identity(self):
        with self.remote_update_fixture("trial-2.residency.test") as fixture:
            after = copy.deepcopy(fixture.persisted)
            after["Web"][f"{STAGING_HOST}:10000"] = after["Web"].pop(f"{fixture.host}:10000")
            (fixture.state / "tailscale-after.json").write_text(json.dumps(after))
            result = fixture.run()
            self.assert_route_refusal(
                fixture, result, mutated=True, diagnostic="staging Serve route"
            )
            self.assertNotIn("Staging: ", result.stdout)

    def test_reset_and_load_deploys_a_new_commit_then_reuses_the_current_release(self):
        with self.remote_update_fixture() as fixture:
            host_root = fixture.host_root
            log = fixture.log
            state = fixture.state
            production_tailscale_state = fixture.production
            persisted_tailscale_state = fixture.persisted
            result = fixture.run("--reset-and-load")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_update_artifacts_removed(fixture)
            self.assertIn(f"Staging update deployed commit {fixture.commit}", result.stdout)
            environment = dict(
                line.split("=", 1)
                for line in (host_root / "etc/muriel-staging/environment").read_text().splitlines()
            )
            secret = environment.pop("PRECEPTOR_STAGING_SECRET_KEY")
            self.assertGreaterEqual(len(secret), 50)
            self.assertNotIn(secret, result.stdout + result.stderr + log.read_text())
            required_environment = {
                "PRECEPTOR_STAGING_ALLOWED_HOST": fixture.host,
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": fixture.origin,
                "PRECEPTOR_STAGING_POSTGRES_NAME": "muriel_staging",
                "PRECEPTOR_STAGING_POSTGRES_USER": "muriel_staging",
            }
            for setting, value in required_environment.items():
                self.assertEqual(environment.get(setting), value)
            self.assertEqual(
                (host_root / "etc/muriel-staging/environment").stat().st_mode & 0o777,
                0o640,
            )
            commands = log.read_text().splitlines()
            self.assertEqual(
                commands.count("management manage.py reset_staging_synthetic --confirm"), 1
            )
            self.assertIn("management manage.py check_staging", commands)
            self.assertIn("systemctl restart muriel-staging", commands)

            def assert_staging_route_effects(command_lines):
                health_check = shlex.split(
                    next(command for command in command_lines if command.startswith("curl "))
                )
                self.assertEqual(health_check[0], "curl")
                self.assertIn("--fail", health_check)
                self.assertIn("--silent", health_check)
                self.assertEqual(health_check.count("--max-time"), 1)
                timeout_index = health_check.index("--max-time") + 1
                self.assertLess(timeout_index, len(health_check))
                timeout = float(health_check[timeout_index])
                self.assertTrue(math.isfinite(timeout))
                self.assertGreater(timeout, 0)
                staging_health_url = f"{fixture.origin}/health/"
                self.assertEqual(
                    [argument for argument in health_check[1:] if "://" in argument],
                    [staging_health_url],
                )

                tailscale = shlex.split(
                    next(
                        command
                        for command in command_lines
                        if command.startswith("tailscale serve ")
                    )
                )
                self.assertEqual(tailscale[:2], ["tailscale", "serve"])
                self.assertCountEqual(
                    tailscale[2:],
                    ["--bg", "--https=10000", "http://127.0.0.1:8082"],
                )

            assert_staging_route_effects(commands)
            self.assertEqual(
                json.loads((state / "tailscale-status.json").read_text()), persisted_tailscale_state
            )
            persisted_state = json.loads((state / "tailscale-status.json").read_text())
            self.assertEqual(
                {key: value for key, value in persisted_state["TCP"].items() if key != "10000"},
                production_tailscale_state["TCP"],
            )
            self.assertEqual(persisted_state["TCP"]["10000"], {"HTTPS": True})
            self.assertIs(persisted_state["TCP"]["10000"]["HTTPS"], True)
            self.assertEqual(
                persisted_state["Web"]["production.example.test:8443"],
                production_tailscale_state["Web"]["production.example.test:8443"],
            )
            self.assertEqual(
                persisted_state["AllowFunnel"], production_tailscale_state["AllowFunnel"]
            )

            result = fixture.run("--reset-and-load")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"staging release {fixture.commit} is already current", result.stdout)
            self.assertIn(f"Staging update deployed commit {fixture.commit}", result.stdout)
            self.assert_update_artifacts_removed(fixture)
            next_commands = log.read_text().splitlines()
            self.assertNotIn("systemctl restart muriel-staging", next_commands)
            self.assertIn("systemctl is-active --quiet muriel-staging", next_commands)
            self.assertEqual(
                next_commands.count("management manage.py reset_staging_synthetic --confirm"), 1
            )
            self.assertIn("management manage.py check_staging", next_commands)
            assert_staging_route_effects(next_commands)

    def assert_route_refusal(self, fixture, result, *, mutated, diagnostic):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Staging update deployed", result.stdout)
        self.assertNotIn("tailnet-only HTTPS route are verified", result.stdout)
        commands = fixture.log.read_text().splitlines()
        self.assertFalse(any(line.startswith("curl ") for line in commands))
        self.assertEqual(
            [line for line in commands if line.startswith("tailscale ")],
            ["tailscale serve --bg --https=10000 http://127.0.0.1:8082"] if mutated else [],
        )
        self.assertEqual(
            [line for line in commands if line.startswith("tailscale-status ")],
            ["tailscale-status before", "tailscale-status after"]
            if mutated
            else ["tailscale-status before"],
        )
        self.assert_update_artifacts_removed(fixture)

    def test_requires_complete_exact_staging_listener_and_handler(self):
        route = f"{STAGING_HOST}:10000"
        # Ellipsis means removal; each case starts from a fresh complete post-state.
        cases = (
            (("TCP", "10000"), ...),
            (("TCP", "10000"), []),
            (("TCP", "10000", "HTTPS"), False),
            (("TCP", "10000", "HTTPS"), 1),
            (("TCP", "10000", "HTTPS"), "true"),
            (("TCP", "10000", "TCPForward"), "127.0.0.1:8082"),
            (("Web", route), ...),
            (("Web", route), []),
            (("Web", route, "Extra"), True),
            (("Web", route, "Handlers"), []),
            (("Web", route, "Handlers", "/"), ...),
            (("Web", route, "Handlers", "/extra"), {"Proxy": "http://127.0.0.1:8082"}),
            (("Web", route, "Handlers", "/", "Proxy"), "http://127.0.0.1:9999"),
            (("Web", route, "Handlers", "/", "Extra"), False),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value), self.remote_update_fixture() as fixture:
                after = copy.deepcopy(fixture.persisted)
                target = after
                for key in path[:-1]:
                    target = target[key]
                if value is ...:
                    del target[path[-1]]
                else:
                    target[path[-1]] = value
                (fixture.state / "tailscale-after.json").write_text(json.dumps(after))
                result = fixture.run()
                self.assert_route_refusal(fixture, result, mutated=True, diagnostic="staging")

    def test_preserves_all_non_staging_keys_values_and_types(self):
        host = "production.example.test"
        cases = (
            (("AllowFunnel", f"{host}:443"), 1),
            (("AllowFunnel", f"{host}:443"), False),
            (("AllowFunnel", f"{host}:443"), ...),
            (("AllowFunnel", "foreign.test:10000"), True),
            (("TCP", "443", "ProxyProtocol"), 2.0),
            (("TCP", "8443", "HTTPS"), 1),
            (("TCP", "443", "TCPForward"), "127.0.0.1:9999"),
            (("TCP", "8443"), ...),
            (("TCP", "10001"), {"HTTPS": True}),
            (("Web", f"{host}:8443", "Handlers", "/", "Proxy"), "https://127.0.0.1:8081"),
            (("Web", f"{host}:8443", "Handlers", "/extra"), {"Proxy": "http://elsewhere"}),
            (("Web", "foreign.test:10000"), {"Handlers": {}}),
            (("Other", "nested"), [1, {"value": 2}]),
            (("Other", "nested"), [True]),
            (("Other",), ...),
            (("New",), {}),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value), self.remote_update_fixture() as fixture:
                before = copy.deepcopy(fixture.production)
                after = copy.deepcopy(fixture.persisted)
                before["Other"] = {"nested": [True, {"value": 2}]}
                after["Other"] = copy.deepcopy(before["Other"])
                target = after
                for key in path[:-1]:
                    target = target[key]
                if value is ...:
                    del target[path[-1]]
                else:
                    target[path[-1]] = value
                (fixture.state / "tailscale-status.json").write_text(json.dumps(before))
                (fixture.state / "tailscale-after.json").write_text(json.dumps(after))
                result = fixture.run()
                self.assert_route_refusal(
                    fixture,
                    result,
                    mutated=True,
                    diagnostic="changed a production Tailscale mapping",
                )

    def test_staging_permission_must_be_absent_or_boolean_false(self):
        route = f"{STAGING_HOST}:10000"
        for phase in ("before", "after"):
            for permission in (True, 1, 0, "false", "true", None, [], {}):
                with (
                    self.subTest(phase=phase, permission=permission),
                    self.remote_update_fixture() as fixture,
                ):
                    state = copy.deepcopy(
                        fixture.production if phase == "before" else fixture.persisted
                    )
                    state["AllowFunnel"][route] = permission
                    filename = (
                        "tailscale-status.json" if phase == "before" else "tailscale-after.json"
                    )
                    (fixture.state / filename).write_text(json.dumps(state))
                    result = fixture.run()
                    self.assert_route_refusal(
                        fixture, result, mutated=phase == "after", diagnostic="Funnel exposure"
                    )

    def test_convergence_preserves_observed_state_not_a_global_production_schema(self):
        route = f"{STAGING_HOST}:10000"
        # Non-staging state may differ from the commissioned production baseline.
        # Only exact preservation, not global commissioning, belongs to this guard.
        with self.remote_update_fixture() as fixture:
            before = copy.deepcopy(fixture.production)
            after = copy.deepcopy(fixture.persisted)
            for state in (before, after):
                state["TCP"]["443"]["TCPForward"] = "127.0.0.1:9090"
                state["TCP"]["10001"] = {"HTTPS": True}
                state["Web"]["foreign.test:10000"] = {
                    "Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}
                }
                state["AllowFunnel"]["foreign.test:10000"] = False
                state["Other"] = {"nested": [True, {"value": 2}]}
            before["AllowFunnel"][route] = False
            (fixture.state / "tailscale-status.json").write_text(json.dumps(before))
            (fixture.state / "tailscale-after.json").write_text(json.dumps(after))
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_update_artifacts_removed(fixture)
            # Explicit false may also be introduced on an otherwise private route.
            after["AllowFunnel"][route] = False
            (fixture.state / "tailscale-after.json").write_text(json.dumps(after))
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_update_artifacts_removed(fixture)

    def test_malformed_snapshots_and_acquisition_errors_fail_closed(self):
        malformed = [
            "",
            "{invalid",
            "null",
            "[]",
            '{"Other": NaN}',
            '{"TCP": {}, "TCP": {}}',
            '{"TCP": {"10000": null}}',
            '{"Web": {"foreign.test:8443": []}}',
            '{"Web": {"foreign.test:8443": {"Handlers": {"/": false}}}}',
        ]
        for section in ("TCP", "Web", "AllowFunnel"):
            for value in (None, [], True):
                malformed.append(json.dumps({section: value}))
        for phase in ("before", "after"):
            for snapshot in malformed:
                with (
                    self.subTest(phase=phase, snapshot=snapshot),
                    self.remote_update_fixture() as fixture,
                ):
                    filename = (
                        "tailscale-status.json" if phase == "before" else "tailscale-after.json"
                    )
                    (fixture.state / filename).write_text(snapshot)
                    result = fixture.run()
                    self.assert_route_refusal(
                        fixture,
                        result,
                        mutated=phase == "after",
                        diagnostic="staging route verification",
                    )
            with (
                self.subTest(phase=phase, acquisition=True),
                self.remote_update_fixture() as fixture,
            ):
                result = fixture.run(STAGING_TEST_STATUS_FAILURE=phase)
                self.assert_route_refusal(
                    fixture,
                    result,
                    mutated=phase == "after",
                    diagnostic="could not inspect Serve state"
                    if phase == "before"
                    else "could not verify the persisted Serve state",
                )
