import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ops" / "create-staging-superuser"


class StagingSuperuserHelperTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name)
        self.root, self.bin = self.path / "host", self.path / "bin"
        self.log = self.path / "commands.log"
        self.root.mkdir()
        self.bin.mkdir()
        self.release = self.root / "srv/muriel-staging/releases/release"
        python = self.release / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("""#!/bin/sh
[ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel_staging ] || exit 1
[ "$DJANGO_SETTINGS_MODULE" = preceptor_project.staging_settings ] || exit 1
[ "$PRECEPTOR_STAGING_POSTGRES_NAME" = muriel_staging ] || exit 1
[ "$PRECEPTOR_STAGING_POSTGRES_USER" = muriel_staging ] || exit 1
printf 'django-execution\\t%s\\t%s\\t%s\\t%s\\t%s' "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" "$PRECEPTOR_STAGING_POSTGRES_NAME" "$PRECEPTOR_STAGING_POSTGRES_USER" "$(pwd)" >> "$STAGING_SUPERUSER_LOG"
for argument in "$@"; do printf '\\t%s' "$argument" >> "$STAGING_SUPERUSER_LOG"; done
printf '\\n' >> "$STAGING_SUPERUSER_LOG"
""")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (self.release / "manage.py").touch()
        (self.root / "srv/muriel-staging/current").symlink_to("releases/release")
        self.environment = self.root / "etc/muriel-staging/environment"
        self.environment.parent.mkdir(parents=True)
        self.environment.write_text(
            "PRECEPTOR_STAGING_SECRET_KEY=staging-only-secret\n"
            "PRECEPTOR_STAGING_ALLOWED_HOST=staging.example.test\n"
            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN=https://staging.example.test:10000\n"
            "PRECEPTOR_STAGING_POSTGRES_NAME=muriel_staging\n"
            "PRECEPTOR_STAGING_POSTGRES_USER=muriel_staging\n"
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
printf '%s' "$command" >> "$STAGING_SUPERUSER_LOG"
for argument in "$@"; do printf '\\t%s' "$(printf '%s' "$argument" | tr '\\n' ' ')" >> "$STAGING_SUPERUSER_LOG"; done
printf '\\n' >> "$STAGING_SUPERUSER_LOG"
case "$command" in
  id) printf '%s\\n' "${STAGING_TEST_UID:-0}" ;;
  runuser)
    requested_user=$2; shift 3
    [ "$1" = sh ] || exit 1
    script=$3; shift 3
    MURIEL_FAKE_EFFECTIVE_USER="$requested_user" /bin/sh -c "$script" "$@" ;;
  ssh)
    printf '%s' "${STAGING_SSH_STDOUT:-}"
    printf '%s' "${STAGING_SSH_STDERR:-}" >&2
    exit "${STAGING_SSH_STATUS:-0}" ;;
