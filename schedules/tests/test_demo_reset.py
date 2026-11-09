import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from io import StringIO
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.db import models
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.utils import timezone

from schedules.management.commands import reset_demo
from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    InterviewShift,
    InterviewShiftChange,
    MonthlyPhysicianAvailabilityRule,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulerInvitation,
    Vacation,
    VacationChange,
)
from schedules.services.interviews import interview_findings_for_academic_year
from schedules.services.preceptors import draft_findings_for_month
from schedules.services.rounding import casual_rounder_rules_on, rounding_effect_on

ROOT = Path(__file__).resolve().parents[2]

DOCUMENTED_DEMO_FACULTY = (
    "Alex Kim",
    "Avery Patel",
    "Casey Nguyen",
    "Devon Park",
    "Jordan Lee",
    "Morgan Reed",
    "Quinn Baker",
    "Riley Chen",
    "Taylor Adams",
    "Wren Foster",
)
DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS = ("Fictional Cedar", "Fictional Harbor", "Fictional Summit")
DOCUMENTED_FACULTY_LEADERSHIP = {
    "Alex Kim": EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
    "Avery Patel": EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
    "Casey Nguyen": EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
}

_RESET_MODE_SCRIPT = r"""
import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import django

django.setup()

from django.conf import settings
from django.apps import apps
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.utils import timezone

from schedules.management.commands import reset_demo
from schedules.models import AccountChange, CustomAvailabilityCategory, Physician, SchedulerInvitation

# This inventory is maintained by this reset contract test.  It intentionally
# does not import a command's deletion list or discover all application models.
RESET_DOMAIN_MODEL_LABELS = (
    "schedules.PreceptingSolverProposalReconciliation",
    "schedules.PreceptingSolverProposalComparison",
    "schedules.PreceptingSolverLateDocReview",
    "schedules.PreceptingSolverProposalEdit",
    "schedules.PreceptingSolverProposal",
    "schedules.LateDocAssignmentChange",
    "schedules.LateDocAssignment",
    "schedules.StipendRateChange",
    "schedules.StipendRate",
    "schedules.HolidayOccurrenceChange",
    "schedules.HolidayOccurrence",
    "schedules.AcademicEventChange",
    "schedules.AcademicEventDateRange",
    "schedules.AcademicEvent",
    "schedules.AcademicEventMonthlyRuleChange",
    "schedules.AcademicEventMonthlyRule",
    "schedules.AcademicEventDefinitionChange",
    "schedules.CommunityPreceptorScheduleChange",
    "schedules.CustomAvailabilityCategoryChange",
    "schedules.InterviewShiftChange",
    "schedules.PreceptorAssignmentChange",
    "schedules.RoundingScheduleChange",
    "schedules.VacationChange",
    "schedules.PhysicianChange",
    "schedules.PreceptorAssignment",
    "schedules.CommunityPreceptorRule",
    "schedules.CustomAvailabilityCategoryAssignment",
    "schedules.InterviewShift",
    "schedules.CasualRounderRule",
    "schedules.NormalRounder",
    "schedules.MonthlyPhysicianAvailabilityRule",
    "schedules.FacultyAvailabilityTemplate",
    "schedules.EffectiveRole",
    "schedules.PreceptorSession",
    "schedules.SchedulePeriod",
    "schedules.AcademicEventDefinition",
    "schedules.Vacation",
    "schedules.Physician",
)

empty = os.environ["RESET_EMPTY"] == "1"
preserve_logins = os.environ["RESET_PRESERVE_LOGINS"] == "1"
call_command("migrate", interactive=False, verbosity=0, stdout=StringIO())

user_model = get_user_model()
preserved = user_model.objects.create_superuser(
    username="preserved",
    password="preserved-password",
    email="preserved@example.com",
)
group = reset_demo._scheduling_administrators_group()
preserved.groups.add(group)
direct_permission = Permission.objects.get(
    content_type__app_label="schedules",
    content_type__model="physician",
    codename="change_physician",
)
preserved.user_permissions.add(direct_permission)
invited = user_model.objects.create_user(username="invited", email="invited@example.com", is_active=False)
SchedulerInvitation.objects.create(
    account=preserved,
    created_by=preserved,
    secret_digest="a" * 64,
    status=SchedulerInvitation.Status.ACCEPTED,
    expires_at=timezone.now() + timedelta(days=1),
)
SchedulerInvitation.objects.create(
    account=invited,
    created_by=preserved,
    secret_digest="b" * 64,
    expires_at=timezone.now() + timedelta(days=1),
)
AccountChange.objects.create(
    account=preserved,
    changed_by=preserved,
    action=AccountChange.Action.UPDATED,
    recorded_value={"email": preserved.email},
)
session = SessionStore()
session["account"] = preserved.pk
session.save()
Physician.objects.create(display_name="Preserved physician", user=preserved)
reset_demo.seed_demo_data(scheduler=preserved, scheduling_administrator=preserved)

arguments = ["--confirm"]
if empty:
    arguments.append("--empty")
if preserve_logins:
    arguments.append("--except-logins")
output = StringIO()
with patch.object(
    reset_demo,
    "_canonical_main_worktree",
    return_value=Path(settings.BASE_DIR).resolve(),
):
    call_command("reset_demo", *arguments, verbosity=0, stdout=output)

preserved_state = None
if user_model.objects.filter(username="preserved").exists():
    preserved = user_model.objects.get(username="preserved")
    preserved_state = {
        "email": preserved.email,
        "password_matches": authenticate(username="preserved", password="preserved-password")
        is not None,
        "is_active": preserved.is_active,
        "is_staff": preserved.is_staff,
        "is_superuser": preserved.is_superuser,
        "has_group": preserved.groups.filter(name="Scheduling Administrators").exists(),
        "has_direct_permission": preserved.user_permissions.filter(pk=direct_permission.pk).exists(),
    }

print(
    json.dumps(
        {
            "users": list(user_model.objects.order_by("username").values_list("username", flat=True)),
            "invitations": SchedulerInvitation.objects.count(),
            "account_changes": AccountChange.objects.count(),
            "sessions": Session.objects.count(),
            "preserved_physician_link": Physician.objects.filter(user__username="preserved").exists(),
            "preserved": preserved_state,
            "domain": {
                label: apps.get_model(label).objects.count()
                for label in RESET_DOMAIN_MODEL_LABELS
            },
            "scheduling_authority_count": apps.get_model(
                "schedules.SchedulingMutationAuthority"
            ).objects.count(),
            "system_admin_category_count": CustomAvailabilityCategory.objects.filter(
                system_key=CustomAvailabilityCategory.SystemKey.ADMIN
            ).count(),
            "non_system_category_count": CustomAvailabilityCategory.objects.exclude(
                system_key=CustomAvailabilityCategory.SystemKey.ADMIN
            ).count(),
            "demo_scheduler": user_model.objects.filter(username=reset_demo.DEMO_USERNAME).exists(),
            "demo_administrator": user_model.objects.filter(
                username=reset_demo.DEMO_SCHEDULING_ADMINISTRATOR_USERNAME
            ).exists(),
            "output": output.getvalue(),
        }
    )
)
"""


