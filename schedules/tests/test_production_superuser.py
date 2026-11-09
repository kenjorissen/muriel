import os
import stat
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ops" / "create-superuser"


class ProductionSuperuserHelperTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name)
        self.root, self.bin = self.path / "host", self.path / "bin"
        self.log = self.path / "commands.log"
        self.root.mkdir()
        self.bin.mkdir()
        self.release = self.root / "srv/muriel/releases/release"
        python = self.release / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("""#!/bin/sh
[ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel ] || exit 1
[ "$DJANGO_SETTINGS_MODULE" = preceptor_project.production_settings ] || exit 1
[ "$PRECEPTOR_POSTGRES_NAME" = muriel ] || exit 1
[ "$PRECEPTOR_POSTGRES_USER" = muriel ] || exit 1
printf 'django-execution\\t%s\\t%s\\t%s\\t%s\\t%s' "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" "$PRECEPTOR_POSTGRES_NAME" "$PRECEPTOR_POSTGRES_USER" "$(pwd)" >> "$SUPERUSER_LOG"
for argument in "$@"; do printf '\\t%s' "$argument" >> "$SUPERUSER_LOG"; done
printf '\\n' >> "$SUPERUSER_LOG"
""")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (self.release / "manage.py").touch()
        (self.root / "srv/muriel/current").symlink_to("releases/release")
        self.environment = self.root / "etc/muriel/environment"
        self.environment.parent.mkdir(parents=True)
        self.environment.write_text(
            "PRECEPTOR_PRODUCTION_SECRET_KEY=not-a-production-secret\n"
            "PRECEPTOR_PRODUCTION_ALLOWED_HOST=production.example.test\n"
            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN=https://production.example.test\n"
            "PRECEPTOR_POSTGRES_NAME=muriel\nPRECEPTOR_POSTGRES_USER=muriel\n"
        )
        self._write_dispatcher()

    def _write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_dispatcher(self):
        dispatcher = self.bin / "dispatcher"
        self._write_executable(
            dispatcher,
            """#!/bin/sh
set -eu
command=$(basename "$0")
printf '%s' "$command" >> "$SUPERUSER_LOG"
for argument in "$@"; do printf '\\t%s' "$(printf '%s' "$argument" | tr '\\n' ' ')" >> "$SUPERUSER_LOG"; done
printf '\\n' >> "$SUPERUSER_LOG"
case "$command" in
  id) [ "${NONROOT:-}" != 1 ] && printf '0\\n' || printf '1000\\n' ;;
  pg_lsclusters) printf '18 main 5432 online postgres /var/lib/postgresql/18/main log\\n' ;;
  runuser)
    requested_user=$2; shift 3
    [ "$1" = sh ] || exit 1
    script=$3; shift 3
    MURIEL_FAKE_EFFECTIVE_USER="$requested_user" /bin/sh -c "$script" "$@" ;;
  stat) printf '%s\\n' "${ENVIRONMENT_METADATA:-root:muriel:640}" ;;
esac
""",
        )
        for command in ("id", "pg_lsclusters", "runuser", "stat"):
            (self.bin / command).symlink_to(dispatcher.name)

    def _run(self, *arguments, **environment):
        return subprocess.run(
            [str(HELPER), *arguments, "--root", str(self.root)],
            env=os.environ
            | {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "SUPERUSER_LOG": str(self.log),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def _commands(self):
        return [tuple(line.split("\t")) for line in self.log.read_text().splitlines()]

    def test_refuses_non_root_before_reading_production_configuration(self):
        result = self._run(NONROOT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run through sudo", result.stderr)
        self.assertTrue(any(command[:2] == ("id", "-u") for command in self._commands()))
        self.assertFalse(any(command[0] == "django-execution" for command in self._commands()))

    def test_refuses_missing_or_unsafe_active_release_before_database_access(self):
        current = self.root / "srv/muriel/current"
        current.unlink()
        missing_result = self._run()
        outside_release = self.path / "outside-release"
        outside_release.mkdir()
        current.symlink_to(outside_release)
        unsafe_result = self._run()
        self.assertEqual(missing_result.returncode, 1)
        self.assertIn("current production release is missing or unsafe", missing_result.stderr)
        self.assertEqual(unsafe_result.returncode, 1)
        self.assertIn("outside the managed release directory", unsafe_result.stderr)
        self.assertFalse(any(command[0] == "pg_lsclusters" for command in self._commands()))

    def test_refuses_missing_or_unsafe_environment_before_database_access(self):
        self.environment.unlink()
        missing_result = self._run()
        outside_environment = self.path / "outside-environment"
        outside_environment.write_text("PRECEPTOR_POSTGRES_NAME=muriel\n")
        self.environment.symlink_to(outside_environment)
        unsafe_result = self._run()
        self.assertEqual(missing_result.returncode, 1)
        self.assertEqual(unsafe_result.returncode, 1)
        self.assertIn("production environment file is missing or unsafe", missing_result.stderr)
        self.assertIn("production environment file is missing or unsafe", unsafe_result.stderr)
        self.assertFalse(any(command[0] == "pg_lsclusters" for command in self._commands()))

    def test_refuses_environment_with_unsafe_ownership_or_mode_before_database_access(self):
        for metadata, description in (
            ("root:muriel:660", "group-writable"),
            ("root:muriel:642", "world-writable"),
            ("root:muriel:644", "world-readable"),
            ("other:muriel:640", "wrong owner"),
            ("root:other:640", "wrong group"),
        ):
            with self.subTest(description=description):
                result = self._run(ENVIRONMENT_METADATA=metadata)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "production environment file must be root:muriel mode 0640", result.stderr
                )
                self.assertFalse(
                    any(command[0] == "django-execution" for command in self._commands())
                )

    def test_runs_interactive_django_command_as_service_identity_with_production_target(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self._commands()
        execution = next(command for command in commands if command[0] == "django-execution")
        self.assertEqual(
            execution[:6],
            (
                "django-execution",
                "muriel",
                "preceptor_project.production_settings",
                "muriel",
                "muriel",
                str(self.release),
            ),
        )
        self.assertEqual(execution[6:], ("manage.py", "createsuperuser"))
        self.assertFalse(
            any("password" in value.lower() for command in commands for value in command)
        )

    def test_refuses_environment_values_overridden_after_valid_assignments(self):
        original_environment = self.environment.read_text()
        for override, expected_error in (
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
        ):
            with self.subTest(override=override):
                self.environment.write_text(f"{original_environment}{override}\n")
                result = self._run()
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(
                    any(command[0] == "django-execution" for command in self._commands())
                )

    def test_refuses_command_arguments_that_could_change_django_interaction(self):
        result = self._run("--noinput")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(any(command[0] == "django-execution" for command in self._commands()))
