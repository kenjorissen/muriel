from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase

from schedules.management.commands.reset_demo import seed_demo_data
from schedules.management.commands.reset_production_domain import replace_domain
from schedules.models import LeadTimeChange, LeadTimeSetting
from schedules.services.lead_times import (
    lead_time_changes,
    lead_time_months,
    lead_time_policy,
    set_lead_time,
)

# docs/design.md section 7.2: default months by work family.
DOCUMENTED_LEAD_TIME_DEFAULTS = {
    LeadTimeSetting.Family.HOLIDAYS: 18,
    LeadTimeSetting.Family.ACADEMIC_EVENTS: 6,
    LeadTimeSetting.Family.PRECEPTING: 5,
    LeadTimeSetting.Family.NORMAL_ROUNDING: 5,
    LeadTimeSetting.Family.INTERVIEWS: 0,
}


class LeadTimePolicyTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_migration_defaults_provide_a_complete_deterministic_read_only_policy(self):
        settings_before = list(
            LeadTimeSetting.objects.order_by("pk").values_list("family", "months")
        )

        self.assertEqual(lead_time_policy(), DOCUMENTED_LEAD_TIME_DEFAULTS)
        self.assertEqual(
            lead_time_months(LeadTimeSetting.Family.ACADEMIC_EVENTS),
            DOCUMENTED_LEAD_TIME_DEFAULTS[LeadTimeSetting.Family.ACADEMIC_EVENTS],
        )
        self.assertEqual(
            list(LeadTimeSetting.objects.order_by("pk").values_list("family", "months")),
            settings_before,
        )

    def test_changes_are_independent_audited_and_repeating_a_value_is_a_no_op(self):
        changed = set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=9,
            scheduler=self.administrator,
            note="  Additional preparation time.  ",
        )

        self.assertEqual(changed.months, 9)
        self.assertEqual(lead_time_months(LeadTimeSetting.Family.PRECEPTING), 9)
        self.assertEqual(
            lead_time_months(LeadTimeSetting.Family.NORMAL_ROUNDING),
            DOCUMENTED_LEAD_TIME_DEFAULTS[LeadTimeSetting.Family.NORMAL_ROUNDING],
        )
        change = lead_time_changes(LeadTimeSetting.Family.PRECEPTING).get()
        self.assertEqual(change.setting, changed)
        self.assertEqual(change.family, LeadTimeSetting.Family.PRECEPTING)
        self.assertEqual((change.previous_months, change.resulting_months), (5, 9))
        self.assertEqual(change.changed_by, self.administrator)
        self.assertIsNotNone(change.changed_at)
        self.assertEqual(change.note, "Additional preparation time.")

        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=9,
            scheduler=self.administrator,
            note="A no-op note is not retained",
        )

        self.assertEqual(LeadTimeChange.objects.count(), 1)

    def test_invalid_values_and_unauthorized_changes_leave_the_policy_unchanged(self):
        initial_policy = lead_time_policy()
        for family, months in (
            ("unknown", 1),
            (None, 1),
            (LeadTimeSetting.Family.HOLIDAYS, None),
            (LeadTimeSetting.Family.HOLIDAYS, -1),
            (LeadTimeSetting.Family.HOLIDAYS, "5"),
            (LeadTimeSetting.Family.HOLIDAYS, 1.5),
            (LeadTimeSetting.Family.HOLIDAYS, True),
        ):
            with self.subTest(family=family, months=months):
                with self.assertRaises(ValidationError):
                    set_lead_time(
                        family=family,
                        months=months,
                        scheduler=self.administrator,
                    )
                self.assertEqual(lead_time_policy(), initial_policy)
                self.assertFalse(LeadTimeChange.objects.exists())

        with self.assertRaises(PermissionDenied):
            set_lead_time(
                family=LeadTimeSetting.Family.HOLIDAYS,
                months=12,
                scheduler=self.scheduler,
            )
        self.assertEqual(lead_time_policy(), initial_policy)

    def test_failed_audit_record_rolls_back_the_setting_change(self):
        with patch(
            "schedules.services.lead_times.LeadTimeChange.objects.create",
            side_effect=RuntimeError("audit storage failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit storage failed"):
                set_lead_time(
                    family=LeadTimeSetting.Family.INTERVIEWS,
                    months=4,
                    scheduler=self.administrator,
                )

        self.assertEqual(
            lead_time_months(LeadTimeSetting.Family.INTERVIEWS),
            DOCUMENTED_LEAD_TIME_DEFAULTS[LeadTimeSetting.Family.INTERVIEWS],
        )
        self.assertFalse(LeadTimeChange.objects.exists())

    def test_current_settings_and_history_are_retained_and_history_is_immutable(self):
        setting = LeadTimeSetting.objects.get(family=LeadTimeSetting.Family.HOLIDAYS)
        with self.assertRaisesRegex(ValidationError, "cannot be deleted"):
            setting.delete()

        set_lead_time(
            family=LeadTimeSetting.Family.HOLIDAYS,
            months=12,
            scheduler=self.administrator,
        )
        change = LeadTimeChange.objects.get()
        change.note = "changed outside the service"
        with self.assertRaisesRegex(ValidationError, "immutable"):
            change.save()
        with self.assertRaisesRegex(ValidationError, "cannot be deleted"):
            change.delete()

    def test_demo_seed_restores_defaults_after_an_empty_database_reset(self):
        call_command("flush", interactive=False, verbosity=0)

        seed_demo_data()

        self.assertEqual(lead_time_policy(), DOCUMENTED_LEAD_TIME_DEFAULTS)

    def test_production_empty_reset_preserves_the_complete_current_policy(self):
        set_lead_time(
            family=LeadTimeSetting.Family.HOLIDAYS,
            months=12,
            scheduler=self.administrator,
        )

        replace_domain(mode="empty")

        self.assertEqual(
            lead_time_policy(),
            DOCUMENTED_LEAD_TIME_DEFAULTS | {LeadTimeSetting.Family.HOLIDAYS: 12},
        )
        self.assertEqual(
            list(
                LeadTimeChange.objects.values_list("family", "previous_months", "resulting_months")
            ),
            [(LeadTimeSetting.Family.HOLIDAYS, 18, 12)],
        )