esac
""",
        )
        for command in ("id", "runuser", "ssh"):
            (self.bin / command).symlink_to(dispatcher.name)

    def _run(self, *arguments, helper=HELPER, **environment):
        return self._invoke("--root", str(self.root), *arguments, helper=helper, **environment)

    def _invoke(self, *arguments, helper=HELPER, **environment):
        inherited = os.environ.copy()
        inherited.pop("MURIEL_SSH_TARGET", None)
        return subprocess.run(
            [str(helper), *arguments],
            cwd=self.path,
            env=inherited
            | {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "STAGING_SUPERUSER_LOG": str(self.log),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def _commands(self):
        return [tuple(line.split("\t")) for line in self.log.read_text().splitlines()]

    def test_operator_target_uses_only_the_staging_host_helper(self):
        # Alias spelling selects a physical host, not the application environment.
        for alias in ("staging-a", "2.Shared_host-b", "production"):
            with self.subTest(alias=alias):
                self.log.unlink(missing_ok=True)
                result = self._invoke(alias, MURIEL_SSH_TARGET=alias)

                self.assertEqual(result.returncode, 0, result.stderr)
                (remote,) = self._commands()
                self.assertEqual(remote[:3], ("ssh", "-tt", alias))
                self.assertEqual(len(remote), 4)
                self.assertEqual(
                    shlex.split(remote[3]), ["sudo", "/srv/muriel-staging/create-superuser"]
                )

    def test_remote_output_and_failure_propagate(self):
        for status in (0, 1, 255):
            with self.subTest(status=status):
                result = self._invoke(
                    "staging-b",
                    MURIEL_SSH_TARGET="staging-b",
                    STAGING_SSH_STATUS=str(status),
                    STAGING_SSH_STDOUT="account prompt or output\n",
                    STAGING_SSH_STDERR="remote diagnostic\n",
                )
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stdout, "account prompt or output\n")
                self.assertEqual(result.stderr, "remote diagnostic\n")

    def test_refuses_missing_invalid_or_mismatched_selection_before_any_execution(self):
        selections = (
            None,
            "",
            " staging-a",
            "staging-a ",
            "staging-a\n",
            "-staging-a",
            ".staging-a",
            "_staging-a",
            "user@host",
            "ssh://host",
            "host:22",
            "stage*",
            "stage;id",
            "$(id)",
            "stáging",
            "different-host",
        )
        for setting in selections:
            with self.subTest(setting=setting):
                environment = {} if setting is None else {"MURIEL_SSH_TARGET": setting}
                target = "staging-a" if setting in (None, "different-host") else setting
                result = self._invoke(target, **environment)
                self.assertEqual(result.returncode, 2)
                self.assertIn("MURIEL_SSH_TARGET", result.stderr)
                self.assertFalse(self.log.exists())

    def test_refuses_wrong_remote_shapes_without_entering_local_mode(self):
        for arguments in (
            ("staging-a", "extra"),
            ("staging-a", "--root", str(self.root)),
            ("--root", str(self.root), "staging-a"),
            ("--root",),
            ("--root", str(self.root), "--root", str(self.root)),
            ("",),
            ("-oProxyCommand=id",),
            ("user@host",),
            ("staging-a ",),
            ("staging-a;id",),
        ):
            with self.subTest(arguments=arguments):
                result = self._invoke(*arguments, MURIEL_SSH_TARGET="staging-a")
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertFalse(self.log.exists())

    def _assert_staging_execution(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self._commands()
        self.assertFalse(any(command[0] == "ssh" for command in commands))
        execution = next(command for command in commands if command[0] == "django-execution")
        self.assertEqual(
            execution[:6],
            (
                "django-execution",
                "muriel_staging",
                "preceptor_project.staging_settings",
                "muriel_staging",
                "muriel_staging",
                str(self.release),
            ),
        )
        self.assertEqual(execution[6:], ("manage.py", "createsuperuser"))
        self.assertFalse(
            any("production_settings" in value for command in commands for value in command)
        )
        self.assertFalse(
            any("/srv/muriel/current" in value for command in commands for value in command)
        )

    def test_host_helper_runs_django_only_with_staging_configuration(self):
        self._assert_staging_execution(self._run())

    def test_installed_copy_runs_without_sibling_guard_or_ssh_configuration(self):
        installed = self.root / "srv/muriel-staging/create-superuser"
        shutil.copy2(HELPER, installed)
        self.assertFalse((installed.parent / "require-ssh-target.sh").exists())
        self._assert_staging_execution(self._run(helper=installed))
        self.log.unlink()
        self._assert_staging_execution(
            self._run(helper=installed, MURIEL_SSH_TARGET="not a valid alias")
        )

    def test_no_argument_installed_mode_requires_sudo_without_loading_ssh_support(self):
        installed = self.root / "srv/muriel-staging/create-superuser"
        shutil.copy2(HELPER, installed)
        # Refuse at the privilege boundary before any real-host file is inspected.
        result = self._invoke(helper=installed, STAGING_TEST_UID="1000")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run through sudo", result.stderr)
        self.assertEqual(self._commands(), [("id", "-u")])

    def test_normalizes_default_real_root_before_comparing_release_paths(self):
        helper = HELPER.read_text()
        self.assertIn('fi\nroot=$(CDPATH= cd -- "$root" && pwd -P)\n\nfail()', helper)
        self.assertIn('resolved_releases=$(CDPATH= cd -- "$releases" && pwd -P)', helper)
        self.assertIn('"$resolved_releases"/*)', helper)

    def test_rejects_an_active_release_outside_the_managed_directory(self):
        current = self.root / "srv/muriel-staging/current"
        current.unlink()
        outside_release = self.path / "outside-release"
        outside_release.mkdir()
        current.symlink_to(outside_release)
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside the managed release directory", result.stderr)
        self.assertFalse(any(command[0] == "django-execution" for command in self._commands()))

    def test_installed_copy_refuses_a_nonstaging_database_or_role(self):
        installed = self.root / "srv/muriel-staging/create-superuser"
        shutil.copy2(HELPER, installed)
        original = self.environment.read_text()
        for setting in ("PRECEPTOR_STAGING_POSTGRES_NAME", "PRECEPTOR_STAGING_POSTGRES_USER"):
            with self.subTest(setting=setting):
                self.log.unlink(missing_ok=True)
                self.environment.write_text(
                    original.replace(f"{setting}=muriel_staging", f"{setting}=muriel")
                )
                result = self._run(helper=installed)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"requires {setting}=muriel_staging", result.stderr)
                self.assertEqual(self._commands(), [("id", "-u")])
