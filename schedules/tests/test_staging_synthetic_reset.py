from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings, tag

from schedules.management.commands import reset_staging_synthetic
from schedules.models import (
    LateDocAssignment,
    LateDocAssignmentChange,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
)


@override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
class StagingSyntheticResetTests(TestCase):
    def test_confirmed_reset_uses_a_non_login_audit_actor_and_retains_other_identities(self):
        retained_user = get_user_model().objects.create_user(username="retained-user")
        retained_user_values = get_user_model().objects.filter(pk=retained_user.pk).values().get()
        summary = ({"physicians": 1}, 1, date(2020, 7, 31))

        with (
            patch.dict(
                settings.DATABASES["default"],
                {"NAME": "muriel_staging", "USER": "muriel_staging"},
            ),
            patch.object(
                reset_staging_synthetic, "replace_domain", return_value=summary
            ) as replace,
        ):
            call_command("reset_staging_synthetic", "--confirm", "--seed", "fast-staging")

        actor = get_user_model().objects.get(pk=replace.call_args.kwargs["actor"].pk)

        self.assertTrue(actor.is_active)
        self.assertTrue(actor.is_superuser)
        self.assertFalse(actor.is_staff)
        self.assertFalse(actor.has_usable_password())
        self.assertIsNone(authenticate(username=actor.username, password="not-a-password"))
        self.assertEqual(replace.call_args.kwargs["mode"], "synthetic")
        self.assertEqual(replace.call_args.kwargs["seed"], "fast-staging")
        self.assertEqual(
            get_user_model().objects.filter(pk=retained_user.pk).values().get(),
            retained_user_values,
        )

    @tag("reset-tool-integration")
    def test_fresh_staging_database_loads_with_a_non_login_audit_actor(self):
        with patch.dict(
            settings.DATABASES["default"],
            {"NAME": "muriel_staging", "USER": "muriel_staging"},
        ):
            call_command(
                "reset_staging_synthetic",
                "--confirm",
                "--seed",
                "fresh-staging",
            )

        actor = get_user_model().objects.get(is_superuser=True, is_staff=False)
        self.assertTrue(actor.is_active)
        self.assertTrue(actor.is_superuser)
        self.assertFalse(actor.is_staff)
        self.assertFalse(actor.has_usable_password())
        self.assertIsNone(authenticate(username=actor.username, password="not-a-password"))
        self.assertTrue(PreceptorAssignment.objects.exists())
        self.assertTrue(LateDocAssignment.objects.exists())
        self.assertTrue(PreceptorAssignmentChange.objects.filter(changed_by=actor).exists())
        self.assertTrue(LateDocAssignmentChange.objects.filter(changed_by=actor).exists())
        self.assertTrue(PhysicianChange.objects.filter(changed_by=actor).exists())
