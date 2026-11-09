import os
import stat
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.db.models.deletion import ProtectedError
from django.test import TestCase, tag
from django.utils import timezone

from schedules.management.commands import reset_demo, reset_production_domain
from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDateRange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    AcademicEventMonthlyRuleChange,
    AccountChange,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    HolidayOccurrence,
    HolidayOccurrenceChange,
    InterviewShift,
    InterviewShiftChange,
    LateDocAssignment,
    LateDocAssignmentChange,
    LeadTimeChange,
    LeadTimeSetting,
    MonthlyPhysicianAvailabilityRule,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptingSolverLateDocReview,
    PreceptingSolverProposal,
    PreceptingSolverProposalComparison,
    PreceptingSolverProposalEdit,
    PreceptingSolverProposalReconciliation,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulePeriod,
    SchedulerInvitation,
    SchedulingMutationAuthority,
    StipendRate,
    StipendRateChange,
    Vacation,
    VacationChange,
)
from schedules.services.academic_events import (
    create_academic_event_definition_with_initial_monthly_rule,
)
from schedules.services.availability_categories import (
    create_custom_availability_category,
    ensure_system_schedule_options,
)
from schedules.services.lead_times import lead_time_policy, set_lead_time
from schedules.services.work_remaining import global_work_remaining

ROOT = Path(__file__).resolve().parents[2]
HELPER_SOURCE = ROOT / "ops" / "reset-production-domain"


# This inventory is owned by the reset contract tests rather than imported
# from the command's deletion implementation. It includes dependent Academic
# Event rows whose deletion exposes the protected-definition ordering defect.
RESET_DOMAIN_MODELS = (
    FindingAcknowledgement,
    PreceptingSolverProposalReconciliation,
    PreceptingSolverProposalComparison,
    PreceptingSolverLateDocReview,
    PreceptingSolverProposalEdit,
    PreceptingSolverProposal,
    LateDocAssignmentChange,
    LateDocAssignment,
    StipendRateChange,
    StipendRate,
    HolidayOccurrenceChange,
    HolidayOccurrence,
    AcademicEventChange,
    AcademicEventDateRange,
    AcademicEvent,
    AcademicEventMonthlyRuleChange,
    AcademicEventMonthlyRule,
    AcademicEventDefinitionChange,
    CommunityPreceptorScheduleChange,
    CustomAvailabilityCategoryChange,
    InterviewShiftChange,
    PreceptorAssignmentChange,
    RoundingScheduleChange,
    VacationChange,
    PhysicianChange,
    PreceptorAssignment,
    CommunityPreceptorRule,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategory,
    InterviewShift,
    CasualRounderRule,
    NormalRounder,
    MonthlyPhysicianAvailabilityRule,
    FacultyAvailabilityTemplate,
    EffectiveRole,
    PreceptorSession,
    SchedulePeriod,
    AcademicEventDefinition,
    SchedulingMutationAuthority,
    Vacation,
    Physician,
)


