from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from schedules.management.commands import reset_synthetic


class SyntheticResetGuardTests(SimpleTestCase):
    @override_settings(
        DEBUG=False,
    )
    def test_refuses_without_confirmation_before_inspecting_or_replacing_data(self):
        with (
            patch.object(reset_synthetic, "replace_domain") as replace_domain,
            self.assertRaisesRegex(CommandError, "--confirm"),
        ):
            call_command("reset_synthetic")

        replace_domain.assert_not_called()
