import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "ops" / "backup-database"
RESTORE = ROOT / "ops" / "restore-database"
KNOWN_DUMP_BYTES = b"custom dump"
KNOWN_DUMP_DIGEST = hashlib.sha256(KNOWN_DUMP_BYTES).hexdigest()


class DatabaseBackupTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.backups = self.temporary_path / "backups"
        self.backups.mkdir()
        self.offsite_backups = self.temporary_path / "offsite" / "srv" / "backups" / "muriel"
        self.release = self.temporary_path / ("a" * 40)
        self.release.mkdir()
        self.fake_bin = self.temporary_path / "bin"
        self.fake_bin.mkdir()
        self.log = self.temporary_path / "commands.log"
        self.write_dispatcher()

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def recorded_calls(self):
        """Return structured records from the isolated fake executables."""
        return [json.loads(record) for record in self.log.read_bytes().split(b"\0") if record]

    def calls_for(self, command):
        return [call for call in self.recorded_calls() if call["command"] == command]

    def option_value(self, argv, option):
        """Return a long option's value in either supported command-line form."""
        for index, argument in enumerate(argv):
            if argument == option and index + 1 < len(argv):
                return argv[index + 1]
            if argument.startswith(f"{option}="):
                return argument.removeprefix(f"{option}=")
        return None

    def assert_has_batch_mode(self, call):
        argv = call["argv"]
        self.assertTrue(
            any(
                argument == "-oBatchMode=yes"
                or (
                    argument == "-o"
                    and index + 1 < len(argv)
                    and argv[index + 1] == "BatchMode=yes"
                )
                for index, argument in enumerate(argv)
            ),
            argv,
        )

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            f"""#!{sys.executable}
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path


command = Path(sys.argv[0]).name
argv = sys.argv[1:]
record = {{
    "command": command,
    "argv": argv,
    "environment": {{"PGCLUSTER": os.environ.get("PGCLUSTER", "")}},
}}
with open(os.environ["BACKUP_LOG"], "ab") as output:
    output.write(json.dumps(record).encode() + b"\\0")


def fail_if_requested():
    if os.environ.get("FAIL_COMMAND") == command:
        raise SystemExit(1)


def long_option_value(option):
    for index, argument in enumerate(argv):
        if argument == option and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(f"{{option}}="):
            return argument.removeprefix(f"{{option}}=")
    return None


def remote_target(remote_path):
    root = Path(os.environ["FAKE_OFFSITE_ROOT"]).resolve()
    if not remote_path.startswith("/"):
        raise SystemExit(2)
    target = (root / remote_path.lstrip("/")).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise SystemExit(2) from None
    return target


def remote_invocation():
    if not argv:
        raise SystemExit(2)
    host_index = len(argv) - 2
    if host_index < 0 or argv[host_index] != os.environ["MURIEL_BACKUP_OFFSITE_HOST"]:
        raise SystemExit(2)
    return argv[-1]


def remote_segments(remote_command):
    tokens = shlex.split(remote_command)
    segments = [[]]
    for token in tokens:
        if token == "&&":
            if not segments[-1]:
                raise SystemExit(2)
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        raise SystemExit(2)
    return segments


def move(segment):
    if segment[:1] != ["mv"]:
        raise SystemExit(2)
    operands = [part for part in segment[1:] if part != "--"]
    if len(operands) != 2:
        raise SystemExit(2)
    source, destination = map(remote_target, operands)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def retain(segment):
    if segment[:1] != ["find"]:
        raise SystemExit(2)
    try:
        directory = remote_target(segment[1])
        pattern = segment[segment.index("-name") + 1]
        days = int(segment[segment.index("-mtime") + 1])
    except (IndexError, ValueError):
        raise SystemExit(2) from None
    if "-delete" not in segment:
        raise SystemExit(2)
    cutoff = time.time() - (days + 1) * 24 * 60 * 60
    for path in directory.glob(pattern):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()


if command == "pg_dump":
    destination = long_option_value("--file")
    if destination is None:
        raise SystemExit(2)
    Path(destination).write_bytes(os.environ.get("FAKE_DUMP_BYTES", "custom dump").encode())
    fail_if_requested()
elif command == "pg_restore":
    fail_if_requested()
elif command == "sha256sum":
    if os.environ.get("FORGED_SHA256"):
        print(os.environ["FORGED_SHA256"])
    else:
        import hashlib
        print(f"{{hashlib.sha256(Path(argv[-1]).read_bytes()).hexdigest()}}  {{argv[-1]}}")
elif command == "scp":
    if len(argv) < 2 or ":" not in argv[-1]:
        raise SystemExit(2)
    source = Path(argv[-2])
    host, remote_path = argv[-1].split(":", 1)
    if host != os.environ["MURIEL_BACKUP_OFFSITE_HOST"]:
        raise SystemExit(2)
    destination = remote_target(remote_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    fail_if_requested()
elif command == "ssh":
    remote_command = remote_invocation()
    for segment in remote_segments(remote_command):
        if segment[:1] == ["mkdir"]:
            remote_target(segment[-1]).mkdir(parents=True, exist_ok=True)
        elif segment[:1] == ["mv"]:
            move(segment)
        elif segment[:1] == ["find"]:
            retain(segment)
        else:
            raise SystemExit(2)
    fail_if_requested()
else:
    fail_if_requested()
""",
        )
        for command in ("pg_dump", "pg_restore", "scp", "sha256sum", "ssh"):
            (self.fake_bin / command).symlink_to(dispatcher.name)

    def run_backup(self, command=BACKUP, **environment):
        return subprocess.run(
            [str(command)],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "BACKUP_LOG": str(self.log),
                "PRECEPTOR_POSTGRES_NAME": "muriel",
                "MURIEL_BACKUP_DIRECTORY": str(self.backups),
                "MURIEL_BACKUP_OFFSITE_HOST": "backup@example.net",
                "MURIEL_BACKUP_OFFSITE_DIRECTORY": "/srv/backups/muriel",
                "FAKE_OFFSITE_ROOT": str(self.temporary_path / "offsite"),
                "MURIEL_RELEASE_DIRECTORY": str(self.release),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_systemd_backup_service_and_timer_use_the_locked_down_daily_contract(self):
        service = configparser.ConfigParser(interpolation=None)
        service.read_string((ROOT / "ops/systemd/muriel-backup.service").read_text())
        timer = configparser.ConfigParser(interpolation=None)
        timer.read_string((ROOT / "ops/systemd/muriel-backup.timer").read_text())

        self.assertEqual(service["Service"]["User"], "muriel")
        self.assertEqual(service["Service"]["EnvironmentFile"], "/etc/muriel/environment")
        self.assertEqual(service["Service"]["ExecStart"], "/srv/muriel/backup-database")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* 02:17:00 UTC")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        delay = timer["Timer"]["RandomizedDelaySec"]
        match = re.fullmatch(r"(\d+)([sm])", delay)
        self.assertIsNotNone(match)
        assert match is not None
        seconds = int(match.group(1)) * {"s": 1, "m": 60}[match.group(2)]
        self.assertGreaterEqual(seconds, 0)
        self.assertLessEqual(seconds, 15 * 60)
        self.assertEqual(timer["Timer"]["Unit"], "muriel-backup.service")

    def test_creates_private_verified_backup_metadata_and_atomic_offsite_copy(self):
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stderr)
        dumps = list(self.backups.glob("daily-*.dump"))
        self.assertEqual(len(dumps), 1)
        metadata = Path(f"{dumps[0]}.metadata")
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        metadata_values = dict(line.split("=", 1) for line in metadata.read_text().splitlines())
        self.assertEqual(dumps[0].read_bytes(), KNOWN_DUMP_BYTES)
        self.assertEqual(metadata_values.get("format"), "1")
        self.assertEqual(metadata_values.get("dump"), dumps[0].name)
        self.assertEqual(metadata_values.get("sha256"), KNOWN_DUMP_DIGEST)
        self.assertEqual(metadata_values.get("database"), "muriel")
        self.assertEqual(metadata_values.get("application_revision"), "a" * 40)
        created_at = datetime.strptime(metadata_values["created_at"], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
        self.assertEqual(
            created_at.strftime("%Y%m%dT%H%M%SZ"), dumps[0].stem.removeprefix("daily-")
        )
        remote_dump = self.offsite_backups / dumps[0].name
        remote_metadata = Path(f"{remote_dump}.metadata")
        self.assertEqual(remote_dump.read_bytes(), KNOWN_DUMP_BYTES)
        self.assertEqual(remote_metadata.read_bytes(), metadata.read_bytes())
        self.assertEqual(stat.S_IMODE(remote_dump.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(remote_metadata.stat().st_mode), 0o600)
        self.assertNotIn(".partial-", " ".join(path.name for path in self.backups.iterdir()))
        self.assertEqual(list(self.offsite_backups.glob("*.partial")), [])

        dump_calls = self.calls_for("pg_dump")
        restore_calls = self.calls_for("pg_restore")
        scp_calls = self.calls_for("scp")
        ssh_calls = self.calls_for("ssh")
        partial_dump = next(
            (
                value
                for call in dump_calls
                if (value := self.option_value(call["argv"], "--file")) is not None
                and ".partial-" in value
            ),
            None,
        )
        self.assertIsNotNone(partial_dump)
        assert partial_dump is not None
        dump_call = next(
            call for call in dump_calls if self.option_value(call["argv"], "--file") == partial_dump
        )
        restore_call = next(call for call in restore_calls if partial_dump in call["argv"])
        self.assertEqual(self.option_value(dump_call["argv"], "--host"), "/var/run/postgresql")
        self.assertEqual(self.option_value(dump_call["argv"], "--port"), "5432")
        self.assertEqual(self.option_value(dump_call["argv"], "--format"), "custom")
        self.assertEqual(self.option_value(restore_call["argv"], "--host"), "/var/run/postgresql")
        self.assertEqual(self.option_value(restore_call["argv"], "--port"), "5432")
        self.assertIn("--list", restore_call["argv"])
        self.assertEqual(dump_call["environment"]["PGCLUSTER"], "18/main")
        self.assertEqual(restore_call["environment"]["PGCLUSTER"], "18/main")
        records = self.recorded_calls()
        transfer_calls = [*scp_calls, *ssh_calls]
        self.assertTrue(transfer_calls)
        self.assertLess(records.index(dump_call), records.index(restore_call))
        self.assertLess(
            records.index(restore_call), min(records.index(call) for call in transfer_calls)
        )
        for call in [*scp_calls, *ssh_calls]:
            self.assert_has_batch_mode(call)

    def test_failed_dump_preserves_last_known_good_backup_and_removes_partial_output(self):
        last_good = self.backups / "daily-last-good.dump"
        last_good.write_text("known good")

        result = self.run_backup(FAIL_COMMAND="pg_dump")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(last_good.read_text(), "known good")
        self.assertEqual(list(self.backups.glob("*.partial-*")), [])
        self.assertFalse((self.backups / "daily-last-good.dump.metadata").exists())

    def test_backup_uses_the_18_main_socket_and_port_when_postgresql_16_is_retained(self):
        result = self.run_backup(RETAINED_POSTGRESQL_16="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        for call in [*self.calls_for("pg_dump"), *self.calls_for("pg_restore")]:
            self.assertIn("--host=/var/run/postgresql", call["argv"])
            self.assertIn("--port=5432", call["argv"])
            self.assertEqual(call["environment"]["PGCLUSTER"], "18/main")

    def test_offhost_failure_is_degraded_and_does_not_prune_retained_backup(self):
        old_dump = self.backups / "daily-old.dump"
        old_metadata = Path(f"{old_dump}.metadata")
        old_dump.write_text("old")
        old_metadata.write_text("old")
        old_time = time.time() - 15 * 24 * 60 * 60
        os.utime(old_dump, (old_time, old_time))
        os.utime(old_metadata, (old_time, old_time))
        last_good = self.offsite_backups / "daily-last-good.dump"
        last_good.parent.mkdir(parents=True)
        last_good.write_bytes(b"offsite known good dump")
        last_good_metadata = Path(f"{last_good}.metadata")
        last_good_metadata.write_bytes(b"offsite known good metadata")

        result = self.run_backup(FAIL_COMMAND="scp")

        self.assertEqual(result.returncode, 1)
        self.assertIn("DEGRADED", result.stderr)
        self.assertTrue(old_dump.exists())
        self.assertTrue(old_metadata.exists())
        self.assertEqual(len(list(self.backups.glob("daily-*.dump"))), 2)
        self.assertEqual(last_good.read_bytes(), b"offsite known good dump")
        self.assertEqual(last_good_metadata.read_bytes(), b"offsite known good metadata")
        self.assertFalse(
            any(
                path.name.startswith("daily-")
                and path.name not in {last_good.name, last_good_metadata.name}
                for path in self.offsite_backups.iterdir()
                if not path.name.endswith(".partial")
            )
        )

    def test_success_prunes_daily_files_older_than_fourteen_days(self):
        old_dump = self.backups / "daily-old.dump"
        old_metadata = Path(f"{old_dump}.metadata")
        old_dump.write_text("old")
        old_metadata.write_text("old")
        old_time = time.time() - 15 * 24 * 60 * 60
        os.utime(old_dump, (old_time, old_time))
        os.utime(old_metadata, (old_time, old_time))
        remote_old_dump = self.offsite_backups / "daily-old.dump"
        remote_old_dump.parent.mkdir(parents=True)
        remote_old_dump.write_bytes(b"old offsite dump")
        remote_old_metadata = Path(f"{remote_old_dump}.metadata")
        remote_old_metadata.write_bytes(b"old offsite metadata")
        os.utime(remote_old_dump, (old_time, old_time))
        os.utime(remote_old_metadata, (old_time, old_time))

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old_dump.exists())
        self.assertFalse(old_metadata.exists())
        self.assertFalse(remote_old_dump.exists())
        self.assertFalse(remote_old_metadata.exists())

    def test_manual_backup_escapes_an_inaccessible_inherited_working_directory(self):
        inaccessible_directory = self.temporary_path / "inaccessible"
        inaccessible_directory.mkdir()
        wrapper = self.temporary_path / "run-from-inaccessible-directory"
        self.write_executable(
            wrapper,
            """#!/bin/sh
set -eu
cd "$INACCESSIBLE_DIRECTORY"
chmod 000 "$INACCESSIBLE_DIRECTORY"
exec "$BACKUP_SCRIPT"
""",
        )
        self.addCleanup(inaccessible_directory.chmod, 0o700)

        result = self.run_backup(
            command=wrapper,
            INACCESSIBLE_DIRECTORY=str(inaccessible_directory),
            BACKUP_SCRIPT=str(BACKUP),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        dump = self.backups.glob("daily-*.dump").__next__()
        # TEXT-CONTRACT: backup-completion-report; consumers: ops/deploy-release.create_backup,
        # ops/reset-production-domain, and ops/clear-production-login-access. Scope: exact
        # prefix, filename, capitalization, punctuation, and separating space; command
        # substitution strips trailing newlines. Protecting test:
        # schedules/tests/test_database_backup.py::DatabaseBackupTests.test_manual_backup_escapes_an_inaccessible_inherited_working_directory
        self.assertEqual(
            result.stdout.rstrip("\n"), f"Backup protected locally and off-host: {dump.name}"
        )


class DatabaseRestoreTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.fake_bin = self.temporary_path / "bin"
        self.fake_bin.mkdir()
        self.log = self.temporary_path / "commands.log"
        self.database_states = self.temporary_path / "database-states"
        self.database_states.mkdir()
        self.scratch_database = "muriel_restore_drill"
        self.production_database_state = self.database_states / "muriel"
        self.production_database_state.write_text("live database sentinel")
        self.unrelated_scratch_database = "muriel_restore_unrelated"
        self.unrelated_scratch_state = self.database_states / self.unrelated_scratch_database
        self.unrelated_scratch_state.write_text("unrelated scratch sentinel")
        self.release = self.temporary_path / "release"
        check = self.release / "scripts" / "check-production"
        check.parent.mkdir(parents=True)
        self.write_executable(
            check,
            f"""#!{sys.executable}
import json
import os
from pathlib import Path


record = {{
    "command": "check-production",
    "argv": [],
    "environment": {{
        "MURIEL_TEST_IDENTITY": os.environ.get("MURIEL_TEST_IDENTITY", ""),
        "PGCLUSTER": os.environ.get("PGCLUSTER", ""),
        "PRECEPTOR_POSTGRES_NAME": os.environ.get("PRECEPTOR_POSTGRES_NAME", ""),
    }},
}}
with open(os.environ["BACKUP_LOG"], "ab") as output:
    output.write(json.dumps(record).encode() + b"\\0")

target = os.environ.get("PRECEPTOR_POSTGRES_NAME")
scratch = os.environ["RESTORE_SCRATCH_DATABASE"]
state = Path(os.environ["RESTORE_DATABASE_STATES"]) / scratch
if target != scratch or not state.is_file() or state.read_text() != "restored":
    raise SystemExit(1)
""",
        )
        self.write_dispatcher()
        self.backup = self.temporary_path / "daily-test.dump"
        self.backup.write_text("custom dump")
        self.metadata = Path(f"{self.backup}.metadata")
        self.metadata.write_text(
            "format=1\n"
            "dump=daily-test.dump\n"
            f"sha256={hashlib.sha256(self.backup.read_bytes()).hexdigest()}\n"
            "database=muriel\n"
            "application_revision=" + "a" * 40 + "\n"
        )
        self.environment = self.temporary_path / "environment"
        self.environment.write_text(
            "PRECEPTOR_PRODUCTION_SECRET_KEY=secret\n"
            "PRECEPTOR_PRODUCTION_ALLOWED_HOST=muriel.example\n"
            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://muriel.example\n"
            "PRECEPTOR_POSTGRES_NAME=muriel\n"
            "PRECEPTOR_POSTGRES_USER=muriel\n"
        )

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def recorded_calls(self):
        if not self.log.exists():
            return []
        return [json.loads(record) for record in self.log.read_bytes().split(b"\0") if record]

    def calls_for(self, command):
        return [call for call in self.recorded_calls() if call["command"] == command]

    def option_value(self, argv, option):
        for index, argument in enumerate(argv):
            if argument == option and index + 1 < len(argv):
                return argv[index + 1]
            if argument.startswith(f"{option}="):
                return argument.removeprefix(f"{option}=")
        return None

    def command_target(self, call):
        value_options = {"--cluster", "--host", "--owner", "--port", "--dbname"}
        operands = []
        skip_next = False
        for argument in call["argv"]:
            if skip_next:
                skip_next = False
            elif argument in value_options:
                skip_next = True
            elif not argument.startswith("-"):
                operands.append(argument)
        self.assertEqual(operands, [self.scratch_database], call["argv"])
        return operands[0]

    def query_text(self, call):
        return self.option_value(call["argv"], "-tAc")

    def assert_endpoint_call(self, call, identity):
        self.assertEqual(call["environment"]["MURIEL_TEST_IDENTITY"], identity)
        self.assertEqual(call["environment"]["PGCLUSTER"], "18/main")
        self.assertEqual(self.option_value(call["argv"], "--host"), "/var/run/postgresql")
        self.assertEqual(self.option_value(call["argv"], "--port"), "5432")

    def assert_no_postgres_lifecycle(self):
        self.assertFalse(self.calls_for("createdb"))
        self.assertFalse(self.calls_for("dropdb"))

    def assert_muriel_queries_use_the_verified_endpoint(self):
        queries = self.calls_for("psql")
        self.assertTrue(queries)
        for query in queries:
            self.assert_endpoint_call(query, "muriel")

    def assert_scratch_cleanup(self, call):
        self.assert_endpoint_call(call, "postgres")
        self.command_target(call)
        self.assertIn("--if-exists", call["argv"])

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys


command = Path(sys.argv[0]).name
argv = sys.argv[1:]
record = {{
    "command": command,
    "argv": argv,
    "environment": {{
        "MURIEL_TEST_IDENTITY": os.environ.get("MURIEL_TEST_IDENTITY", ""),
        "PGCLUSTER": os.environ.get("PGCLUSTER", ""),
        "PRECEPTOR_POSTGRES_NAME": os.environ.get("PRECEPTOR_POSTGRES_NAME", ""),
    }},
}}
with open(os.environ["BACKUP_LOG"], "ab") as output:
    output.write(json.dumps(record).encode() + b"\\0")


def long_option_value(option):
    for index, argument in enumerate(argv):
        if argument == option and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(f"{{option}}="):
            return argument.removeprefix(f"{{option}}=")
    return None


def positional_target():
    value_options = {{"--cluster", "--host", "--owner", "--port", "--dbname"}}
    operands = []
    skip_next = False
    for argument in argv:
        if skip_next:
            skip_next = False
        elif argument in value_options:
            skip_next = True
        elif not argument.startswith("-"):
            operands.append(argument)
    if len(operands) != 1:
        raise SystemExit(2)
    return operands[0]


def owned_state(target):
    if target != os.environ["RESTORE_SCRATCH_DATABASE"]:
        raise SystemExit(2)
    root = Path(os.environ["RESTORE_DATABASE_STATES"]).resolve()
    state = (root / target).resolve()
    try:
        state.relative_to(root)
    except ValueError:
        raise SystemExit(2) from None
    return state


if command == "id":
    print(os.environ.get("RESTORE_CALLER_UID", "1000"))
elif command == "pg_lsclusters":
    print(
        "18 {{cluster}} {{port}} {{status}} postgres /var/lib/postgresql/18/main log".format(
            cluster=os.environ.get("RESTORE_CLUSTER", "main"),
            port=os.environ.get("RESTORE_CLUSTER_PORT", "5432"),
            status=os.environ.get("RESTORE_CLUSTER_STATUS", "online"),
        )
    )
elif command == "ss":
    if os.environ.get("RESTORE_SOCKET_LISTENER", "present") == "present":
        print(
            "u_str LISTEN 0 244 /var/run/postgresql/.s.PGSQL.5432 0 * 0 "
            "users:((\\\"postgres\\\",pid={{pid}},fd=6))".format(
                pid=os.environ.get("RESTORE_SOCKET_PID", "1234")
            )
        )
elif command == "ps":
    print(
        os.environ.get(
            "RESTORE_PROCESS_IDENTITY",
            "postgres postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main",
        )
    )
elif command == "runuser":
    if len(argv) < 4 or argv[0] != "-u" or argv[2] != "--":
        raise SystemExit(2)
    environment = os.environ | {{
        "MURIEL_TEST_IDENTITY": argv[1],
        "RESTORE_PARENT_PID": str(os.getppid()),
    }}
    raise SystemExit(subprocess.run(argv[3:], env=environment, check=False).returncode)
elif command in {{"createdb", "dropdb"}}:
    state = owned_state(positional_target())
    if command == "createdb":
        state.write_text("created")
    elif state.exists():
        state.unlink()
    elif "--if-exists" not in argv:
        raise SystemExit(1)
elif command == "pg_restore":
    target = long_option_value("--dbname")
    if target is not None:
        state = owned_state(target)
        if not state.is_file():
            raise SystemExit(2)
        state.write_text("restored")
    if os.environ.get("RESTORE_FAIL_RESTORE") == "1" and "--clean" in argv:
        raise SystemExit(1)
    if os.environ.get("RESTORE_SIGNAL") and "--clean" in argv:
        os.kill(int(os.environ["RESTORE_PARENT_PID"]), getattr(__import__("signal"), "SIG" + os.environ["RESTORE_SIGNAL"]))
        raise SystemExit(1)
elif command == "psql":
    query = long_option_value("-tAc")
    if (
        query
        and os.environ.get("RESTORE_CONFIG_FILE_DENIED") == "1"
        and "config_file" in query.lower()
    ):
        raise SystemExit(1)
    if query and "SELECT 1 FROM pg_database WHERE datname = '" in query:
        target = query.split("datname = '", 1)[1].split("'", 1)[0]
        if owned_state(target).exists():
            print("1")
    elif query and "SHOW server_version_num" in query:
        print(os.environ.get("RESTORE_ENDPOINT_VERSION", "180000"))
""",
        )
        for command in (
            "createdb",
            "dropdb",
            "id",
            "pg_lsclusters",
            "pg_restore",
            "ps",
            "psql",
            "runuser",
            "ss",
        ):
            (self.fake_bin / command).symlink_to(dispatcher.name)

    def run_restore(self, *arguments, **environment):
        return subprocess.run(
            [str(RESTORE), *arguments],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "BACKUP_LOG": str(self.log),
                "RESTORE_DATABASE_STATES": str(self.database_states),
                "RESTORE_SCRATCH_DATABASE": self.scratch_database,
                "PRECEPTOR_POSTGRES_NAME": "muriel",
                "MURIEL_RELEASE_DIRECTORY": str(self.release),
                "MURIEL_ENVIRONMENT_FILE": str(self.environment),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_scratch_database_is_retired(self):
        self.assertFalse((self.database_states / self.scratch_database).exists())
        self.assertEqual(self.production_database_state.read_text(), "live database sentinel")
        self.assertEqual(self.unrelated_scratch_state.read_text(), "unrelated scratch sentinel")

    def test_refuses_checksum_mismatch_before_opening_dump(self):
        self.metadata.write_text(self.metadata.read_text().replace(KNOWN_DUMP_DIGEST, "0" * 64, 1))

        result = self.run_restore(
            "--backup", str(self.backup), "--target", "elsewhere", "--confirm"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum", result.stderr)
        self.assertFalse(self.calls_for("pg_restore"))
        self.assert_scratch_database_is_retired()

    def test_refuses_production_without_deliberate_override(self):
        result = self.run_restore("--backup", str(self.backup), "--target", "muriel", "--confirm")

        self.assertEqual(result.returncode, 1)
        self.assertIn("--allow-production", result.stderr)
        self.assertFalse(self.calls_for("pg_restore"))
        self.assert_scratch_database_is_retired()

    def test_scratch_drill_orders_database_creation_restore_checks_and_cleanup(self):
        result = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel_restore_drill",
            "--scratch",
            "--confirm",
            RESTORE_CALLER_UID="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        cleanup_calls = self.calls_for("dropdb")
        creation = self.calls_for("createdb")[0]
        inspection = next(
            call
            for call in self.calls_for("pg_restore")
            if "--list" in call["argv"] and call["argv"][-1] == str(self.backup)
        )
        restore = next(call for call in self.calls_for("pg_restore") if "--clean" in call["argv"])
        readiness = self.calls_for("check-production")[0]

        for cleanup in cleanup_calls:
            self.assert_scratch_cleanup(cleanup)
        self.assert_endpoint_call(creation, "postgres")
        self.command_target(creation)
        self.assertEqual(self.option_value(creation["argv"], "--owner"), "muriel")
        self.assert_endpoint_call(inspection, "muriel")
        self.assert_endpoint_call(restore, "muriel")
        self.assertEqual(self.option_value(restore["argv"], "--dbname"), self.scratch_database)
        self.assertEqual(restore["argv"][-1], str(self.backup))
        self.assertEqual(readiness["environment"]["MURIEL_TEST_IDENTITY"], "muriel")
        self.assertEqual(readiness["environment"]["PRECEPTOR_POSTGRES_NAME"], self.scratch_database)
        cleanup_indexes = [index for index, call in enumerate(calls) if call["command"] == "dropdb"]
        self.assertLess(calls.index(inspection), cleanup_indexes[0])
        self.assertLess(cleanup_indexes[0], calls.index(creation))
        self.assertLess(calls.index(creation), calls.index(restore))
        self.assertLess(calls.index(restore), calls.index(readiness))
        self.assertGreater(cleanup_indexes[-1], calls.index(readiness))
        self.assertTrue(self.calls_for("pg_restore"))
        self.assertTrue(self.calls_for("psql"))
        self.assert_muriel_queries_use_the_verified_endpoint()
        self.assert_scratch_database_is_retired()

    def test_restore_pins_every_client_to_18_main_when_postgresql_16_is_retained(self):
        result = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel_restore_drill",
            "--scratch",
            "--confirm",
            RETAINED_POSTGRESQL_16="1",
            RESTORE_CALLER_UID="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for command, identity in (
            ("pg_restore", "muriel"),
            ("dropdb", "postgres"),
            ("createdb", "postgres"),
        ):
            for call in self.calls_for(command):
                self.assert_endpoint_call(call, identity)

    def test_scratch_drill_works_when_config_file_is_restricted_to_muriel(self):
        result = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel_restore_drill",
            "--scratch",
            "--confirm",
            RESTORE_CALLER_UID="0",
            RESTORE_CONFIG_FILE_DENIED="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        psql_calls = self.calls_for("psql")
        queries = [self.query_text(call) for call in psql_calls]
        self.assertIn("SHOW server_version_num", queries)
        self.assertFalse(
            any(query and "config_file" in query.lower() for query in queries), queries
        )
        self.assert_muriel_queries_use_the_verified_endpoint()

    def test_scratch_drill_refuses_divergent_endpoint_facts_before_database_lifecycle(self):
        failures = (
            ("RESTORE_CLUSTER", "other", "18/main"),
            ("RESTORE_CLUSTER_PORT", "5433", "port 5432"),
            ("RESTORE_SOCKET_LISTENER", "missing", "/var/run/postgresql/.s.PGSQL.5432"),
            (
                "RESTORE_PROCESS_IDENTITY",
                "postgres postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/other",
                "PostgreSQL 18/main",
            ),
            ("RESTORE_ENDPOINT_VERSION", "170000", "PostgreSQL major version 18"),
        )
        for key, value, expected_error in failures:
            with self.subTest(endpoint_fact=key):
                result = self.run_restore(
                    "--backup",
                    str(self.backup),
                    "--target",
                    "muriel_restore_drill",
                    "--scratch",
                    "--confirm",
                    RESTORE_CALLER_UID="0",
                    **{key: value},
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_error, result.stderr)
                self.assert_no_postgres_lifecycle()
                self.assert_scratch_database_is_retired()

    def test_scratch_drill_refuses_non_root_and_unsafe_targets_before_database_commands(self):
        non_root = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel_restore_drill",
            "--scratch",
            "--confirm",
        )
        unsafe_target = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "other",
            "--scratch",
            "--confirm",
            RESTORE_CALLER_UID="0",
        )

        self.assertEqual(non_root.returncode, 1)
        self.assertIn("through sudo", non_root.stderr)
        self.assertEqual(unsafe_target.returncode, 1)
        self.assertIn("muriel_restore_", unsafe_target.stderr)
        self.assert_no_postgres_lifecycle()
        self.assert_scratch_database_is_retired()

    def test_scratch_drill_refuses_effective_environment_overrides_before_postgres_lifecycle(self):
        original_environment = self.environment.read_text()
        overrides = (
            ("PRECEPTOR_PRODUCTION_SECRET_KEY=", "PRECEPTOR_PRODUCTION_SECRET_KEY"),
            ("PRECEPTOR_PRODUCTION_ALLOWED_HOST=", "PRECEPTOR_PRODUCTION_ALLOWED_HOST"),
            (
                "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=",
                "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN",
            ),
            ("PRECEPTOR_POSTGRES_NAME=other", "PRECEPTOR_POSTGRES_NAME=muriel"),
            ("PRECEPTOR_POSTGRES_USER=other", "PRECEPTOR_POSTGRES_USER=muriel"),
            ("PRECEPTOR_POSTGRES_HOST=/tmp", "PostgreSQL 18/main local socket"),
            ("PRECEPTOR_POSTGRES_PORT=5433", "PostgreSQL 18/main port 5432"),
        )

        for override, expected_error in overrides:
            with self.subTest(override=override):
                self.environment.write_text(f"{original_environment}{override}\n")

                result = self.run_restore(
                    "--backup",
                    str(self.backup),
                    "--target",
                    "muriel_restore_drill",
                    "--scratch",
                    "--confirm",
                    RESTORE_CALLER_UID="0",
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_error, result.stderr)
                self.assertTrue(
                    any(
                        call["command"] == "runuser" and call["argv"][:3] == ["-u", "muriel", "--"]
                        for call in self.recorded_calls()
                    )
                )
                self.assert_no_postgres_lifecycle()
                self.assert_scratch_database_is_retired()

    def test_scratch_failure_cleans_up_as_postgres_before_returning_failure(self):
        result = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel_restore_drill",
            "--scratch",
            "--confirm",
            RESTORE_CALLER_UID="0",
            RESTORE_FAIL_RESTORE="1",
        )

        self.assertEqual(result.returncode, 1)
        calls = self.recorded_calls()
        restore = next(call for call in self.calls_for("pg_restore") if "--clean" in call["argv"])
        cleanup = self.calls_for("dropdb")[-1]
        cleanup_index = max(
            index for index, call in enumerate(calls) if call["command"] == "dropdb"
        )
        self.assert_scratch_cleanup(cleanup)
        self.assertGreater(cleanup_index, calls.index(restore))
        self.assert_muriel_queries_use_the_verified_endpoint()
        self.assert_scratch_database_is_retired()

    def test_scratch_signal_paths_clean_up_as_postgres(self):
        for signal in ("HUP", "INT", "TERM"):
            with self.subTest(signal=signal):
                self.log.unlink(missing_ok=True)
                result = self.run_restore(
                    "--backup",
                    str(self.backup),
                    "--target",
                    "muriel_restore_drill",
                    "--scratch",
                    "--confirm",
                    RESTORE_CALLER_UID="0",
                    RESTORE_SIGNAL=signal,
                )

                self.assertEqual(result.returncode, 1)
                calls = self.recorded_calls()
                restore = next(
                    call for call in self.calls_for("pg_restore") if "--clean" in call["argv"]
                )
                cleanup = self.calls_for("dropdb")[-1]
                cleanup_index = max(
                    index for index, call in enumerate(calls) if call["command"] == "dropdb"
                )
                self.assert_scratch_cleanup(cleanup)
                self.assertGreater(cleanup_index, calls.index(restore))
                self.assert_muriel_queries_use_the_verified_endpoint()
                self.assert_scratch_database_is_retired()

    def test_root_cannot_use_the_scratch_shortcut_for_production_restore(self):
        result = self.run_restore(
            "--backup",
            str(self.backup),
            "--target",
            "muriel",
            "--confirm",
            "--allow-production",
            RESTORE_CALLER_UID="0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unprivileged muriel", result.stderr)
        self.assertFalse(self.calls_for("pg_restore"))
        self.assert_scratch_database_is_retired()
