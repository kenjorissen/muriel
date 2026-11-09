import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from preceptor_project.settings import development_allowed_hosts

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "start-dev"
ROOT = LAUNCHER.parent.parent
PREVIEW_LAUNCHER = ROOT / "scripts" / "start-preview"

_HOST_HTTP_PROBE = """
import json

import django

django.setup()

from django.conf import settings
from django.test import Client

hosts = ("localhost", "127.0.0.1", "preview.example", "untrusted.example", "0.0.0.0")
client = Client()
print(
    json.dumps(
        {
            "allowed_hosts": settings.ALLOWED_HOSTS,
            "statuses": {host: client.get("/", HTTP_HOST=host).status_code for host in hosts},
        },
        sort_keys=True,
    )
)
"""


class DevelopmentAllowedHostsTests(TestCase):
    def test_defaults_to_explicit_local_hosts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertCountEqual(development_allowed_hosts(), ["localhost", "127.0.0.1"])

    def test_parses_a_comma_separated_override_without_blank_entries(self):
        self.assertEqual(
            development_allowed_hosts(" preview.example, localhost, "),
            ["preview.example", "localhost"],
        )

    def probe_hosts(self, **overrides):
        environment = os.environ.copy()
        environment.pop("PRECEPTOR_ALLOWED_HOSTS", None)
        environment.pop("PRECEPTOR_BIND_ADDRESS", None)
        environment["DJANGO_SETTINGS_MODULE"] = "preceptor_project.settings"
        environment.update(overrides)

        result = subprocess.run(
            [sys.executable, "-c", _HOST_HTTP_PROBE],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_requests_trust_only_local_hosts_regardless_of_bind(self):
        for bind in (None, "0.0.0.0", "preview.example"):
            with self.subTest(bind=bind):
                probe = self.probe_hosts(**({"PRECEPTOR_BIND_ADDRESS": bind} if bind else {}))
                self.assertCountEqual(probe["allowed_hosts"], ["localhost", "127.0.0.1"])
                self.assertEqual(
                    probe["statuses"],
                    {
                        "localhost": 302,
                        "127.0.0.1": 302,
                        "preview.example": 400,
                        "untrusted.example": 400,
                        "0.0.0.0": 400,
                    },
                )

    def test_explicit_hosts_replace_defaults_without_trusting_the_bind_address(self):
        probe = self.probe_hosts(
            PRECEPTOR_ALLOWED_HOSTS=" preview.example, localhost, , ",
            PRECEPTOR_BIND_ADDRESS="0.0.0.0",
        )
        self.assertEqual(probe["allowed_hosts"], ["preview.example", "localhost"])
        self.assertEqual(
            probe["statuses"],
            {
                "localhost": 302,
                "127.0.0.1": 400,
                "preview.example": 302,
                "untrusted.example": 400,
                "0.0.0.0": 400,
            },
        )

    def test_rejects_empty_and_wildcard_overrides(self):
        for value in ("", " , ", "*", "localhost, *, preview.example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                development_allowed_hosts(value)


class StartDevelopmentLauncherTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "main"
        self.root.mkdir()
        self.launcher = self.root / "scripts" / "start-dev"
        self.launcher.parent.mkdir()
        shutil.copy2(LAUNCHER, self.launcher)
        self.launcher.chmod(self.launcher.stat().st_mode | stat.S_IXUSR)
        self.launch_state = self.root / "launch-state"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.write_executable(
            self.fake_bin / "git",
            """#!/bin/sh
printf 'worktree %s\\nbranch refs/heads/main\\n' "$FAKE_MAIN_WORKTREE"
""",
        )

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def create_virtual_environment(self):
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        self.write_executable(
            python,
            """#!/bin/sh
set -eu
mkdir -p "$LAUNCH_STATE"
printf '%s\\0' "$PWD" "$@" '' >> "$LAUNCH_STATE/commands"
[ "$#" -ge 2 ] || exit 1
[ "$1" = manage.py ] || exit 1
shift
command=$1
shift
case "$command" in check|migrate|runserver) ;; *) exit 1 ;; esac
option_requires_value=
address=
for argument; do
    if [ -n "$option_requires_value" ]; then
        case "$argument" in [0-3]) option_requires_value= ;; *) exit 1 ;; esac
        continue
    fi
    case "$argument" in
        manage.py|check|migrate|runserver) exit 1 ;;
        *:8000)
            [ "$command" = runserver ] && [ -z "$address" ] || exit 1
            address=$argument
            ;;
        --noinput) [ "$command" = migrate ] || exit 1 ;;
        --verbosity=[0-3]) ;;
        --verbosity) option_requires_value=1 ;;
        *) exit 1 ;;
    esac
done
[ -z "$option_requires_value" ] || exit 1
case "$command" in
check)
    exit 0
    ;;
migrate)
    printf '%s\\n' "$PWD" > "$LAUNCH_STATE/migrate-working-directory"
    printf '%s\\n' "${PRECEPTOR_ALLOWED_HOSTS-}" > "$LAUNCH_STATE/migrate-allowed-hosts"
    : > "$LAUNCH_STATE/migrated"
    exit 0
    ;;
runserver)
    [ -n "$address" ] || exit 1
    [ -f "$LAUNCH_STATE/migrated" ] || exit 1
    printf '%s\\n' "$PWD" > "$LAUNCH_STATE/server-working-directory"
    printf '%s\\n' "${PRECEPTOR_ALLOWED_HOSTS-}" > "$LAUNCH_STATE/server-allowed-hosts"
    printf '%s\\n' "$address" > "$LAUNCH_STATE/server-address"
    : > "$LAUNCH_STATE/server-started"
    exit 0
    ;;
*) exit 1 ;;
esac
""",
        )

    def launched_commands(self):
        records = (self.launch_state / "commands").read_bytes().split(b"\0\0")
        return [
            tuple(part.decode() for part in record.split(b"\0") if part)
            for record in records
            if record
        ]

    def run_launcher(self, *, main_worktree=None, **environment):
        launcher_environment = os.environ.copy()
        launcher_environment.pop("PRECEPTOR_ALLOWED_HOSTS", None)
        launcher_environment.pop("PRECEPTOR_BIND_ADDRESS", None)
        launcher_environment |= {
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "FAKE_MAIN_WORKTREE": str(main_worktree or self.root),
            "LAUNCH_STATE": str(self.launch_state),
            **environment,
        }
        return subprocess.run(
            [str(self.launcher)],
            cwd=Path.home(),
            env=launcher_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_has_valid_shell_syntax(self):
        result = subprocess.run(["sh", "-n", str(LAUNCHER)], text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_a_missing_virtual_environment(self):
        result = self.run_launcher()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing virtual environment", result.stderr)

    def test_refuses_a_script_outside_the_canonical_main_worktree(self):
        result = self.run_launcher(main_worktree=self.root / "different-worktree")

        self.assertEqual(result.returncode, 1)
        self.assertIn("canonical main worktree", result.stderr)

    def test_migrates_then_starts_the_default_server_from_its_repository(self):
        self.create_virtual_environment()

        result = self.run_launcher()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.launch_state / "migrate-working-directory").read_text(), f"{self.root}\n"
        )
        self.assertEqual((self.launch_state / "migrate-allowed-hosts").read_text(), "\n")
        self.assertTrue((self.launch_state / "migrated").is_file())
        self.assertEqual(
            (self.launch_state / "server-working-directory").read_text(), f"{self.root}\n"
        )
        self.assertEqual((self.launch_state / "server-allowed-hosts").read_text(), "\n")
        self.assertEqual((self.launch_state / "server-address").read_text(), "127.0.0.1:8000\n")
        self.assertTrue((self.launch_state / "server-started").is_file())
        commands = self.launched_commands()
        self.assertTrue(commands)
        self.assertTrue(all(command[0] == str(self.root) for command in commands))
        migrate_index = next(
            index
            for index, command in enumerate(commands)
            if command[1:3] == ("manage.py", "migrate")
        )
        server_index = next(
            index
            for index, command in enumerate(commands)
            if command[1:3] == ("manage.py", "runserver")
        )
        self.assertLess(migrate_index, server_index)
        self.assertIn("127.0.0.1:8000", commands[server_index][3:])
        self.assertFalse(
            {"flush", "reset_demo", "seed_demo", "createsuperuser"}
            & {argument for command in commands for argument in command[1:]}
        )

    def test_uses_overrides_after_migrating_from_another_current_directory(self):
        self.create_virtual_environment()

        result = self.run_launcher(
            PRECEPTOR_BIND_ADDRESS="0.0.0.0",
            PRECEPTOR_ALLOWED_HOSTS="localhost,127.0.0.1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.launch_state / "migrate-allowed-hosts").read_text(), "localhost,127.0.0.1\n"
        )
        self.assertEqual(
            (self.launch_state / "server-allowed-hosts").read_text(), "localhost,127.0.0.1\n"
        )
        self.assertEqual((self.launch_state / "server-address").read_text(), "0.0.0.0:8000\n")
        commands = self.launched_commands()
        self.assertTrue(commands)
        self.assertTrue(all(command[0] == str(self.root) for command in commands))
        migrate_index = next(
            index
            for index, command in enumerate(commands)
            if command[1:3] == ("manage.py", "migrate")
        )
        server_index = next(
            index
            for index, command in enumerate(commands)
            if command[1:3] == ("manage.py", "runserver")
        )
        self.assertLess(migrate_index, server_index)
        self.assertIn("0.0.0.0:8000", commands[server_index][3:])


class StartPreviewLauncherTests(TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name) / "main"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.preview = self.root.parent / "preview worktree"
        self.preview.mkdir()
        (self.preview / "manage.py").touch()
        settings_directory = self.preview / "preceptor_project"
        settings_directory.mkdir()
        shutil.copy2(ROOT / "preceptor_project" / "settings.py", settings_directory)
        self.launcher = self.root / "scripts" / "start-preview"
        self.launcher.parent.mkdir()
        shutil.copy2(PREVIEW_LAUNCHER, self.launcher)
        self.launch_state = self.root / "launch-state.jsonl"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.write_executable(
            self.fake_bin / "git",
            """#!/bin/sh
[ "$1" = -C ] || exit 1
case "$2" in
    "$FAKE_MAIN_WORKTREE") printf '%s/.git\\n' "$FAKE_MAIN_WORKTREE" ;;
    "$FAKE_PREVIEW_WORKTREE")
        printf '%s\\n' "${FAKE_PREVIEW_COMMON_DIR:-$FAKE_MAIN_WORKTREE/.git}" ;;
    *) exit 1 ;;
esac
""",
        )
        self.python = self.root / ".venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.write_executable(
            self.python,
            f"#!{sys.executable}\n"
            + """import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys

sys.path.insert(0, os.getcwd())
runtime = Path(os.environ["PYTHONPATH"])
settings = runpy.run_path(runtime / (os.environ["DJANGO_SETTINGS_MODULE"] + ".py"))
database = Path(settings["DATABASES"]["default"]["NAME"])
command = sys.argv[2]
with sqlite3.connect(database) as connection:
    before = connection.execute("SELECT value FROM fixture").fetchall()
    if command == "migrate":
        connection.execute("INSERT INTO fixture VALUES ('preview migration')")
    elif command != "runserver":
        sys.exit(1)
with open(os.environ["LAUNCH_STATE"], "a") as state:
    state.write(json.dumps({
        "executable": sys.argv[0],
        "arguments": sys.argv[1:],
        "cwd": os.getcwd(),
        "runtime": str(runtime),
        "database": str(database),
        "before": before,
        "allowed_hosts_override": os.environ.get("PRECEPTOR_ALLOWED_HOSTS"),
        "allowed_hosts": settings["ALLOWED_HOSTS"],
    }) + "\\n")
if command == os.environ.get("FAIL_COMMAND"):
    sys.exit(23)
""",
        )
        self.main_database = self.root / "db.sqlite3"
        with sqlite3.connect(self.main_database) as connection:
            connection.execute("CREATE TABLE fixture (value TEXT)")
            connection.execute("INSERT INTO fixture VALUES ('main data')")
        self.original_database = self.main_database.read_bytes()
        self.preview_database = self.preview / "db.sqlite3"
        self.preview_database.write_bytes(b"existing worktree data")

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_launcher(self, **environment):
        launcher_environment = os.environ.copy()
        launcher_environment.pop("PRECEPTOR_ALLOWED_HOSTS", None)
        launcher_environment.pop("PRECEPTOR_BIND_ADDRESS", None)
        launcher_environment |= {
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "FAKE_MAIN_WORKTREE": str(self.root),
            "FAKE_PREVIEW_WORKTREE": str(self.preview),
            "LAUNCH_STATE": str(self.launch_state),
            **environment,
        }
        return subprocess.run(
            [str(self.launcher), str(self.preview)],
            cwd=Path.home(),
            env=launcher_environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def launched_commands(self):
        return [json.loads(line) for line in self.launch_state.read_text().splitlines()]

    def assert_isolated_and_cleaned(self, commands):
        self.assertTrue(commands)
        database = Path(commands[0]["database"])
        runtime = Path(commands[0]["runtime"])
        self.assertEqual(database.parent, runtime)
        self.assertNotIn(database, (self.main_database, self.preview_database))
        for command in commands:
            self.assertEqual(command["executable"], str(self.python))
            self.assertEqual(command["cwd"], str(self.preview))
            self.assertEqual(command["database"], str(database))
            self.assertEqual(command["runtime"], str(runtime))
        self.assertEqual(commands[0]["before"], [["main data"]])
        if len(commands) == 2:
            self.assertEqual(commands[1]["before"], [["main data"], ["preview migration"]])
        self.assertEqual(self.main_database.read_bytes(), self.original_database)
        self.assertEqual(self.preview_database.read_bytes(), b"existing worktree data")
        self.assertFalse(runtime.exists(), f"Preview runtime was not removed: {runtime}")

    def test_has_valid_shell_syntax(self):
        result = subprocess.run(["sh", "-n", str(PREVIEW_LAUNCHER)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_and_explicit_bind_keep_host_trust_independent(self):
        for override, hosts in (
            (None, None),
            ("0.0.0.0", None),
            ("0.0.0.0", "preview.example,localhost"),
        ):
            with self.subTest(bind=override, hosts=hosts):
                self.launch_state.unlink(missing_ok=True)
                environment = {}
                if override is not None:
                    environment["PRECEPTOR_BIND_ADDRESS"] = override
                if hosts is not None:
                    environment["PRECEPTOR_ALLOWED_HOSTS"] = hosts
                result = self.run_launcher(**environment)

                self.assertEqual(result.returncode, 0, result.stderr)
                commands = self.launched_commands()
                self.assertEqual(len(commands), 2)
                self.assertEqual(commands[0]["arguments"], ["manage.py", "migrate", "--noinput"])
                address = f"{override or '127.0.0.1'}:8001"
                self.assertEqual(
                    commands[1]["arguments"],
                    ["manage.py", "runserver", address, "--noreload"],
                )
                self.assertIn(f"http://{address}/", result.stdout)
                for command in commands:
                    self.assertEqual(command["allowed_hosts_override"], hosts)
                    if hosts is not None:
                        self.assertEqual(command["allowed_hosts"], ["preview.example", "localhost"])
                    else:
                        self.assertIn("127.0.0.1", command["allowed_hosts"])
                        self.assertNotIn("0.0.0.0", command["allowed_hosts"])
                        self.assertNotIn("*", command["allowed_hosts"])
                self.assert_isolated_and_cleaned(commands)

    def test_migration_and_server_failure_propagate_and_clean_owned_storage(self):
        for failure, expected_commands in (("migrate", 1), ("runserver", 2)):
            with self.subTest(failure=failure):
                self.launch_state.unlink(missing_ok=True)
                result = self.run_launcher(FAIL_COMMAND=failure)

                self.assertEqual(result.returncode, 23, result.stderr)
                commands = self.launched_commands()
                self.assertEqual(len(commands), expected_commands)
                self.assert_isolated_and_cleaned(commands)

    def test_rejects_foreign_repository_or_non_application_before_launching(self):
        result = self.run_launcher(FAKE_PREVIEW_COMMON_DIR=str(self.root.parent / "other/.git"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.launch_state.exists())
        (self.preview / "manage.py").unlink()
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.launch_state.exists())