class DemoResetGuardTests(TestCase):
    destroyed_reset_categories = (
        "sessions",
        "invitation state",
        "account-change history",
        "physician-account links",
        "physicians",
        "scheduling data",
        "retained domain history",
    )
    preserved_login_categories = (
        "django login accounts",
        "password hashes",
        "unique email",
        "active",
        "staff",
        "superuser",
        "group memberships",
        "direct permissions",
    )

    def assert_reset_scope(self, message, *, preserves_logins, detailed_login_scope=True):
        message = " ".join(message.casefold().split())
        clauses = re.split(r"\s*(?:;|\.|\bbut\b)\s*", message)
        if preserves_logins:
            preserved_scope = next((clause for clause in clauses if "preserv" in clause), "")
            self.assertTrue(preserved_scope)
            preserved_categories = (
                self.preserved_login_categories
                if detailed_login_scope
                else ("django login accounts and authority",)
            )
            for category in preserved_categories:
                self.assertIn(category, preserved_scope)
        else:
            clauses = (message,)

        destroyed_scope = next(
            (clause for clause in clauses if "destroy" in clause or "permanently lost" in clause),
            "",
        )
        self.assertTrue(destroyed_scope)
        if not preserves_logins:
            self.assertIn("django login accounts and authority", destroyed_scope)
        for category in self.destroyed_reset_categories:
            self.assertIn(category, destroyed_scope)

    def help_option_block(self, rendered_help, option):
        option_heading = rf"^[ \t]*{re.escape(option)}(?:[ \t]|,|$)"
        match = re.search(
            rf"{option_heading}.*?(?=^[ \t]*--\S|\Z)",
            rendered_help,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing help for {option}")
        return match.group()

    def test_requires_confirmation_before_inspecting_or_changing_the_database(self):
        with patch.object(reset_demo, "_reset_database_path") as reset_path:
            with self.assertRaisesRegex(
                CommandError, "Django login accounts and authority, sessions, invitation state"
            ):
                call_command("reset_demo")

        reset_path.assert_not_called()

    @override_settings(DEBUG=False)
    def test_command_refuses_non_development_mode_before_resetting(self):
        Physician.objects.create(display_name="Unchanged physician")
        with self.assertRaisesRegex(CommandError, "development mode"):
            call_command("reset_demo", "--confirm")

        self.assertTrue(Physician.objects.filter(display_name="Unchanged physician").exists())

    def test_command_refuses_any_sqlite_path_other_than_main_worktree_database(self):
        database = settings.DATABASES["default"].copy()
        database.update({"ENGINE": "django.db.backends.sqlite3", "NAME": "/tmp/not-demo.sqlite3"})
        with override_settings(DEBUG=True, DATABASES={"default": database}):
            with self.assertRaisesRegex(CommandError, "expected db.sqlite3"):
                call_command("reset_demo", "--confirm")

    def test_command_refuses_non_sqlite_database_before_resetting(self):
        Physician.objects.create(display_name="Unchanged physician")
        database = settings.DATABASES["default"].copy()
        database["ENGINE"] = "django.db.backends.postgresql"
        with override_settings(DEBUG=True, DATABASES={"default": database}):
            with self.assertRaisesRegex(CommandError, "SQLite"):
                call_command("reset_demo", "--confirm")

        self.assertTrue(Physician.objects.filter(display_name="Unchanged physician").exists())

    def test_command_refuses_a_non_main_worktree_even_when_the_database_path_matches(self):
        base_dir = Path(settings.BASE_DIR).resolve()
        database = settings.DATABASES["default"].copy()
        database.update({"ENGINE": "django.db.backends.sqlite3", "NAME": base_dir / "db.sqlite3"})
        with (
            override_settings(DEBUG=True, DATABASES={"default": database}),
            patch.object(reset_demo, "_canonical_main_worktree", return_value=base_dir.parent),
        ):
            with self.assertRaisesRegex(CommandError, "canonical main worktree"):
                call_command("reset_demo", "--confirm")

    def test_reset_removes_only_the_configured_database_file_before_migrating(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "db.sqlite3"
            database_path.write_text("old local data")
            sibling = Path(temporary_directory) / "keep-me.txt"
            sibling.write_text("not a database")

            command = reset_demo.Command()
            calls = []
            with (
                patch.object(reset_demo, "_reset_database_path", return_value=database_path),
                patch.object(
                    reset_demo,
                    "call_command",
                    side_effect=lambda *args, **kwargs: calls.append((args, kwargs)),
                ),
                patch.object(
                    reset_demo,
                    "seed_demo_data",
                    side_effect=lambda: calls.append(((), {})),
                ),
                patch.object(reset_demo.connections, "close_all"),
            ):
                command.handle(confirm=True, verbosity=0)

            self.assertFalse(database_path.exists())
            self.assertEqual(sibling.read_text(), "not a database")
            migrate_positions = [
                index for index, (args, _) in enumerate(calls) if args and args[0] == "migrate"
            ]
            seed_position = next(index for index, (args, _) in enumerate(calls) if not args)
            self.assertTrue(migrate_positions)
            self.assertLess(min(migrate_positions), seed_position)

    def test_selective_reset_describes_preserved_and_destroyed_categories_before_deletion(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "db.sqlite3"
            database_path.write_text("old local data")
            output = StringIO()
            command = reset_demo.Command(stdout=output)
            observed_scope_at_deletion = []
            original_unlink = Path.unlink

            def capture_scope_at_database_deletion(path, *args, **kwargs):
                if path == database_path:
                    observed_scope_at_deletion.append(output.getvalue())
                return original_unlink(path, *args, **kwargs)

            with (
                patch.object(reset_demo, "_reset_database_path", return_value=database_path),
                patch.object(reset_demo, "_capture_login_accounts", return_value=()),
                patch.object(reset_demo, "_restore_login_accounts"),
                patch.object(reset_demo, "call_command"),
                patch.object(reset_demo, "seed_demo_data", return_value=False),
                patch.object(reset_demo.connections, "close_all"),
                patch.object(
                    Path, "unlink", autospec=True, side_effect=capture_scope_at_database_deletion
                ),
            ):
                command.handle(confirm=True, except_logins=True, verbosity=0)

        self.assertEqual(len(observed_scope_at_deletion), 1)
        self.assert_reset_scope(observed_scope_at_deletion[0], preserves_logins=True)
        message = output.getvalue()
        self.assertNotIn(reset_demo.DEMO_USERNAME, message)
        self.assertNotIn(reset_demo.DEMO_PASSWORD, message)
        self.assertNotIn("pbkdf2_", message)

    def test_help_describes_the_full_and_selective_reset_scopes(self):
        rendered_help = reset_demo.Command().create_parser("manage.py", "reset_demo").format_help()
        confirm_help = self.help_option_block(rendered_help, "--confirm")
        except_logins_help = self.help_option_block(rendered_help, "--except-logins")
        empty_help = self.help_option_block(rendered_help, "--empty")

        self.assert_reset_scope(confirm_help, preserves_logins=False)
        self.assert_reset_scope(except_logins_help, preserves_logins=True)
        self.assertIn("empty", empty_help.casefold())
        self.assertIn("migrat", empty_help.casefold())
        self.assertIn("seed", empty_help.casefold())

    def test_selective_reset_confirmation_describes_preserved_and_destroyed_categories(self):
        with self.assertRaises(CommandError) as raised:
            call_command("reset_demo", "--except-logins")

        self.assert_reset_scope(
            str(raised.exception), preserves_logins=True, detailed_login_scope=False
        )

    def test_empty_reset_confirmation_describes_the_empty_result_and_login_scope(self):
        with self.assertRaises(CommandError) as raised:
            call_command("reset_demo", "--empty", "--except-logins")

        self.assert_reset_scope(
            str(raised.exception), preserves_logins=True, detailed_login_scope=False
        )
        self.assertIn("empty", str(raised.exception).casefold())


class ResetModeTests(SimpleTestCase):
    def run_reset(self, *, empty, preserve_logins):
        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            settings_path = temporary_path / "reset_mode_settings.py"
            settings_path.write_text(
                "from pathlib import Path\n"
                "from preceptor_project.settings import *\n"
                f"BASE_DIR = Path({str(temporary_path)!r})\n"
                "DATABASES = {\n"
                "    'default': {\n"
                "        'ENGINE': 'django.db.backends.sqlite3',\n"
                "        'NAME': BASE_DIR / 'db.sqlite3',\n"
                "    },\n"
                "}\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "DJANGO_SETTINGS_MODULE": "reset_mode_settings",
                    "PYTHONPATH": os.pathsep.join(
                        filter(
                            None, (str(temporary_path), str(ROOT), environment.get("PYTHONPATH"))
                        )
                    ),
                    "RESET_EMPTY": str(int(empty)),
                    "RESET_PRESERVE_LOGINS": str(int(preserve_logins)),
                }
            )
            completed = subprocess.run(
                [sys.executable, "-c", _RESET_MODE_SCRIPT],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            0,
            f"reset mode subprocess failed:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    @tag("reset-tool-integration")
    def test_modes_rebuild_real_databases_with_the_requested_login_scope(self):
        for empty, preserve_logins in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(empty=empty, preserve_logins=preserve_logins):
                state = self.run_reset(empty=empty, preserve_logins=preserve_logins)
                expected_users = set()
                if preserve_logins:
                    expected_users.add("preserved")
                if not empty:
                    expected_users.update(
                        {
                            reset_demo.DEMO_USERNAME,
                            reset_demo.DEMO_SCHEDULING_ADMINISTRATOR_USERNAME,
                        }
                    )

                self.assertEqual(set(state["users"]), expected_users)
                self.assertEqual(state["invitations"], 0)
                self.assertEqual(state["account_changes"], 0)
                self.assertEqual(state["sessions"], 0)
                self.assertFalse(state["preserved_physician_link"])

                if preserve_logins:
                    self.assertEqual(
                        state["preserved"],
                        {
                            "email": "preserved@example.com",
                            "password_matches": True,
                            "is_active": True,
                            "is_staff": True,
                            "is_superuser": True,
                            "has_group": True,
                            "has_direct_permission": True,
                        },
                    )
                else:
                    self.assertIsNone(state["preserved"])

                if empty:
                    self.assertEqual(state["scheduling_authority_count"], 1)
                    self.assertEqual(state["system_admin_category_count"], 1)
                    self.assertEqual(state["non_system_category_count"], 0)
                    self.assertTrue(all(count == 0 for count in state["domain"].values()))
                    self.assertFalse(state["demo_scheduler"])
                    self.assertFalse(state["demo_administrator"])
                    self.assertIn("Local database rebuilt empty", state["output"])
                else:
                    self.assertEqual(state["domain"]["schedules.Physician"], 13)
                    self.assertGreater(state["domain"]["schedules.PreceptorSession"], 0)
                    self.assertTrue(state["demo_scheduler"])
                    self.assertTrue(state["demo_administrator"])
                    self.assertIn("Local demonstration data created", state["output"])

    def test_empty_reset_preserves_requested_login_scope(self):
        state = self.run_reset(empty=True, preserve_logins=True)

        self.assertEqual(set(state["users"]), {"preserved"})
        self.assertEqual(state["invitations"], 0)
        self.assertEqual(state["account_changes"], 0)
        self.assertEqual(state["sessions"], 0)
        self.assertFalse(state["preserved_physician_link"])
        self.assertEqual(state["scheduling_authority_count"], 1)
        self.assertEqual(state["system_admin_category_count"], 1)
        self.assertEqual(state["non_system_category_count"], 0)
        self.assertTrue(all(count == 0 for count in state["domain"].values()))
        self.assertIn("Local database rebuilt empty", state["output"])


class LoginPreservationTests(TestCase):
    def test_snapshot_restores_login_credentials_and_authority_but_not_invitation_only_accounts(
        self,
    ):
        user_model = get_user_model()
        ordinary = user_model.objects.create_user(
            username="ordinary", password="ordinary-password", email="ordinary@example.com"
        )
        manager = user_model.objects.create_user(
            username="manager", password="manager-password", email="manager@example.com"
        )
        user_model.objects.create_user(
            username="staff", password="staff-password", email="staff@example.com", is_staff=True
        )
        administrator = user_model.objects.create_superuser(
            username="administrator", password="administrator-password", email="admin@example.com"
        )
        manager.groups.add(reset_demo._scheduling_administrators_group())
        direct_permission = Permission.objects.get(
            content_type__app_label="schedules",
            content_type__model="physician",
            codename="change_physician",
        )
        ordinary.user_permissions.add(direct_permission)
        invitation_only = user_model.objects.create_user(
            username="invited", email="invited@example.com", is_active=False
        )
        SchedulerInvitation.objects.create(
            account=invitation_only,
            created_by=administrator,
            secret_digest="a" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        session = SessionStore()
        session["account"] = ordinary.pk
        session.save()

        snapshot = reset_demo._capture_login_accounts()
        call_command("flush", interactive=False, verbosity=0)
        reset_demo._restore_login_accounts(snapshot)

        restored = user_model.objects.get(username="ordinary")
        self.assertEqual(restored.email, "ordinary@example.com")
        self.assertTrue(restored.has_perm("schedules.change_physician"))
        self.assertIsNotNone(authenticate(username="ordinary", password="ordinary-password"))
        self.assertTrue(
            user_model.objects.get(username="manager")
            .groups.filter(name="Scheduling Administrators")
            .exists()
        )
        self.assertTrue(user_model.objects.get(username="staff").is_staff)
        self.assertTrue(user_model.objects.get(username="administrator").is_superuser)
        self.assertFalse(user_model.objects.filter(username="invited").exists())
        self.assertFalse(SchedulerInvitation.objects.exists())
        self.assertFalse(Session.objects.exists())

    @tag("reset-tool-integration")
    def test_preserved_demo_scheduler_is_not_changed_when_seeding(self):
        user_model = get_user_model()
        scheduler = user_model.objects.create_user(
            username=reset_demo.DEMO_USERNAME,
            password="kept-password",
            email="kept@example.com",
            is_active=False,
        )
        scheduler.groups.add(reset_demo._scheduling_administrators_group())
        direct_permission = Permission.objects.get(
            content_type__app_label="schedules",
            content_type__model="physician",
            codename="change_physician",
        )
        scheduler.user_permissions.add(direct_permission)
        snapshot = reset_demo._capture_login_accounts()
        call_command("flush", interactive=False, verbosity=0)
        reset_demo._restore_login_accounts(snapshot)

        self.assertFalse(reset_demo.seed_demo_data())
        scheduler = user_model.objects.get(username=reset_demo.DEMO_USERNAME)
        self.assertEqual(scheduler.email, "kept@example.com")
        self.assertFalse(scheduler.is_active)
        self.assertTrue(scheduler.groups.filter(name="Scheduling Administrators").exists())
        self.assertTrue(
            scheduler.user_permissions.filter(codename=direct_permission.codename).exists()
        )
        self.assertTrue(scheduler.check_password("kept-password"))

    @tag("reset-tool-integration")
    def test_seeding_demo_scheduling_administrator_does_not_expand_preserved_group_authority(self):
        user_model = get_user_model()
        preserved_user = user_model.objects.create_user(
            username="preserved", password="preserved-password", email="preserved@example.com"
        )
        group = reset_demo._scheduling_administrators_group()
        group.permissions.clear()
        preserved_user.groups.add(group)
        snapshot = reset_demo._capture_login_accounts()
        call_command("flush", interactive=False, verbosity=0)
        reset_demo._restore_login_accounts(snapshot)

        reset_demo.seed_demo_data()

        permission = Permission.objects.get(
            content_type__app_label="schedules",
            content_type__model="physician",
            codename="manage_scheduling_administration",
        )
        preserved_user = user_model.objects.get(username="preserved")
        group = preserved_user.groups.get(name="Scheduling Administrators")
        demo_scheduling_administrator = user_model.objects.get(
            username=reset_demo.DEMO_SCHEDULING_ADMINISTRATOR_USERNAME
        )
        self.assertFalse(group.permissions.filter(pk=permission.pk).exists())
        self.assertFalse(preserved_user.has_perm("schedules.manage_scheduling_administration"))
        self.assertTrue(
            demo_scheduling_administrator.has_perm("schedules.manage_scheduling_administration")
        )
        self.assertTrue(
            demo_scheduling_administrator.user_permissions.filter(pk=permission.pk).exists()
        )

    def test_missing_preserved_authority_fails_with_actionable_detail(self):
        snapshot = (
            {
                "username": "ordinary",
                "password": "!",
                "email": "ordinary@example.com",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
                "groups": (),
                "permissions": (("missing", "authority", "not_available"),),
            },
        )

        with self.assertRaisesRegex(CommandError, "missing.authority.not_available"):
            reset_demo._restore_login_accounts(snapshot)

    @tag("reset-tool-integration")
    def test_command_output_never_includes_credentials_when_creating_the_demo_scheduler(self):
        output = StringIO()
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "db.sqlite3"
            command = reset_demo.Command(stdout=output)
            with (
                patch.object(reset_demo, "_reset_database_path", return_value=database_path),
                patch.object(reset_demo, "call_command"),
                patch.object(reset_demo.connections, "close_all"),
            ):
                command.handle(confirm=True, except_logins=False, verbosity=0)

        scheduler = get_user_model().objects.get(username=reset_demo.DEMO_USERNAME)
        message = output.getvalue()
        self.assertIn("Destroying Django login accounts and authority, sessions", message)
        self.assertIn("Physician-account links, Physicians, scheduling data", message)
        self.assertNotIn(reset_demo.DEMO_USERNAME, message)
        self.assertNotIn(reset_demo.DEMO_PASSWORD, message)
        self.assertNotIn(scheduler.password, message)
        self.assertNotIn("Password:", message)


class DemoSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        reset_demo.seed_demo_data()

    def assert_approximately_four_workweeks(self, covered_workday_half_days):
        self.assertGreaterEqual(covered_workday_half_days, 30)
        self.assertLessEqual(covered_workday_half_days, 50)

    def assert_roughly_even_quarter_distribution(self, quarterly_definition_counts):
        self.assertLessEqual(max(quarterly_definition_counts) - min(quarterly_definition_counts), 6)

    def assert_weekly_rotation(self, rounder_names):
        self.assertTrue(all(first != second for first, second in pairwise(rounder_names)))

    def test_seed_has_deterministic_physicians_sources_and_retained_provenance(self):
        scheduler = get_user_model().objects.get(username=reset_demo.DEMO_USERNAME)
        scheduling_administrator = get_user_model().objects.get(
            username=reset_demo.DEMO_SCHEDULING_ADMINISTRATOR_USERNAME
        )
        self.assertFalse(scheduler.groups.filter(name="Scheduling Administrators").exists())
        self.assertTrue(
            scheduling_administrator.groups.filter(name="Scheduling Administrators").exists()
        )
        self.assertEqual(
            list(Physician.objects.values_list("display_name", flat=True)),
            sorted(DOCUMENTED_DEMO_FACULTY + DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS),
        )
        faculty_roles = EffectiveRole.objects.filter(role=EffectiveRole.Role.FACULTY)
        self.assertEqual(
            set(faculty_roles.values_list("physician__display_name", flat=True)),
            set(DOCUMENTED_DEMO_FACULTY),
        )
        self.assertTrue(
            all(
                role.effective_start <= date(2026, 8, 1) and role.effective_end >= date(2027, 7, 31)
                for role in faculty_roles
            )
        )
        self.assertEqual(
            Physician.objects.filter(display_name__in=DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS).count(),
            3,
        )
        self.assertTrue(
            EffectiveRole.objects.filter(
                physician__display_name="Alex Kim", role=EffectiveRole.Role.FLOAT_PRECEPTOR
            ).exists()
        )
        self.assertTrue(
            EffectiveRole.objects.filter(
                physician__display_name=reset_demo.EXTERNAL_PATIENT_CARE_PHYSICIAN,
                role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            ).exists()
        )
        template_counts = {
            name: FacultyAvailabilityTemplate.objects.filter(physician__display_name=name).count()
            for name in DOCUMENTED_DEMO_FACULTY
        }
        self.assertEqual(
            {name for name, count in template_counts.items() if count == 0}, {"Taylor Adams"}
        )
        self.assertTrue(all(1 <= count <= 2 for count in template_counts.values() if count))
        monday_am_states = {
            selection["base_state"]
            for template in FacultyAvailabilityTemplate.objects.all()
            for selection in [template.slots["monday_am"]]
            if "base_state" in selection
        }
        self.assertEqual(
            monday_am_states,
            {
                FacultyAvailabilityTemplate.AvailabilityState.OPEN,
                FacultyAvailabilityTemplate.AvailabilityState.OFF,
                FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
            },
        )
        self.assertIn(
            FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE,
            [
                selection["base_state"]
                for template in FacultyAvailabilityTemplate.objects.all()
                for selection in template.slots.values()
                if "base_state" in selection
            ],
        )
        vacation_ids = set(Vacation.objects.values_list("pk", flat=True))
        vacation_changes = VacationChange.objects.all()
        self.assertEqual(
            set(vacation_changes.values_list("vacation_id_at_recording", flat=True)), vacation_ids
        )
        self.assertTrue(all(vacation.note == "" for vacation in Vacation.objects.all()))
        self.assertTrue(
            all(
                change.vacation_id == change.vacation_id_at_recording
                and change.action == VacationChange.Action.CREATED
                and change.physician_id == change.vacation.physician_id
                and change.previous_value is None
                and change.recorded_value is not None
                and change.previous_note is None
                and change.recorded_note == ""
                and change.changed_by == scheduler
                for change in vacation_changes
            )
        )
        physician_changes = PhysicianChange.objects.all()
        self.assertTrue(
            all(
                change.changed_by == scheduler
                and change.reason == reset_demo.DEMO_REASON
                and change.recorded_value is not None
                for change in physician_changes.filter(
                    subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE
                )
            )
        )
        self.assertTrue(
            all(
                change.changed_by == scheduling_administrator
                and change.reason == reset_demo.DEMO_REASON
                and change.recorded_value is not None
                for change in physician_changes.exclude(
                    subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE
                )
            )
        )
        for subject, source_ids in (
            (PhysicianChange.Subject.IDENTITY, Physician.objects.values_list("pk", flat=True)),
            (PhysicianChange.Subject.ROLE, EffectiveRole.objects.values_list("pk", flat=True)),
            (
                PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
                FacultyAvailabilityTemplate.objects.values_list("pk", flat=True),
            ),
            (
                PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE,
                MonthlyPhysicianAvailabilityRule.objects.values_list("pk", flat=True),
            ),
        ):
            with self.subTest(history_subject=subject):
                self.assertTrue(
                    set(source_ids).issubset(
                        set(
                            physician_changes.filter(subject=subject).values_list(
                                "subject_id", flat=True
                            )
                        )
                    )
                )
        self.assertEqual(
            list(
                MonthlyPhysicianAvailabilityRule.objects.order_by(
                    "physician__display_name"
                ).values_list("physician__display_name", "weekday", "ordinals", "periods")
            ),
            [
                ("Alex Kim", 1, [2], ["AM"]),
                ("Avery Patel", 2, [1, 3], ["PM"]),
                ("Taylor Adams", 3, [3], ["AM", "PM"]),
            ],
        )
        assignment_changes = PreceptorAssignmentChange.objects.filter(reason=reset_demo.DEMO_REASON)
        self.assertEqual(
            set(assignment_changes.values_list("assignment_id", flat=True)),
            set(PreceptorAssignment.objects.values_list("pk", flat=True)),
        )
        self.assertTrue(
            all(
                change.action == PreceptorAssignmentChange.Action.ASSIGNED
                and change.session_id
                == PreceptorAssignment.objects.get(pk=change.assignment_id).session_id
                and change.previous_value is None
                and change.recorded_value is not None
                and change.changed_by == scheduler
                for change in assignment_changes
            )
        )

    def test_seed_skips_closed_precepting_coverage(self):
        self.assertFalse(
            PreceptorAssignment.objects.filter(session__date=date(2026, 9, 7)).exists()
        )

    def test_community_preceptors_and_faculty_leadership_have_effective_retained_roles(self):
        roles = EffectiveRole.objects.select_related("physician")
        self.assertEqual(
            {
                role.physician.display_name: role.faculty_leadership
                for role in roles.filter(role=EffectiveRole.Role.FACULTY)
                if role.faculty_leadership
            },
            DOCUMENTED_FACULTY_LEADERSHIP,
        )
        self.assertEqual(
            roles.filter(
                role=EffectiveRole.Role.FACULTY,
                faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            ).count(),
            1,
        )
        self.assertEqual(
            roles.filter(
                role=EffectiveRole.Role.FACULTY,
                faculty_leadership=EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
            ).count(),
            2,
        )
        self.assertEqual(
            list(
                roles.filter(role=EffectiveRole.Role.COMMUNITY_PRECEPTOR)
                .order_by("physician__display_name")
                .values_list(
                    "physician__display_name",
                    "effective_start",
                    "effective_end",
                    "faculty_equivalent",
                )
            ),
            [
                ("Fictional Cedar", date(2026, 8, 1), date(2027, 7, 31), True),
                ("Fictional Harbor", date(2026, 8, 1), date(2027, 7, 31), True),
                ("Fictional Summit", date(2026, 8, 1), date(2027, 7, 31), True),
            ],
        )
        self.assertFalse(
            roles.filter(
                physician__display_name__in=DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS,
                role=EffectiveRole.Role.FACULTY,
            ).exists()
        )
        self.assertEqual(
            list(
                roles.filter(role=EffectiveRole.Role.CASUAL_ROUNDER)
                .order_by("physician__display_name")
                .values_list("physician__display_name", "effective_start", "effective_end")
            ),
            [
                ("Fictional Cedar", date(2026, 8, 1), date(2026, 9, 30)),
                ("Fictional Harbor", date(2026, 8, 1), None),
                ("Fictional Summit", date(2026, 8, 1), date(2026, 9, 30)),
            ],
        )
        self.assertFalse(
            roles.filter(
                physician__display_name__in=reset_demo.DEMO_FACULTY_PHYSICIANS,
                role=EffectiveRole.Role.CASUAL_ROUNDER,
            ).exists()
        )

    def test_community_rules_have_zero_one_and_two_retained_examples(self):
        self.assertEqual(
            {
                name: CommunityPreceptorRule.objects.filter(physician__display_name=name).count()
                for name in DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS
            },
            {"Fictional Cedar": 0, "Fictional Harbor": 1, "Fictional Summit": 2},
        )
        self.assertEqual(
            list(
                CommunityPreceptorRule.objects.order_by(
                    "physician__display_name", "weekday"
                ).values_list(
                    "physician__display_name",
                    "weekday",
                    "ordinals",
                    "periods",
                    "effective_start",
                    "effective_end",
                )
            ),
            [
                ("Fictional Harbor", 0, [4], ["AM", "PM"], date(2026, 8, 1), date(2026, 8, 30)),
                ("Fictional Summit", 2, [2, 4], ["AM", "PM"], date(2026, 8, 1), date(2026, 9, 15)),
                ("Fictional Summit", 4, [3, 4], ["PM"], date(2026, 8, 15), date(2026, 9, 7)),
            ],
        )
        scheduler = get_user_model().objects.get(username=reset_demo.DEMO_USERNAME)
        changes = CommunityPreceptorScheduleChange.objects.all()
        self.assertEqual(changes.count(), 3)
        self.assertTrue(
            all(
                change.action == CommunityPreceptorScheduleChange.Action.CREATED
                and change.community_preceptor_rule_id == change.source_id_at_recording
                and change.changed_by == scheduler
                and change.reason == reset_demo.DEMO_REASON
                and change.previous_value is None
                and change.recorded_value is not None
                for change in changes
            )
        )

    def test_interview_season_has_documented_assignments_and_retained_history(self):
        shifts = InterviewShift.objects.select_related("physician").order_by("date")
        self.assertEqual(shifts.count(), 15)
        self.assertTrue(all(shift.is_active and shift.physician_id for shift in shifts))
        self.assertEqual(
            {(shift.date.year, shift.date.month) for shift in shifts},
            {(2026, 10), (2026, 11), (2026, 12), (2027, 1)},
        )
        self.assertTrue(
            all(
                EffectiveRole.objects.filter(
                    physician=shift.physician,
                    role=EffectiveRole.Role.FACULTY,
                    effective_start__lte=shift.date,
                    effective_end__gte=shift.date,
                ).exists()
                for shift in shifts
            )
        )
        self.assertTrue(
            shifts.filter(
                physician__roles__faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
            ).exists()
        )
        scheduler = get_user_model().objects.get(username=reset_demo.DEMO_USERNAME)
        changes = InterviewShiftChange.objects.all()
        self.assertTrue(
            all(
                change.shift_id == change.shift_id_at_recording
                and change.changed_by == scheduler
                and change.recorded_value is not None
                for change in changes
            )
        )
        for shift in shifts:
            with self.subTest(shift=shift.date):
                self.assertEqual(
                    set(
                        changes.filter(shift_id_at_recording=shift.pk).values_list(
                            "action", flat=True
                        )
                    ),
                    {InterviewShiftChange.Action.CREATED, InterviewShiftChange.Action.ASSIGNED},
                )

    def test_seeded_assignment_findings_are_limited_to_the_deliberate_examples(self):
        for assignment in PreceptorAssignment.objects.select_related("session", "physician"):
            self.assertFalse(
                assignment.responsibility == PreceptorAssignment.Responsibility.SECONDARY
                and EffectiveRole.objects.filter(
                    physician=assignment.physician,
                    role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                    effective_start__lte=assignment.session.date,
                )
                .filter(
                    models.Q(effective_end__isnull=True)
                    | models.Q(effective_end__gte=assignment.session.date)
                )
                .exists()
            )
        for month in (date(2026, 8, 1), date(2026, 9, 1)):
            with self.subTest(month=month):
                findings = draft_findings_for_month(month).findings
                assignment_findings = [
                    finding
                    for finding in findings
                    if finding.code
                    in {
                        "not_available_assignment",
                        "backup_only_assignment",
                        "role_ineligible_assignment",
                        "interview_assignment_conflict",
                    }
                ]
                self.assertCountEqual(
                    [(finding.code, finding.severity) for finding in assignment_findings],
                    [
                        ("not_available_assignment", "hard"),
                        ("backup_only_assignment", "preference"),
                    ],
                )
        for month in (10, 11, 12, 1):
            with self.subTest(month=month):
                findings = [
                    finding
                    for finding in interview_findings_for_academic_year(2026).findings
                    if finding.shift.date.month == month
                ]
                self.assertCountEqual(
                    [(finding.code, finding.severity) for finding in findings],
                    [
                        ("not_available_assignment", "hard"),
                        ("backup_only_assignment", "preference"),
                    ],
                )

    def test_vacations_cover_the_documented_horizon_with_spaced_week_and_day_examples(self):
        for physician in Physician.objects.filter(
            display_name__in=DOCUMENTED_DEMO_FACULTY + DOCUMENTED_DEMO_COMMUNITY_PRECEPTORS
        ):
            with self.subTest(physician=physician.display_name):
                vacations = physician.vacations.order_by("start_date")
                self.assertGreater(vacations.count(), 0)
                self.assertTrue(
                    all(
                        reset_demo.DEMO_HORIZON_START
                        <= vacation.start_date
                        <= vacation.end_date
                        <= reset_demo.DEMO_HORIZON_END
                        for vacation in vacations
                    )
                )
                full_weeks = physician.vacations.filter(
                    period_of_day=Vacation.PeriodOfDay.WHOLE_DAY
                ).exclude(start_date=models.F("end_date"))
                self.assertGreaterEqual(full_weeks.count(), 2)
                self.assertTrue(all(vacation.start_date.weekday() == 0 for vacation in full_weeks))
                self.assertTrue(
                    all(
                        (vacation.end_date - vacation.start_date).days == 4
                        for vacation in full_weeks
                    )
                )
                self.assertTrue(
                    all(
                        following.start_date > preceding.end_date
                        for preceding, following in pairwise(full_weeks)
                    )
                )
                self.assertEqual(
                    set(
                        physician.vacations.filter(start_date=models.F("end_date")).values_list(
                            "period_of_day", flat=True
                        )
                    ),
                    {
                        Vacation.PeriodOfDay.WHOLE_DAY,
                        Vacation.PeriodOfDay.AM,
                        Vacation.PeriodOfDay.PM,
                    },
                )
                covered_workday_half_days = sum(
                    2 if vacation.period_of_day == Vacation.PeriodOfDay.WHOLE_DAY else 1
                    for vacation in vacations
                    for offset in range((vacation.end_date - vacation.start_date).days + 1)
                    if (vacation.start_date + timedelta(days=offset)).weekday() < 5
                )
                # The demo Vacation sample calls for approximately four workweeks, not a
                # particular number of rows or whole-week records. Fifteen to twenty-five
                # workdays keeps the sample useful without treating density as a seed contract.
                self.assert_approximately_four_workweeks(covered_workday_half_days)

    def test_academic_events_and_rounding_sources_cover_the_seeded_demo_shapes(self):
        events = AcademicEvent.objects.filter(monthly_rule_source_id__isnull=True)
        self.assertEqual(
            set(events.values_list("period_of_day", flat=True)),
            {
                AcademicEvent.PeriodOfDay.WHOLE_DAY,
                AcademicEvent.PeriodOfDay.AM,
                AcademicEvent.PeriodOfDay.PM,
            },
        )
        self.assertTrue(
            all(
                date(2026, 8, 1) <= event.start_date <= event.end_date <= date(2026, 9, 30)
                for event in events
            )
        )
        self.assertEqual({event.start_date.month for event in events}, {8, 9})
        self.assertTrue(any(event.start_date == event.end_date for event in events))
        self.assertTrue(any(event.start_date < event.end_date for event in events))
        self.assertTrue(any(event.participants.count() > 1 for event in events))
        individual_monthly = (
            AcademicEvent.objects.filter(
                definition__scheduling_mode="monthly_rules", all_available_faculty=False
            )
            .filter(start_date__range=(date(2026, 8, 1), date(2026, 9, 30)))
            .order_by("start_date")
        )
        collective_monthly = (
            AcademicEvent.objects.filter(
                definition__scheduling_mode="monthly_rules", all_available_faculty=True
            )
            .filter(start_date__range=(date(2026, 8, 1), date(2026, 9, 30)))
            .order_by("start_date")
        )
        self.assertEqual({event.start_date.month for event in individual_monthly}, {8, 9})
        self.assertTrue(
            all(
                not event.all_available_faculty
                and event.assignment_is_default
                and tuple(
                    event.participants.order_by("display_name").values_list(
                        "display_name", flat=True
                    )
                )
                == ("Alex Kim", "Avery Patel")
                for event in individual_monthly
            )
        )
        self.assertEqual({event.start_date.month for event in collective_monthly}, {8, 9})
        self.assertTrue(
            all(
                event.all_available_faculty
                and event.assignment_is_default
                and not event.participants.exists()
                for event in collective_monthly
            )
        )
        self.assertEqual(
            set(
                AcademicEvent.objects.filter(monthly_rule_source_id__isnull=True).values_list(
                    "expected_quarter", flat=True
                )
            ),
            {"Q1", "Q2", "Q3", "Q4"},
        )
        self.assertTrue(
            all(
                event.expected_quarter == event.definition.expected_quarter
                for event in AcademicEvent.objects.filter(
                    monthly_rule_source_id__isnull=True
                ).select_related("definition")
            )
        )

    def test_academic_event_catalog_has_deterministic_quarter_defaults_and_unscheduled_choices(
        self,
    ):
        definitions = AcademicEventDefinition.objects.filter(is_active=True)
        yearly_definitions = definitions.filter(scheduling_mode="yearly")
        self.assertEqual(yearly_definitions.count(), 40)
        # TEXT-CONTRACT: demo-department-meeting-name. The demo catalogue requires
        # the exact Department Meeting label, capitalization, and spacing for its
        # meeting example. This protects schedules/management/commands/reset_demo.py.
        meeting = definitions.get(base_name="Department Meeting")
        self.assertEqual(meeting.default_duration_days, 1)
        self.assertTrue(all(definition.typically_scheduled for definition in yearly_definitions))
        monthly_definitions = definitions.filter(scheduling_mode="monthly_rules")
        self.assertTrue(monthly_definitions.filter(all_available_faculty=False).exists())
        self.assertTrue(monthly_definitions.filter(all_available_faculty=True).exists())
        quarterly_definition_counts = {
            quarter: yearly_definitions.filter(expected_quarter=quarter).count()
            for quarter in ("Q1", "Q2", "Q3", "Q4")
        }
        self.assertTrue(all(quarterly_definition_counts.values()))
        # The demo catalogue needs a roughly even visible spread, not near-identical quarters.
        # A difference of six still leaves each of the forty annual choices usefully distributed.
        self.assert_roughly_even_quarter_distribution(quarterly_definition_counts.values())
        self.assertEqual(
            {
                quarter: definitions.filter(
                    expected_quarter=quarter, default_required_participant_count=2
                ).count()
                for quarter in ("Q1", "Q2", "Q3", "Q4")
            },
            {"Q1": 2, "Q2": 2, "Q3": 2, "Q4": 2},
        )
        self.assertTrue(
            {1, 2, 3}.issubset(
                set(yearly_definitions.values_list("default_duration_days", flat=True))
            )
        )
        self.assertTrue(
            {
                AcademicEvent.PeriodOfDay.WHOLE_DAY,
                AcademicEvent.PeriodOfDay.AM,
                AcademicEvent.PeriodOfDay.PM,
            }.issubset(set(yearly_definitions.values_list("default_period_of_day", flat=True)))
        )
        self.assertTrue(yearly_definitions.exclude(annual_events__is_active=True).exists())

        expected_weeks = [date(2026, 8, 3) + timedelta(weeks=index) for index in range(9)]
        self.assertEqual(
            list(NormalRounder.objects.order_by("week").values_list("week", flat=True)),
            expected_weeks,
        )
        rounder_names = list(
            NormalRounder.objects.order_by("week").values_list("physician__display_name", flat=True)
        )
        self.assertTrue(set(rounder_names).issubset(DOCUMENTED_DEMO_FACULTY))
        self.assert_weekly_rotation(rounder_names)

        self.assertEqual(
            list(
                CasualRounderRule.objects.order_by("weekday").values_list(
                    "physician__display_name",
                    "weekday",
                    "ordinals",
                    "effective_start",
                    "effective_end",
                )
            ),
            [
                ("Fictional Cedar", 0, [1, 2, 3, 4, 5], date(2026, 8, 1), date(2026, 9, 30)),
                ("Fictional Harbor", 2, [1, 3, 5], date(2026, 8, 1), None),
                ("Fictional Summit", 4, [2, 4], date(2026, 8, 1), date(2026, 9, 30)),
            ],
        )
        self.assertEqual(
            [rule.physician.display_name for rule in casual_rounder_rules_on(date(2026, 8, 31))],
            ["Fictional Cedar"],
        )
        self.assertEqual(
            [rule.physician.display_name for rule in casual_rounder_rules_on(date(2026, 9, 30))],
            ["Fictional Harbor"],
        )
        for period_of_day in ("AM", "PM"):
            with self.subTest(period_of_day=period_of_day):
                effect = rounding_effect_on(date(2026, 8, 31), period_of_day)
                self.assertEqual(effect.availability, "backup_only")
                self.assertEqual(
                    [physician.display_name for physician in effect.casual_rounder_physicians],
                    ["Fictional Cedar"],
                )

    def test_seeded_casual_rules_are_covered_by_casual_rounder_roles(self):
        for rule in CasualRounderRule.objects.select_related("physician"):
            with self.subTest(physician=rule.physician.display_name, weekday=rule.weekday):
                covering_roles = EffectiveRole.objects.filter(
                    physician=rule.physician,
                    role=EffectiveRole.Role.CASUAL_ROUNDER,
                    effective_start__lte=rule.effective_start,
                )
                if rule.effective_end is None:
                    covering_roles = covering_roles.filter(effective_end__isnull=True)
                else:
                    covering_roles = covering_roles.filter(
                        models.Q(effective_end__isnull=True)
                        | models.Q(effective_end__gte=rule.effective_end)
                    )
                self.assertTrue(covering_roles.exists())

    def test_seeded_academic_and_rounding_histories_retain_scheduler_and_reason(self):
        scheduler = get_user_model().objects.get(username=reset_demo.DEMO_USERNAME)
        academic_event_changes = AcademicEventChange.objects.all()
        self.assertEqual(
            set(academic_event_changes.values_list("event_id_at_recording", flat=True)),
            set(AcademicEvent.objects.values_list("pk", flat=True)),
        )
        yearly_changes = academic_event_changes.filter(event__monthly_rule_source_id__isnull=True)
        self.assertTrue(
            all(
                change.changed_by == scheduler
                and change.reason == reset_demo.DEMO_REASON
                and change.action == AcademicEventChange.Action.CREATED
                and change.event_id_at_recording == change.event_id
                and change.previous_value is None
                and change.recorded_value is not None
                for change in yearly_changes
            )
        )
        self.assertTrue(
            all(
                change.action == AcademicEventChange.Action.CREATED
                and change.event_id_at_recording == change.event_id
                and change.previous_value is None
                and change.recorded_value is not None
                for change in academic_event_changes.filter(
                    event__monthly_rule_source_id__isnull=False
                )
            )
        )

        rounding_changes = RoundingScheduleChange.objects.all()
        for subject, source_ids in (
            (
                RoundingScheduleChange.Subject.NORMAL_ROUNDER,
                NormalRounder.objects.values_list("pk", flat=True),
            ),
            (
                RoundingScheduleChange.Subject.CASUAL_ROUNDER_RULE,
                CasualRounderRule.objects.values_list("pk", flat=True),
            ),
        ):
            with self.subTest(history_subject=subject):
                changes = rounding_changes.filter(subject=subject)
                self.assertEqual(
                    set(changes.values_list("source_id_at_recording", flat=True)), set(source_ids)
                )
                self.assertTrue(
                    all(
                        change.changed_by == scheduler
                        and change.reason == reset_demo.DEMO_REASON
                        and change.action == RoundingScheduleChange.Action.CREATED
                        and change.previous_value is None
                        and change.recorded_value is not None
                        and change.source_id_at_recording
                        == (change.normal_rounder_id or change.casual_rounder_rule_id)
                        for change in changes
                    )
                )

    def test_august_is_complete_and_september_has_each_draft_gap_shape_without_later_sessions(self):
        august_sessions = PreceptorSession.objects.filter(
            date__range=(date(2026, 8, 3), date(2026, 8, 28))
        )
        self.assertEqual(august_sessions.count(), 40)
        self.assertTrue(
            all(
                session.assignments.filter(responsibility="primary").count() == 2
                for session in august_sessions
            )
        )
        september_sessions = PreceptorSession.objects.filter(
            date__range=(date(2026, 8, 31), date(2026, 9, 30))
        )
        primary_counts = {
            session.assignments.filter(responsibility="primary").count()
            for session in september_sessions
        }
        self.assertEqual(primary_counts, {0, 1, 2})
        self.assertFalse(PreceptorSession.objects.filter(date__gt=date(2026, 9, 30)).exists())
        self.assertFalse(
            PreceptorAssignment.objects.filter(session__date__gt=date(2026, 9, 30)).exists()
        )

    def test_scheduler_credentials_can_sign_in_and_a_fresh_reseed_has_the_same_data_shape(self):
        first_shape = self._shape()
        # TEXT-CONTRACT: demo-scheduler-credentials. README.md documents these exact
        # username/password values, including lowercase spelling and hyphens.
        self.assertIsNotNone(authenticate(username="demo-scheduler", password="demo-password"))

        call_command("flush", interactive=False, verbosity=0)
        reset_demo.seed_demo_data()

        self.assertEqual(self._shape(), first_shape)

    def _shape(self):
        return {
            "physicians": list(
                Physician.objects.order_by("pk").values_list("display_name", flat=True)
            ),
            "roles": list(
                EffectiveRole.objects.order_by("pk").values_list(
                    "physician__display_name",
                    "role",
                    "effective_start",
                    "effective_end",
                    "faculty_equivalent",
                    "faculty_leadership",
                )
            ),
            "templates": list(
                FacultyAvailabilityTemplate.objects.order_by("pk").values_list(
                    "physician__display_name", "effective_start", "effective_end", "slots"
                )
            ),
            "vacations": list(
                Vacation.objects.order_by("pk").values_list(
                    "physician__display_name", "start_date", "end_date", "period_of_day"
                )
            ),
            "academic_events": [
                (
                    event.title,
                    event.start_date,
                    event.end_date,
                    event.period_of_day,
                    tuple(event.participants.order_by("pk").values_list("display_name", flat=True)),
                )
                for event in AcademicEvent.objects.order_by("pk")
            ],
            "academic_event_definitions": list(
                AcademicEventDefinition.objects.order_by("pk").values_list(
                    "base_name",
                    "start_academic_year",
                    "end_academic_year",
                    "expected_quarter",
                    "default_duration_days",
                    "default_period_of_day",
                    "default_required_participant_count",
                    "is_active",
                )
            ),
            "normal_rounders": list(
                NormalRounder.objects.order_by("pk").values_list("week", "physician__display_name")
            ),
            "casual_rounder_rules": list(
                CasualRounderRule.objects.order_by("pk").values_list(
                    "physician__display_name",
                    "weekday",
                    "ordinals",
                    "effective_start",
                    "effective_end",
                )
            ),
            "community_preceptor_rules": list(
                CommunityPreceptorRule.objects.order_by("pk").values_list(
                    "physician__display_name",
                    "weekday",
                    "ordinals",
                    "periods",
                    "effective_start",
                    "effective_end",
                )
            ),
            "interview_shifts": list(
                InterviewShift.objects.order_by("pk").values_list(
                    "date", "physician__display_name", "note", "is_active"
                )
            ),
            "assignments": list(
                PreceptorAssignment.objects.order_by("pk").values_list(
                    "session__date",
                    "session__period_of_day",
                    "physician__display_name",
                    "responsibility",
                    "primary_position",
                )
            ),
            "physician_history": list(
                PhysicianChange.objects.order_by("pk").values_list(
                    "subject", "reason", "previous_value", "recorded_value"
                )
            ),
            "vacation_history": list(
                VacationChange.objects.order_by("pk").values_list(
                    "action", "previous_value", "recorded_value", "previous_note", "recorded_note"
                )
            ),
            "academic_event_history": list(
                AcademicEventChange.objects.order_by("pk").values_list(
                    "event_id_at_recording",
                    "action",
                    "reason",
                    "previous_value",
                    "recorded_value",
                    "changed_by__username",
                )
            ),
            "rounding_history": list(
                RoundingScheduleChange.objects.order_by("pk").values_list(
                    "source_id_at_recording",
                    "subject",
                    "action",
                    "physician__display_name",
                    "reason",
                    "previous_value",
                    "recorded_value",
                    "changed_by__username",
                )
            ),
            "assignment_history": list(
                PreceptorAssignmentChange.objects.order_by("pk").values_list(
                    "action", "reason", "previous_value", "recorded_value"
                )
            ),
            "community_preceptor_history": list(
                CommunityPreceptorScheduleChange.objects.order_by("pk").values_list(
                    "source_id_at_recording",
                    "action",
                    "physician__display_name",
                    "reason",
                    "previous_value",
                    "recorded_value",
                    "changed_by__username",
                )
            ),
            "interview_history": list(
                InterviewShiftChange.objects.order_by("pk").values_list(
                    "shift_id_at_recording",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "changed_by__username",
                )
            ),
        }
