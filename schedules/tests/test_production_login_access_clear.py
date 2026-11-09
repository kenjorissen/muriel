import os
import stat
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from schedules.management.commands import clear_production_login_access
from schedules.models import (
    AccountChange,
    Physician,
    PhysicianChange,
    SchedulerInvitation,
)
from schedules.services.physicians import create_physician, link_physician_user

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ops" / "clear-production-login-access"


class ProductionLoginAccessClearCommandTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.recovery = users.objects.create_superuser(
            username="recovery-admin", password="recovery-secret", email="recovery@example.com"
        )
        self.account = users.objects.create_user(
            username="ordinary-account", password="ordinary-secret", email="ordinary@example.com"
        )
        self.recovery_physician = create_physician(
            display_name="Recovery Physician", scheduler=self.recovery
        )
        link_physician_user(
            physician=self.recovery_physician, user=self.recovery, scheduler=self.recovery
        )
        self.physician = create_physician(
            display_name="Ordinary Physician", scheduler=self.recovery
        )
        link_physician_user(physician=self.physician, user=self.account, scheduler=self.recovery)
        self.invitation = SchedulerInvitation.objects.create(
            account=self.account,
            created_by=self.recovery,
            secret_digest="a" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        for account in (self.recovery, self.account):
            session = SessionStore()
            session["_auth_user_id"] = str(account.pk)
            session.save()
        self.prior_account_change = AccountChange.objects.create(
            account=self.account,
            action=AccountChange.Action.INVITED,
            recorded_value={"email": self.account.email},
            changed_by=self.recovery,
        )

    def _clear(self, username=None, *arguments):
        # Effect tests isolate the external target boundary. Its real rejection is below.
        with patch.object(clear_production_login_access, "validate_production_database"):
            call_command(
                "clear_production_login_access",
                username or self.recovery.username,
                *arguments,
                verbosity=0,
            )

    def _persisted_state(self):
        return {
            "users": list(get_user_model().objects.order_by("pk").values()),
            "physicians": list(Physician.objects.order_by("pk").values()),
            "invitations": list(SchedulerInvitation.objects.order_by("pk").values()),
            "sessions": list(Session.objects.order_by("session_key").values()),
            "account_changes": list(AccountChange.objects.order_by("pk").values()),
            "physician_changes": list(PhysicianChange.objects.order_by("pk").values()),
        }

    def test_disables_other_accounts_and_preserves_recovery_domain_identity_and_history(self):
        before = self._persisted_state()

        self._clear()

        self.recovery.refresh_from_db()
        self.account.refresh_from_db()
        self.physician.refresh_from_db()
        self.invitation.refresh_from_db()
        users_after = list(get_user_model().objects.order_by("pk").values())
        self.assertEqual(
            users_after,
            [
                row | {"is_active": False} if row["id"] == self.account.pk else row
                for row in before["users"]
            ],
        )
        physicians_after = list(Physician.objects.order_by("pk").values())
        self.assertEqual(
            physicians_after,
            [
                row | {"user_id": None} if row["id"] == self.physician.pk else row
                for row in before["physicians"]
            ],
        )
        invitations_after = list(SchedulerInvitation.objects.order_by("pk").values())
        self.assertEqual(len(invitations_after), len(before["invitations"]))
        invitation_after = next(row for row in invitations_after if row["id"] == self.invitation.pk)
        self.assertEqual(
            {
                key: value
                for key, value in invitation_after.items()
                if key not in {"status", "revoked_at"}
            },
            {
                key: value
                for key, value in before["invitations"][0].items()
                if key not in {"status", "revoked_at"}
            },
        )
        self.assertEqual(invitation_after["status"], SchedulerInvitation.Status.REVOKED)
        self.assertIsNotNone(invitation_after["revoked_at"])
        self.assertFalse(Session.objects.exists())
        self.assertEqual(
            list(
                PhysicianChange.objects.filter(
                    pk__in=[row["id"] for row in before["physician_changes"]]
                )
                .order_by("pk")
                .values()
            ),
            before["physician_changes"],
        )
        self.assertEqual(
            list(
                AccountChange.objects.filter(
                    pk__in=[row["id"] for row in before["account_changes"]]
                )
                .order_by("pk")
                .values()
            ),
            before["account_changes"],
        )
        new_physician_changes = PhysicianChange.objects.exclude(
            pk__in=[row["id"] for row in before["physician_changes"]]
        )
        self.assertEqual(new_physician_changes.count(), 1)
        change = new_physician_changes.get()
        self.assertEqual(change.physician_id, self.physician.pk)
        self.assertEqual(change.subject, PhysicianChange.Subject.ACCOUNT_IDENTITY)
        self.assertEqual(change.previous_value["user_id"], self.account.pk)
        self.assertIsNone(change.recorded_value)
        self.assertEqual(change.changed_by, self.recovery)
        self.assertTrue(self.client.login(username="recovery-admin", password="recovery-secret"))
        self.assertFalse(self.client.login(username="ordinary-account", password="ordinary-secret"))
        new_account_changes = AccountChange.objects.exclude(
            pk__in=[row["id"] for row in before["account_changes"]]
        )
        self.assertEqual(new_account_changes.count(), 2)
        self.assertEqual(
            set(
                new_account_changes.filter(
                    account=self.account, changed_by=self.recovery
                ).values_list("action", flat=True)
            ),
            {AccountChange.Action.UPDATED, AccountChange.Action.REVOKED},
        )

    def test_refuses_missing_or_nonrecovery_account_before_mutation(self):
        self.recovery.is_active = False
        self.recovery.save(update_fields=("is_active",))
        with patch.object(clear_production_login_access, "validate_production_database"):
            for username in (self.recovery.username, "missing", self.account.username):
                before = self._persisted_state()
                with (
                    self.subTest(username=username),
                    self.assertRaisesRegex(CommandError, "existing active superuser"),
                ):
                    call_command("clear_production_login_access", username, verbosity=0)
                self.assertEqual(self._persisted_state(), before)

    def test_recovery_check_does_not_mutate_login_access(self):
        before = self._persisted_state()
        self._clear(None, "--check-recovery")
        self.assertEqual(self._persisted_state(), before)

    def test_rejects_nonproduction_database_before_login_access_mutation(self):
        before = self._persisted_state()
        with self.assertRaisesRegex(CommandError, "requires PostgreSQL"):
            call_command("clear_production_login_access", self.recovery.username, verbosity=0)
        self.assertEqual(self._persisted_state(), before)


class ProductionLoginAccessClearHelperTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name)
        self.root, self.bin = self.path / "host", self.path / "bin"
        self.log, self.service_state = self.path / "commands.log", self.path / "service-state"
        self.helper = self.path / "clear-login-access-helper"
        self.root.mkdir()
        self.bin.mkdir()
        self._write_executable(self.helper, HELPER.read_text())
        release = self.root / "srv/muriel/releases/release"
        (release / ".venv/bin").mkdir(parents=True)
        (self.root / "srv/muriel/current").symlink_to("releases/release")
        (self.root / "var/backups/muriel").mkdir(parents=True)
        environment = self.root / "etc/muriel/environment"
        environment.parent.mkdir(parents=True)
        environment.write_text("PRECEPTOR_POSTGRES_NAME=muriel\nPRECEPTOR_POSTGRES_USER=muriel\n")
        self._write_dispatcher()
        self.service_state.write_text("active\n")

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
printf '%s' "$command" >> "$LOGIN_ACCESS_LOG"
for argument in "$@"; do printf '\\t%s' "$(printf '%s' "$argument" | tr '\\n' ' ')" >> "$LOGIN_ACCESS_LOG"; done
printf '\\n' >> "$LOGIN_ACCESS_LOG"
case "$command" in
  id) [ "${NONROOT:-}" != 1 ] && printf '0\\n' || printf '1000\\n' ;;
  pg_lsclusters) printf '18 main 5432 online postgres /var/lib/postgresql/18/main log\\n' ;;
  runuser)
    requested_user=$2; shift 3
    case "$1" in
      env)
        [ "$requested_user" = muriel ] || exit 1; shift
        environment_file= backup_directory= release_directory=
        while [ "$#" -gt 0 ]; do
          case "$1" in
            MURIEL_ENVIRONMENT_FILE=*) environment_file=${1#*=} ;;
            MURIEL_BACKUP_DIRECTORY=*) backup_directory=${1#*=} ;;
            MURIEL_RELEASE_DIRECTORY=*) release_directory=${1#*=} ;;
            *=*) ;;
            *) break ;;
          esac
          shift
        done
        [ "$environment_file" = "$LOGIN_ACCESS_ENVIRONMENT_FILE" ] || exit 1
        [ "$backup_directory" = "$LOGIN_ACCESS_BACKUP_DIRECTORY" ] || exit 1
        [ "$release_directory" = "$LOGIN_ACCESS_RELEASE_DIRECTORY" ] || exit 1
        [ "$1" = "$LOGIN_ACCESS_BACKUP_EXECUTABLE" ] || exit 1
        [ "$2" = --label ] && [ "$3" = pre-deploy ] || exit 1
        printf 'Backup protected locally and off-host: pre-deploy-20260820T120000Z.dump\\n' ;;
      sh)
        script=$3; shift 3
        MURIEL_FAKE_EFFECTIVE_USER="$requested_user" /bin/sh -c "$script" "$@" ;;
    esac ;;
  python)
    [ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel ] || exit 1
    [ "$DJANGO_SETTINGS_MODULE" = preceptor_project.production_settings ] || exit 1
    [ "$PRECEPTOR_POSTGRES_NAME" = muriel ] || exit 1
    [ "$PRECEPTOR_POSTGRES_USER" = muriel ] || exit 1
    printf 'login-access-execution\\t%s\\t%s\\t%s\\t%s\\t%s' "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" "$PRECEPTOR_POSTGRES_NAME" "$PRECEPTOR_POSTGRES_USER" "$(pwd)" >> "$LOGIN_ACCESS_LOG"
    for argument in "$@"; do printf '\\t%s' "$argument" >> "$LOGIN_ACCESS_LOG"; done
    printf '\\n' >> "$LOGIN_ACCESS_LOG"
    case " $* " in
      *' --check-recovery '*) [ "${FAIL_PREFLIGHT:-}" != 1 ] || exit 1 ;;
      *' clear_production_login_access recovery-admin '*) [ "${FAIL_CLEAR:-}" != 1 ] || exit 1 ;;
    esac ;;
  check-production)
    [ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel ] || exit 1
    [ "$DJANGO_SETTINGS_MODULE" = preceptor_project.production_settings ] || exit 1
    [ "$PRECEPTOR_POSTGRES_NAME" = muriel ] || exit 1
    [ "$PRECEPTOR_POSTGRES_USER" = muriel ] || exit 1
    printf 'check-production-execution\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" "$PRECEPTOR_POSTGRES_NAME" "$PRECEPTOR_POSTGRES_USER" "$(pwd)" >> "$LOGIN_ACCESS_LOG" ;;
  systemctl)
    case "$1 $2 ${3-}" in
      'stop muriel ') printf 'stopped\\n' > "$SERVICE_STATE" ;;
      'start muriel ') printf 'active\\n' > "$SERVICE_STATE" ;;
      'is-active --quiet muriel') [ "${FAIL_START:-}" != 1 ] && [ "$(cat "$SERVICE_STATE")" = active ] || exit 1 ;;
    esac ;;
