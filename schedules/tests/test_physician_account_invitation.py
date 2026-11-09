from datetime import date
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from schedules.models import (
    AccountChange,
    EffectiveRole,
    Physician,
    PhysicianChange,
    SchedulerInvitation,
)
from schedules.services.accounts import create_physician_with_invitation
from schedules.tests.test_scheduler_invitations import manual_setup_link


class PhysicianAccountInvitationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.manager = user_model.objects.create_user(
            username="manager", password="test-password", email="manager@example.com"
        )
        self.manager.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.scheduler = user_model.objects.create_user(
            username="scheduler", password="test-password", email="scheduler@example.com"
        )

    def test_physician_only_creation_remains_unchanged(self):
        self.client.force_login(self.manager)

        response = self.client.post(reverse("physician-create"), {"display_name": "Physician only"})

        physician = Physician.objects.get(display_name="Physician only")
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        self.assertIsNone(physician.user_id)
        self.assertFalse(SchedulerInvitation.objects.exists())

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="manual")
    def test_combined_creation_links_an_unprivileged_account_and_prepares_delivery(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("physician-create"),
            {
                "display_name": "Invited physician",
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-01",
                "create_login_and_invitation": "on",
                "official_email": " Physician@Example.COM ",
                "reason": "Initial entry",
            },
        )

        physician = Physician.objects.get(display_name="Invited physician")
        account = physician.user
        invitation = SchedulerInvitation.objects.get(account=account)
        self.assertEqual(account.username, "physician@example.com")
        self.assertEqual(account.email, "physician@example.com")
        self.assertFalse(account.is_active)
        self.assertFalse(account.has_usable_password())
        self.assertFalse(account.is_staff)
        self.assertFalse(account.is_superuser)
        self.assertFalse(account.groups.exists())
        self.assertFalse(invitation.scheduling_administrator)
        self.assertEqual(invitation.status, SchedulerInvitation.Status.PENDING)
        self.assertEqual(invitation.delivery_status, SchedulerInvitation.DeliveryStatus.MANUAL)
        self.assertTrue(physician.roles.filter(role=EffectiveRole.Role.FACULTY).exists())
        self.assertTrue(
            PhysicianChange.objects.filter(
                physician=physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
            ).exists()
        )
        self.assertTrue(
            AccountChange.objects.filter(
                account=account, action=AccountChange.Action.INVITED
            ).exists()
        )
        setup_link, _ = manual_setup_link(self, response)
        recipient_page = self.client.get(urlsplit(setup_link).path)
        self.assertEqual(recipient_page.context["form"].user, account)

    def test_service_denies_an_ordinary_scheduler_without_writing_records(self):
        with self.assertRaises(PermissionDenied):
            create_physician_with_invitation(
                display_name="Denied", email="denied@example.com", scheduler=self.scheduler
            )

        self.assertFalse(Physician.objects.filter(display_name="Denied").exists())
        self.assertFalse(get_user_model().objects.filter(username="denied@example.com").exists())
        self.assertFalse(SchedulerInvitation.objects.exists())

    def test_duplicate_email_or_username_is_shown_on_the_combined_form_without_a_physician(self):
        user_model = get_user_model()
        user_model.objects.create_user(
            username="existing", password="test-password", email="existing@example.com"
        )
        user_model.objects.create_user(
            username="username@example.com", password="test-password", email="other@example.com"
        )
        self.client.force_login(self.manager)

        for name, email in (
            ("Duplicate email", "Existing@example.com"),
            ("Duplicate username", "username@example.com"),
        ):
            with self.subTest(email=email):
                response = self.client.post(
                    reverse("physician-create"),
                    {
                        "display_name": name,
                        "create_login_and_invitation": "on",
                        "official_email": email,
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertIn("official_email", response.context["form"].errors)
                errors = response.context["form"].errors.as_data()["official_email"]
                self.assertTrue(errors)
                self.assertContains(response, 'name="official_email"')
                self.assertFalse(Physician.objects.filter(display_name=name).exists())

    def test_invalid_initial_role_rolls_back_the_physician_account_link_and_invitation(self):
        with self.assertRaises(ValidationError):
            create_physician_with_invitation(
                display_name="Invalid role",
                email="invalid-role@example.com",
                role="not-a-role",
                effective_start=date(2025, 1, 1),
                scheduler=self.manager,
            )

        self.assertFalse(Physician.objects.filter(display_name="Invalid role").exists())
        self.assertFalse(
            get_user_model().objects.filter(username="invalid-role@example.com").exists()
        )
        self.assertFalse(SchedulerInvitation.objects.exists())
        self.assertFalse(PhysicianChange.objects.filter(reason="").exists())

    @patch(
        "schedules.services.accounts.create_scheduler_invitation",
        side_effect=ValidationError({"username": "Already in use."}),
    )
    def test_account_failure_rolls_back_prior_physician_records(self, create_invitation):
        with self.assertRaises(ValidationError):
            create_physician_with_invitation(
                display_name="Rolled back",
                email="rollback@example.com",
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 1),
                scheduler=self.manager,
            )

        self.assertTrue(create_invitation.called)
        self.assertFalse(Physician.objects.filter(display_name="Rolled back").exists())
        self.assertFalse(PhysicianChange.objects.filter(reason="").exists())
        self.assertFalse(SchedulerInvitation.objects.exists())

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="smtp")
    @patch("schedules.services.accounts.EmailMultiAlternatives.send", return_value=0)
    def test_delivery_failure_keeps_the_combined_invitation_available_to_resend(self, send):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("physician-create"),
            {
                "display_name": "Delivery failure",
                "create_login_and_invitation": "on",
                "official_email": "delivery@example.com",
            },
        )

        self.assertRedirects(response, reverse("account-workspace"), fetch_redirect_response=False)
        physician = Physician.objects.get(display_name="Delivery failure")
        invitation = SchedulerInvitation.objects.get(account=physician.user)
        self.assertEqual(invitation.status, SchedulerInvitation.Status.PENDING)
        self.assertEqual(invitation.delivery_status, SchedulerInvitation.DeliveryStatus.FAILED)
        resend = self.client.post(reverse("account-invitation-resend", args=(physician.user_id,)))
        self.assertRedirects(resend, reverse("account-workspace"), fetch_redirect_response=False)
        self.assertEqual(SchedulerInvitation.objects.filter(account=physician.user).count(), 2)
        self.assertTrue(send.called)