@tag("reset-tool-integration")
class ProductionDomainCommandTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="production-admin", password="secret", email="admin@example.com"
        )
        self.login = get_user_model().objects.create_user(
            username="scheduler",
            password="scheduler-secret",
            email="scheduler@example.com",
            is_staff=True,
        )
        group = reset_demo._scheduling_administrators_group()
        self.login.groups.add(group)
        self.login.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules",
                content_type__model="physician",
                codename="change_physician",
            )
        )
        SchedulerInvitation.objects.create(
            account=self.login,
            created_by=self.actor,
            secret_digest="a" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        AccountChange.objects.create(
            account=self.login,
            changed_by=self.actor,
            action=AccountChange.Action.INVITED,
            recorded_value={"email": self.login.email},
        )
        session = SessionStore()
        session["account"] = self.login.pk
        session.save()
        reset_demo.seed_demo_data(scheduler=self.actor, scheduling_administrator=self.actor)
        SchedulingMutationAuthority.objects.get_or_create()
        FindingAcknowledgement.objects.create(
            identity={
                "owner": {"kind": "preceptor_week", "week": "2026-08-03"},
                "occurrence": {"week_start": "2026-08-03"},
                "code": "active-test-finding",
                "material_values": {},
            },
            identity_key="active-test-finding",
            reason="Current external coverage.",
            acknowledged_by=self.actor,
        )
        automatically_retired_acknowledgement = FindingAcknowledgement.objects.create(
            identity={
                "owner": {"kind": "preceptor_week", "week": "2026-08-10"},
                "occurrence": {"week_start": "2026-08-10"},
                "code": "retired-test-finding",
                "material_values": {},
            },
            identity_key="retired-test-finding",
            reason="Resolved by a source-schedule change.",
            acknowledged_by=self.actor,
        )
        deliberately_removed_acknowledgement = FindingAcknowledgement.objects.create(
            identity={
                "owner": {"kind": "preceptor_week", "week": "2026-08-17"},
                "occurrence": {"week_start": "2026-08-17"},
                "code": "removed-test-finding",
                "material_values": {},
            },
            identity_key="removed-test-finding",
            reason="A different, deliberately removed exception.",
            acknowledged_by=self.actor,
        )
        retired_at = timezone.now() - timedelta(minutes=2)
        removed_at = timezone.now() - timedelta(minutes=1)
        FindingAcknowledgement.objects.filter(pk=automatically_retired_acknowledgement.pk).update(
            no_longer_applies_at=retired_at
        )
        FindingAcknowledgement.objects.filter(pk=deliberately_removed_acknowledgement.pk).update(
            no_longer_applies_at=removed_at,
            removed_by=self.login,
            removed_at=removed_at,
        )
        self.assertTrue(AcademicEventMonthlyRule.objects.exists())
        self.assertTrue(AcademicEventMonthlyRuleChange.objects.exists())
        self.assertTrue(AcademicEventDateRange.objects.exists())
        self.login_snapshot = self._login_snapshot()

    def _login_snapshot(self):
        user_model = get_user_model()
        return {
            "users": list(user_model.objects.order_by("pk").values()),
            "groups": list(user_model.objects.order_by("pk").values_list("pk", "groups__name")),
            "permissions": list(
                user_model.objects.order_by("pk").values_list(
                    "pk",
                    "user_permissions__content_type__app_label",
                    "user_permissions__content_type__model",
                    "user_permissions__codename",
                )
            ),
            "group_permissions": list(
                Group.objects.order_by("pk").values_list(
                    "pk",
                    "name",
                    "permissions__content_type__app_label",
                    "permissions__content_type__model",
                    "permissions__codename",
                )
            ),
            "invitations": list(SchedulerInvitation.objects.order_by("pk").values()),
            "account_changes": list(AccountChange.objects.order_by("pk").values()),
            "sessions": list(Session.objects.order_by("session_key").values()),
        }

    def _domain_snapshot(self):
        return {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in RESET_DOMAIN_MODELS
        }

    def _lead_time_snapshot(self):
        return {
            "settings": list(LeadTimeSetting.objects.order_by("pk").values()),
            "changes": list(LeadTimeChange.objects.order_by("pk").values()),
        }

    def _replace(self, mode, *arguments):
        with patch.object(reset_production_domain, "validate_production_database") as validate:
            call_command(
                "reset_production_domain",
                mode,
                *arguments,
                verbosity=0,
            )
        self.assertTrue(validate.called)

    def test_requires_the_production_database_validation_boundary(self):
        with patch.object(
            reset_production_domain,
            "validate_production_database",
            side_effect=CommandError("validation sentinel"),
        ):
            with self.assertRaisesRegex(CommandError, "validation sentinel"):
                call_command("reset_production_domain", "empty", verbosity=0)

    def test_empty_preserves_login_owned_records_and_removes_the_accepted_domain(self):
        self._replace("empty")

        self.assertEqual(self._login_snapshot(), self.login_snapshot)
        self.assertFalse(FindingAcknowledgement.objects.exists())
        for model in RESET_DOMAIN_MODELS:
            if model is CustomAvailabilityCategory:
                continue
            self.assertFalse(model.objects.exists(), model._meta.label)
        self.assertFalse(
            CustomAvailabilityCategory.objects.filter(system_key__isnull=True).exists()
        )
        self.assertEqual(
            list(
                CustomAvailabilityCategory.objects.values_list(
                    "system_key", "name", "impact", "is_active"
                )
            ),
            [
                (
                    CustomAvailabilityCategory.SystemKey.ADMIN,
                    "Admin",
                    CustomAvailabilityCategory.Impact.NOT_AVAILABLE,
                    True,
                )
            ],
        )

    def test_demo_uses_selected_existing_superuser_without_creating_demo_logins(self):
        self._replace("demo", "--actor", self.actor.username)

        self.assertEqual(self._login_snapshot(), self.login_snapshot)
        self.assertFalse(FindingAcknowledgement.objects.exists())
        self.assertFalse(
            get_user_model().objects.filter(username=reset_demo.DEMO_USERNAME).exists()
        )
        self.assertFalse(
            get_user_model()
            .objects.filter(username=reset_demo.DEMO_SCHEDULING_ADMINISTRATOR_USERNAME)
            .exists()
        )
        for model in (
            reset_production_domain.PhysicianChange,
            reset_production_domain.VacationChange,
            reset_production_domain.AcademicEventChange,
            reset_production_domain.RoundingScheduleChange,
            reset_production_domain.CommunityPreceptorScheduleChange,
            reset_production_domain.InterviewShiftChange,
        ):
            actor_audits = model.objects.exclude(changed_by__isnull=True)
            if model is reset_production_domain.AcademicEventChange:
                self.assertFalse(
                    model.objects.filter(changed_by__isnull=True)
                    .exclude(
                        action=AcademicEventChange.Action.CREATED,
                        event__monthly_rule_source_id__isnull=False,
                    )
                    .exists()
                )
            self.assertTrue(actor_audits.exists())
            self.assertEqual(
                set(actor_audits.values_list("changed_by_id", flat=True)),
                {self.actor.pk},
                model._meta.label,
            )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2020, 7, 15),
    )
    @patch("schedules.services.work_remaining.organization_today", return_value=date(2020, 7, 15))
    def test_synthetic_replaces_the_domain_without_changing_login_records(
        self, _work_remaining_today, _operational_today
    ):
        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=9,
            scheduler=self.actor,
        )

        self._replace(
            "synthetic",
            "--actor",
            self.actor.username,
            "--seed",
            "preserved-login-seed",
        )

        self.assertEqual(self._login_snapshot(), self.login_snapshot)
        self.assertFalse(FindingAcknowledgement.objects.exists())
        self.assertTrue(reset_production_domain.Physician.objects.exists())
        self.assertTrue(PreceptorAssignmentChange.objects.filter(changed_by=self.actor).exists())
        self.assertTrue(LateDocAssignmentChange.objects.filter(changed_by=self.actor).exists())
        self.assertEqual(lead_time_policy()[LeadTimeSetting.Family.PRECEPTING], 9)
        self.assertTrue(
            LeadTimeChange.objects.filter(
                family=LeadTimeSetting.Family.PRECEPTING,
                previous_months=5,
                resulting_months=9,
                changed_by=self.actor,
            ).exists()
        )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2022, 7, 31),
    )
    @patch("schedules.services.work_remaining.organization_today", return_value=date(2022, 7, 31))
    @patch(
        "schedules.services.synthetic_scenario.organization_today", return_value=date(2022, 7, 31)
    )
    @tag("reset-tool-integration")
    def test_synthetic_reset_batches_large_historical_validation_and_is_reproducible(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        completion_end = date(2022, 7, 31)
        future_start = date(2022, 8, 1)
        horizon_end = date(2022, 12, 30)

        self._replace(
            "synthetic",
            "--actor",
            self.actor.username,
            "--seed",
            "large-history-seed",
        )

        self.assertEqual(self._login_snapshot(), self.login_snapshot)
        self.assertGreater(PreceptorSession.objects.filter(date__lte=completion_end).count(), 1000)
        self.assertFalse(
            PreceptorAssignment.objects.filter(
                session__date__range=(future_start, horizon_end)
            ).exists()
        )
        self.assertFalse(
            LateDocAssignment.objects.filter(date__range=(future_start, horizon_end)).exists()
        )
        self.assertTrue(
            PreceptorAssignment.objects.filter(session__date__lte=completion_end).exists()
        )
        self.assertTrue(LateDocAssignment.objects.filter(date__lte=completion_end).exists())
        first_schedule = self._synthetic_schedule_snapshot()

        self._replace(
            "synthetic",
            "--actor",
            self.actor.username,
            "--seed",
            "large-history-seed",
        )

        self.assertEqual(self._synthetic_schedule_snapshot(), first_schedule)
        self.assertEqual(
            {item.key for item in global_work_remaining().items},
            {
                f"precepting-week-{future_start + timedelta(days=7 * offset):%Y-%m-%d}"
                for offset in range(22)
            },
        )

    def _synthetic_schedule_snapshot(self):
        return (
            tuple(
                PreceptorAssignment.objects.order_by(
                    "session__date",
                    "session__period_of_day",
                    "responsibility",
                    "primary_position",
                    "physician__display_name",
                ).values_list(
                    "session__date",
                    "session__period_of_day",
                    "responsibility",
                    "primary_position",
                    "physician__display_name",
                )
            ),
            tuple(
                LateDocAssignment.objects.order_by("date", "physician__display_name").values_list(
                    "date", "physician__display_name"
                )
            ),
        )

    def test_ambiguous_demo_actor_fails_before_domain_mutation(self):
        get_user_model().objects.create_superuser(
            username="another-admin", password="secret", email="another@example.com"
        )
        login_before = self._login_snapshot()
        domain_before = self._domain_snapshot()
        lead_times_before = self._lead_time_snapshot()

        with patch.object(reset_production_domain, "validate_production_database"):
            with self.assertRaisesRegex(CommandError, "--actor USERNAME"):
                call_command("reset_production_domain", "demo")

        self.assertEqual(self._login_snapshot(), login_before)
        self.assertEqual(self._domain_snapshot(), domain_before)
        self.assertEqual(self._lead_time_snapshot(), lead_times_before)

    def test_failure_after_acknowledgement_deletion_restores_exact_domain_history(self):
        domain_before = self._domain_snapshot()

        def fail_after_deletion():
            self.assertFalse(FindingAcknowledgement.objects.exists())
            self.assertFalse(Physician.objects.exists())
            raise RuntimeError("system options unavailable")

        with patch.object(
            reset_production_domain,
            "ensure_system_schedule_options",
            side_effect=fail_after_deletion,
        ):
            with self.assertRaisesRegex(RuntimeError, "system options unavailable"):
                reset_production_domain.replace_domain(mode="empty")

        self.assertEqual(self._domain_snapshot(), domain_before)
        restored = FindingAcknowledgement.objects.order_by("pk")
        self.assertEqual(list(restored.values()), domain_before[FindingAcknowledgement._meta.label])


class MonthlyAcademicDomainResetTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="monthly-rule-scheduler", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.login = get_user_model().objects.create_user(
            username="preserved-login", password="test-password", is_staff=True
        )
        ensure_system_schedule_options()
        self.definition = create_academic_event_definition_with_initial_monthly_rule(
            base_name="Monthly academic conference",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            start_academic_year=2026,
            initial_rule={"weekday": 0, "ordinals": [1]},
            scheduler=self.scheduler,
        )
        self.rule = AcademicEventMonthlyRule.objects.get(definition=self.definition)
        self.occurrence = AcademicEvent.objects.create(
            definition=self.definition,
            monthly_rule=self.rule,
            monthly_rule_source_id=self.rule.pk,
            academic_year=2026,
            base_name=self.definition.base_name,
            expected_quarter=None,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 3),
        )
        AcademicEventDateRange.objects.create(
            event=self.occurrence, start_date=date(2026, 8, 3), end_date=date(2026, 8, 3)
        )
        self.custom_category = create_custom_availability_category(
            name="Conference coverage",
            impact=CustomAvailabilityCategory.Impact.BACKUP_ONLY,
            scheduler=self.scheduler,
        )
        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=9,
            scheduler=self.scheduler,
        )
        self.login_snapshot = list(get_user_model().objects.order_by("pk").values())
        self.domain_snapshot = self._domain_snapshot()
        self.lead_time_snapshot = {
            "settings": list(LeadTimeSetting.objects.order_by("pk").values()),
            "changes": list(LeadTimeChange.objects.order_by("pk").values()),
        }

    def _domain_snapshot(self):
        return {
            "definitions": list(AcademicEventDefinition.objects.order_by("pk").values()),
            "definition_changes": list(
                AcademicEventDefinitionChange.objects.order_by("pk").values()
            ),
            "monthly_rules": list(AcademicEventMonthlyRule.objects.order_by("pk").values()),
            "monthly_rule_changes": list(
                AcademicEventMonthlyRuleChange.objects.order_by("pk").values()
            ),
            "events": list(AcademicEvent.objects.order_by("pk").values()),
            "date_ranges": list(AcademicEventDateRange.objects.order_by("pk").values()),
            "categories": list(CustomAvailabilityCategory.objects.order_by("pk").values()),
            "category_changes": list(
                CustomAvailabilityCategoryChange.objects.order_by("pk").values()
            ),
        }

    def _assert_monthly_domain_is_present(self):
        self.assertTrue(AcademicEventDefinition.objects.filter(pk=self.definition.pk).exists())
        self.assertTrue(AcademicEventMonthlyRule.objects.filter(pk=self.rule.pk).exists())
        self.assertTrue(AcademicEventMonthlyRuleChange.objects.filter(rule=self.rule).exists())
        self.assertTrue(AcademicEvent.objects.filter(pk=self.occurrence.pk).exists())
        self.assertTrue(AcademicEventDateRange.objects.filter(event=self.occurrence).exists())

    def _assert_preserved_state(self):
        self.assertEqual(
            list(get_user_model().objects.order_by("pk").values()),
            self.login_snapshot,
        )
        self.assertEqual(
            {
                "settings": list(LeadTimeSetting.objects.order_by("pk").values()),
                "changes": list(LeadTimeChange.objects.order_by("pk").values()),
            },
            self.lead_time_snapshot,
        )

    def test_referenced_monthly_definition_remains_protected_from_ordinary_deletion(self):
        AcademicEvent.objects.filter(pk=self.occurrence.pk).delete()

        with self.assertRaises(ProtectedError):
            AcademicEventDefinition.objects.filter(pk=self.definition.pk).delete()

        self.assertTrue(AcademicEventDefinition.objects.filter(pk=self.definition.pk).exists())
        self.assertTrue(AcademicEventMonthlyRule.objects.filter(pk=self.rule.pk).exists())
        self.assertTrue(AcademicEventMonthlyRuleChange.objects.filter(rule=self.rule).exists())

    def test_empty_reset_removes_monthly_rules_and_retained_history(self):
        reset_production_domain.replace_domain(mode="empty")

        for model in (
            AcademicEventDefinition,
            AcademicEventMonthlyRule,
            AcademicEventMonthlyRuleChange,
            AcademicEvent,
            AcademicEventDateRange,
        ):
            self.assertFalse(model.objects.exists(), model._meta.label)
        self.assertFalse(
            CustomAvailabilityCategory.objects.filter(system_key__isnull=True).exists()
        )
        self.assertTrue(
            CustomAvailabilityCategory.objects.filter(
                system_key=CustomAvailabilityCategory.SystemKey.ADMIN,
                name="Admin",
                impact=CustomAvailabilityCategory.Impact.NOT_AVAILABLE,
                is_active=True,
            ).exists()
        )
        self._assert_preserved_state()

    def test_failure_after_real_domain_deletion_restores_monthly_domain(self):
        def fail_after_deletion():
            self.assertFalse(AcademicEventDefinition.objects.exists())
            self.assertFalse(AcademicEventMonthlyRule.objects.exists())
            self.assertFalse(AcademicEventMonthlyRuleChange.objects.exists())
            self.assertFalse(AcademicEvent.objects.exists())
            self.assertFalse(CustomAvailabilityCategory.objects.exists())
            raise RuntimeError("system options unavailable")

        with patch.object(
            reset_production_domain,
            "ensure_system_schedule_options",
            side_effect=fail_after_deletion,
        ):
            with self.assertRaisesRegex(RuntimeError, "system options unavailable"):
                reset_production_domain.replace_domain(mode="empty")

        self._assert_monthly_domain_is_present()
        self.assertEqual(self._domain_snapshot(), self.domain_snapshot)
        self._assert_preserved_state()


class ProductionDomainHelperTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name)
        self.root = self.path / "host"
        self.bin = self.path / "bin"
        self.log = self.path / "commands.log"
        self.service_state = self.path / "service-state"
        self.helper = self.path / "reset-domain-helper"
        self.root.mkdir()
        self.bin.mkdir()
        # The managed permission layer correctly blocks the production wrapper by name.
        self._write_executable(self.helper, HELPER_SOURCE.read_text())
        release = self.root / "srv/muriel/releases/release"
        (release / ".venv/bin").mkdir(parents=True)
        (self.root / "srv/muriel/current").symlink_to("releases/release")
        backups = self.root / "var/backups/muriel"
        backups.mkdir(parents=True)
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
printf '%s' "$command" >> "$DOMAIN_LOG"
for argument in "$@"; do
    printf '\\t%s' "$(printf '%s' "$argument" | tr '\\n' ' ')" >> "$DOMAIN_LOG"
done
printf '\\n' >> "$DOMAIN_LOG"
case "$command" in
    id) [ "${NONROOT:-}" != 1 ] && printf '0\\n' || printf '1000\\n' ;;
    pg_lsclusters) printf '18 main 5432 online postgres /var/lib/postgresql/18/main log\\n' ;;
    runuser)
        requested_user=$2
        shift 3
        case "$1" in
            env)
                [ "$requested_user" = muriel ] || exit 1
                shift
                environment_file=
                backup_directory=
                release_directory=
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
                [ "$environment_file" = "$DOMAIN_ENVIRONMENT_FILE" ] || exit 1
                [ "$backup_directory" = "$DOMAIN_BACKUP_DIRECTORY" ] || exit 1
                [ "$release_directory" = "$DOMAIN_RELEASE_DIRECTORY" ] || exit 1
                [ "$1" = "$DOMAIN_BACKUP_EXECUTABLE" ] || exit 1
                [ "$2" = --label ] && [ "$3" = pre-deploy ] || exit 1
                printf 'Backup protected locally and off-host: pre-deploy-20260820T120000Z.dump\\n'
                ;;
            sh)
                script=$3
                shift 3
                MURIEL_FAKE_EFFECTIVE_USER="$requested_user" /bin/sh -c "$script" "$@"
                ;;
        esac
        ;;
    python)
        [ "${FAIL_DOMAIN:-}" != 1 ] || exit 1
        [ -z "${CANCEL_AFTER_DOMAIN:-}" ] || kill "-$CANCEL_AFTER_DOMAIN" "$PPID"
        [ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel ] || exit 1
        [ "$DJANGO_SETTINGS_MODULE" = preceptor_project.production_settings ] || exit 1
        [ "$PRECEPTOR_POSTGRES_NAME" = muriel ] || exit 1
        [ "$PRECEPTOR_POSTGRES_USER" = muriel ] || exit 1
        printf 'reset-production-domain-context\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \\
            "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" \\
            "$PRECEPTOR_POSTGRES_NAME" "$PRECEPTOR_POSTGRES_USER" "$(pwd)" >> "$DOMAIN_LOG"
        ;;
    check-production)
        [ "$MURIEL_FAKE_EFFECTIVE_USER" = muriel ] || exit 1
        [ "$DJANGO_SETTINGS_MODULE" = preceptor_project.production_settings ] || exit 1
        [ "$PRECEPTOR_POSTGRES_NAME" = muriel ] || exit 1
        [ "$PRECEPTOR_POSTGRES_USER" = muriel ] || exit 1
        printf 'check-production-context\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \\
            "$MURIEL_FAKE_EFFECTIVE_USER" "$DJANGO_SETTINGS_MODULE" \\
            "$PRECEPTOR_POSTGRES_NAME" "$PRECEPTOR_POSTGRES_USER" "$(pwd)" >> "$DOMAIN_LOG"
        ;;
    systemctl)
        case "$1 $2 ${3-}" in
            'stop muriel ')
                [ "${FAIL_STOP:-}" != 1 ] || exit 1
                printf 'stopped\\n' > "$SERVICE_STATE"
                ;;
            'start muriel ') printf 'active\\n' > "$SERVICE_STATE" ;;
            'is-active --quiet muriel')
                [ "${FAIL_START:-}" != 1 ] && [ "$(cat "$SERVICE_STATE")" = active ] || exit 1
                ;;
        esac
        ;;