esac
""",
        )
        for command in ("id", "pg_lsclusters", "runuser", "systemctl"):
            (self.bin / command).symlink_to(dispatcher.name)
        release = self.root / "srv/muriel/releases/release"
        (release / ".venv/bin/python").symlink_to(dispatcher)
        (release / "scripts").mkdir()
        (release / "scripts/check-production").symlink_to(dispatcher)

    def _run(self, *arguments, **environment):
        return subprocess.run(
            [str(self.helper), *arguments, "--root", str(self.root)],
            env=os.environ
            | {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "LOGIN_ACCESS_LOG": str(self.log),
                "SERVICE_STATE": str(self.service_state),
                "LOGIN_ACCESS_BACKUP_EXECUTABLE": str(self.root / "srv/muriel/backup-database"),
                "LOGIN_ACCESS_ENVIRONMENT_FILE": str(self.root / "etc/muriel/environment"),
                "LOGIN_ACCESS_BACKUP_DIRECTORY": str(self.root / "var/backups/muriel"),
                "LOGIN_ACCESS_RELEASE_DIRECTORY": str(self.root / "srv/muriel/current"),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def _commands(self):
        return [tuple(line.split("\t")) for line in self.log.read_text().splitlines()]

    def _command_index(self, *prefix):
        return next(
            i for i, command in enumerate(self._commands()) if command[: len(prefix)] == prefix
        )

    def test_preflights_recovery_then_backs_up_quiesces_checks_and_recovers_service(self):
        result = self._run(
            "recovery-admin",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin OTTER",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin OTTER", result.stderr)
        commands = self._commands()
        backup = next(c for c in commands if c[:5] == ("runuser", "-u", "muriel", "--", "env"))
        executable = str(self.root / "srv/muriel/backup-database")
        executable_index = backup.index(executable)
        self.assertEqual(backup[executable_index:], (executable, "--label", "pre-deploy"))
        backup_environment = dict(item.split("=", 1) for item in backup[5:executable_index])
        self.assertTrue(
            {
                "MURIEL_ENVIRONMENT_FILE": str(self.root / "etc/muriel/environment"),
                "MURIEL_BACKUP_DIRECTORY": str(self.root / "var/backups/muriel"),
                "MURIEL_RELEASE_DIRECTORY": str(self.root / "srv/muriel/current"),
            }.items()
            <= backup_environment.items()
        )
        preflight = next(
            c for c in commands if c[0] == "login-access-execution" and "--check-recovery" in c
        )
        clear = next(
            c for c in commands if c[0] == "login-access-execution" and "--check-recovery" not in c
        )
        for command in (preflight, clear):
            self.assertEqual(
                command[:6],
                (
                    "login-access-execution",
                    "muriel",
                    "preceptor_project.production_settings",
                    "muriel",
                    "muriel",
                    str(self.root / "srv/muriel/current"),
                ),
            )
            self.assertIn("manage.py", command)
            self.assertIn("clear_production_login_access", command)
            self.assertIn("recovery-admin", command)
        self.assertIn("--check-recovery", preflight)
        self.assertNotIn("--check-recovery", clear)
        readiness = (
            "check-production-execution",
            "muriel",
            "preceptor_project.production_settings",
            "muriel",
            "muriel",
            str(self.root / "srv/muriel/current"),
        )
        self.assertIn(readiness, commands)
        backup_index, stop_index, clear_index = (
            commands.index(backup),
            self._command_index("systemctl", "stop", "muriel"),
            commands.index(clear),
        )
        readiness_index = commands.index(readiness)
        start_index, active_index = (
            self._command_index("systemctl", "start", "muriel"),
            self._command_index("systemctl", "is-active", "--quiet", "muriel"),
        )
        self.assertLess(commands.index(preflight), backup_index)
        self.assertLess(backup_index, stop_index)
        self.assertLess(stop_index, clear_index)
        self.assertLess(clear_index, readiness_index)
        self.assertLess(readiness_index, start_index)
        self.assertLess(start_index, active_index)
        self.assertEqual(self.service_state.read_text(), "active\n")

    def test_mismatched_displayed_challenge_refuses_before_backup_or_service_change(self):
        result = self._run(
            "recovery-admin",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin LION",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin OTTER", result.stderr)
        self.assertIn("confirmation phrase did not match", result.stderr)
        self.assertFalse(
            any(
                str(self.root / "srv/muriel/backup-database") in command
                for command in self._commands()
            )
        )
        self.assertFalse(any(command[0] == "systemctl" for command in self._commands()))

    def test_refuses_non_root_before_confirmation_or_production_commands(self):
        result = self._run("recovery-admin", NONROOT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run through sudo", result.stderr)
        commands = self._commands()
        self.assertTrue(any(command[:2] == ("id", "-u") for command in commands))
        self.assertFalse(any(command[0] == "systemctl" for command in commands))
        self.assertFalse(any("clear_production_login_access" in command for command in commands))
        self.assertFalse(
            any(str(self.root / "srv/muriel/backup-database") in command for command in commands)
        )

    def test_recovery_preflight_failure_stops_before_backup_or_service_change(self):
        result = self._run(
            "recovery-admin",
            FAIL_PREFLIGHT="1",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin OTTER",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("recovery", result.stderr)
        self.assertIn("preflight", result.stderr)
        commands = self._commands()
        self.assertFalse(
            any(str(self.root / "srv/muriel/backup-database") in command for command in commands)
        )
        self.assertFalse(any(command[0] == "systemctl" for command in commands))

    def test_mutation_failure_restarts_the_service_and_remains_a_failure(self):
        result = self._run(
            "recovery-admin",
            FAIL_CLEAR="1",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="CLEAR MURIEL LOGIN ACCESS KEEP recovery-admin OTTER",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("operation failed", result.stderr)
        self.assertIn("restored", result.stderr)
        self._command_index("systemctl", "stop", "muriel")
        self._command_index("systemctl", "start", "muriel")
        self._command_index("systemctl", "is-active", "--quiet", "muriel")
        self.assertEqual(self.service_state.read_text(), "active\n")