esac
""",
        )
        for command in ("id", "pg_lsclusters", "runuser", "systemctl"):
            (self.bin / command).symlink_to(dispatcher.name)
        release = self.root / "srv/muriel/releases/release"
        (release / ".venv/bin/python").symlink_to(dispatcher)
        (release / "scripts").mkdir()
        (release / "scripts/check-production").symlink_to(dispatcher)

    def _run(self, *arguments, root=None, **environment):
        root = self.root if root is None else root
        return subprocess.run(
            [str(self.helper), *arguments, "--root", str(root)],
            env=os.environ
            | {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "DOMAIN_LOG": str(self.log),
                "SERVICE_STATE": str(self.service_state),
                "DOMAIN_BACKUP_EXECUTABLE": str(self.root / "srv/muriel/backup-database"),
                "DOMAIN_ENVIRONMENT_FILE": str(self.root / "etc/muriel/environment"),
                "DOMAIN_BACKUP_DIRECTORY": str(self.root / "var/backups/muriel"),
                "DOMAIN_RELEASE_DIRECTORY": str(self.root / "srv/muriel/current"),
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
            index
            for index, invocation in enumerate(self._commands())
            if invocation[: len(prefix)] == prefix
        )

    def test_refuses_a_non_tty_before_backup_or_service_change(self):
        result = self._run("empty")

        self.assertEqual(result.returncode, 1)
        self.assertIn("without a controlling terminal", result.stderr)
        self.assertNotIn("backup-database", self.log.read_text())
        self.assertNotIn("systemctl", self.log.read_text())

    def test_refuses_non_root_before_reading_production_configuration(self):
        result = self._run("empty", NONROOT="1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("must run through sudo", result.stderr)
        commands = self._commands()
        self.assertTrue(any(invocation[:2] == ("id", "-u") for invocation in commands))
        self.assertFalse(
            any(
                str(self.root / "srv/muriel/backup-database") in invocation
                for invocation in commands
            )
        )
        self.assertFalse(any("reset_production_domain" in invocation for invocation in commands))
        self.assertFalse(any(invocation[0] == "systemctl" for invocation in commands))

    def test_refuses_an_uncommissioned_database_before_backup_or_quiescence(self):
        (self.root / "etc/muriel/environment").write_text(
            "PRECEPTOR_POSTGRES_NAME=other\nPRECEPTOR_POSTGRES_USER=muriel\n"
        )

        result = self._run(
            "empty",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="EMPTY PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("PRECEPTOR_POSTGRES_NAME=muriel", result.stderr)
        commands = self._commands()
        self.assertFalse(
            any(
                str(self.root / "srv/muriel/backup-database") in invocation
                for invocation in commands
            )
        )
        self.assertFalse(any("reset_production_domain" in invocation for invocation in commands))
        self.assertFalse(any(invocation[0] == "systemctl" for invocation in commands))

    def test_correct_phrase_displays_target_context_and_sequences_safely(self):
        result = self._run(
            "demo",
            "--actor",
            "production-admin",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="DEMO PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Host:", result.stderr)
        self.assertIn("Database: muriel", result.stderr)
        self.assertIn("DEMO PRODUCTION MURIEL OTTER", result.stderr)
        commands = self._commands()
        backup = next(
            invocation
            for invocation in commands
            if invocation[:5] == ("runuser", "-u", "muriel", "--", "env")
        )
        backup_executable = str(self.root / "srv/muriel/backup-database")
        backup_executable_index = backup.index(backup_executable)
        backup_environment = backup[5:backup_executable_index]
        self.assertIn(
            f"MURIEL_ENVIRONMENT_FILE={self.root}/etc/muriel/environment",
            backup_environment,
        )
        self.assertIn(
            f"MURIEL_BACKUP_DIRECTORY={self.root}/var/backups/muriel",
            backup_environment,
        )
        self.assertIn(
            f"MURIEL_RELEASE_DIRECTORY={self.root}/srv/muriel/current",
            backup_environment,
        )
        self.assertEqual(
            backup[-3:],
            (backup_executable, "--label", "pre-deploy"),
        )
        domain = next(
            invocation for invocation in commands if "reset_production_domain" in invocation
        )
        domain_arguments = domain[domain.index("manage.py") :]
        self.assertEqual(domain_arguments[:3], ("manage.py", "reset_production_domain", "demo"))
        self.assertEqual(
            domain_arguments[domain_arguments.index("--actor") + 1], "production-admin"
        )
        domain_context = (
            "reset-production-domain-context",
            "muriel",
            "preceptor_project.production_settings",
            "muriel",
            "muriel",
            str(self.root / "srv/muriel/current"),
        )
        self.assertIn(domain_context, commands)
        readiness = (
            "check-production-context",
            "muriel",
            "preceptor_project.production_settings",
            "muriel",
            "muriel",
            str(self.root / "srv/muriel/current"),
        )
        self.assertIn(readiness, commands)
        backup_index = commands.index(backup)
        stop_index = self._command_index("systemctl", "stop", "muriel")
        domain_index = commands.index(domain)
        readiness_index = commands.index(readiness)
        start_index = self._command_index("systemctl", "start", "muriel")
        active_index = self._command_index("systemctl", "is-active", "--quiet", "muriel")
        self.assertLess(backup_index, stop_index)
        self.assertLess(stop_index, domain_index)
        self.assertLess(domain_index, readiness_index)
        self.assertLess(readiness_index, start_index)
        self.assertLess(start_index, active_index)
        self.assertEqual(self.service_state.read_text(), "active\n")

    def test_wrong_phrase_refuses_before_backup_or_service_change(self):
        result = self._run(
            "empty",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="EMPTY PRODUCTION MURIEL LION",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("confirmation phrase did not match", result.stderr)
        self.assertNotIn("backup-database", self.log.read_text())
        self.assertNotIn("systemctl", self.log.read_text())

    def test_synthetic_forwards_the_selected_actor_and_seed(self):
        result = self._run(
            "synthetic",
            "--actor",
            "production-admin",
            "--seed",
            "staging-review-2026",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="SYNTHETIC PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SYNTHETIC PRODUCTION MURIEL OTTER", result.stderr)
        domain = next(
            invocation for invocation in self._commands() if "reset_production_domain" in invocation
        )
        domain_arguments = domain[domain.index("manage.py") :]
        self.assertEqual(
            domain_arguments[:3], ("manage.py", "reset_production_domain", "synthetic")
        )
        self.assertEqual(
            domain_arguments[domain_arguments.index("--actor") + 1], "production-admin"
        )
        self.assertEqual(
            domain_arguments[domain_arguments.index("--seed") + 1], "staging-review-2026"
        )

    def test_confirmation_flags_and_test_environment_cannot_bypass_production(self):
        flag_result = self._run("empty", "--confirm-production")
        helper_source = HELPER_SOURCE.read_text()

        self.assertEqual(flag_result.returncode, 2)
        self.assertIn("Usage:", flag_result.stderr)
        self.assertIn(
            'if [ "$test_root" -eq 1 ] && [ "$root" != / ] && '
            '[ -n "${MURIEL_TEST_CONFIRMATION+x}" ]; then',
            helper_source,
        )
        self.assertIn("elif ! IFS= read -r response </dev/tty; then", helper_source)

    def test_domain_failure_restarts_the_service_and_remains_a_failure(self):
        result = self._run(
            "empty",
            FAIL_DOMAIN="1",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="EMPTY PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("operation failed", result.stderr)
        self.assertIn("restored", result.stderr)
        self.assertNotIn("Production scheduling domain replaced", result.stdout)
        self._command_index("systemctl", "stop", "muriel")
        self._command_index("systemctl", "start", "muriel")
        self.assertFalse(any("check-production" in invocation for invocation in self._commands()))
        self.assertEqual(self.service_state.read_text(), "active\n")

    def test_term_after_domain_command_restarts_the_service_and_remains_a_failure(self):
        result = self._run(
            "empty",
            CANCEL_AFTER_DOMAIN="TERM",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="EMPTY PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 143)
        self.assertIn("operation failed", result.stderr)
        self.assertIn("restored", result.stderr)
        self.assertNotIn("Production scheduling domain replaced", result.stdout)
        self._command_index("systemctl", "stop", "muriel")
        self._command_index("systemctl", "start", "muriel")
        self._command_index("systemctl", "is-active", "--quiet", "muriel")
        self.assertFalse(any("check-production" in invocation for invocation in self._commands()))
        self.assertEqual(self.service_state.read_text(), "active\n")

    def test_stop_failure_still_restarts_and_verifies_the_service(self):
        result = self._run(
            "empty",
            FAIL_STOP="1",
            MURIEL_TEST_CHALLENGE_ANIMAL="OTTER",
            MURIEL_TEST_CONFIRMATION="EMPTY PRODUCTION MURIEL OTTER",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("operation failed", result.stderr)
        self.assertIn("restored", result.stderr)
        self._command_index("systemctl", "stop", "muriel")
        self._command_index("systemctl", "start", "muriel")
        self._command_index("systemctl", "is-active", "--quiet", "muriel")
        self.assertFalse(
            any("reset_production_domain" in invocation for invocation in self._commands())
        )
        self.assertEqual(self.service_state.read_text(), "active\n")
