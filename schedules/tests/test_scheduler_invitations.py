import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from schedules.models import AccountChange, PhysicianChange, SchedulerInvitation
from schedules.services.accounts import create_scheduler_invitation, deliver_scheduler_invitation
from schedules.services.physicians import create_physician
from schedules.tests.test_password_lifecycle import assert_invalid_link_feedback
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


def manual_setup_link(test_case, response):
    """Return the rendered manual-delivery link after checking its recipient-facing surface."""
    expected_link = response.context["delivery"].setup_link
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    controls = [
        element
        for element in parser.elements
        if element.tag == "input"
        and element.attributes.get("type") == "text"
        and element.attributes.get("value") == expected_link
        and "readonly" in element.attributes
    ]
    test_case.assertEqual(len(controls), 1)
    control = controls[0]
    accessible_name = control.attributes.get("aria-label", "")
    if not accessible_name:
        accessible_name = " ".join(
            label.text()
            for label in parser.elements
            if label.tag == "label" and label.attributes.get("for") == control.attributes.get("id")
        )
    test_case.assertTrue(
        any(word in accessible_name.casefold() for word in ("setup", "activate", "invitation"))
    )
    delivery_containers = [
        element
        for element in control.ancestors()
        if element.tag == "section"
        and any(
            descendant.tag == "pre" and expected_link in descendant.text()
            for descendant in element.descendants()
        )
    ]
    test_case.assertTrue(delivery_containers)
    recipient_messages = [
        element
        for element in delivery_containers[0].descendants()
        if element.tag == "pre" and expected_link in element.text()
    ]
    test_case.assertEqual(len(recipient_messages), 1)
    return expected_link, recipient_messages[0].text()


def account_invitation_history_snapshot(account):
    """Capture all retained state that a rejected invitation operation must preserve."""
    account.refresh_from_db()
    return {
        "account": {
            field.attname: getattr(account, field.attname)
            for field in account._meta.concrete_fields
        },
        "group_ids": tuple(account.groups.order_by("pk").values_list("pk", flat=True)),
        "invitations": tuple(
            SchedulerInvitation.objects.filter(account=account).order_by("pk").values()
        ),
        "account_changes": tuple(
            AccountChange.objects.filter(account=account).order_by("pk").values()
        ),
    }


def assert_rendered_delivery_failure(test_case, response, *, token):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    failure_messages = [
        element
        for element in parser.elements
        if element.tag == "p"
        and any(
            ancestor.tag == "section"
            and "surface-tone--attention" in ancestor.attributes.get("class", "").split()
            for ancestor in element.ancestors()
        )
        and any(term in element.text().casefold() for term in ("delivery", "smtp", "email", "mail"))
        and any(
            term in element.text().casefold() for term in ("failed", "failure", "unable", "problem")
        )
    ]
    test_case.assertEqual(len(failure_messages), 1)
    content = response.content.decode()
    test_case.assertNotIn(reverse("scheduler-invitation-accept", args=(token,)), content)
    test_case.assertNotIn(token, content)


class SchedulerInvitationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username="administrator", password="test-password", email="administrator@example.com"
        )
        self.scheduling_administrator = User.objects.create_user(
            username="manager", password="test-password", email="manager@example.com"
        )
        self.scheduling_administrator.groups.add(
            Group.objects.get(name="Scheduling Administrators")
        )
        self.physician = create_physician(
            display_name="Unlinked physician", scheduler=self.superuser
        )

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="manual")
    def test_manual_invitation_creates_a_disabled_unprivileged_account_and_explicit_link(self):
        self.client.force_login(self.scheduling_administrator)

        response = self.client.post(
            reverse("account-invite"),
            {
                "username": "new-scheduler",
                "email": "NEW@example.com",
                "physician": self.physician.pk,
                "scheduling_administrator": "on",
            },
        )

        account = get_user_model().objects.get(username="new-scheduler")
        invitation = SchedulerInvitation.objects.get(account=account)
        self.physician.refresh_from_db()
        self.assertFalse(account.is_active)
        self.assertFalse(account.has_usable_password())
        self.assertFalse(account.is_staff)
        self.assertFalse(account.is_superuser)
        self.assertFalse(account.groups.exists())
        self.assertEqual(self.physician.user, account)
        self.assertEqual(invitation.status, SchedulerInvitation.Status.PENDING)
        self.assertEqual(invitation.delivery_status, SchedulerInvitation.DeliveryStatus.MANUAL)
        setup_link, _ = manual_setup_link(self, response)
        recipient_page = self.client.get(urlsplit(setup_link).path)
        self.assertEqual(recipient_page.context["form"].user, account)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            PhysicianChange.objects.filter(
                physician=self.physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
            ).exists()
        )
        retained_history = str(AccountChange.objects.filter(account=account).values())
        self.assertNotIn(setup_link, retained_history)
        self.assertNotIn(urlsplit(setup_link).path.rsplit("/", 2)[-2], retained_history)

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="manual")
    def test_acceptance_validates_password_activates_once_and_applies_requested_authority(self):
        self.client.force_login(self.scheduling_administrator)
        response = self.client.post(
            reverse("account-invite"),
            {
                "username": "new-scheduler",
                "email": "new@example.com",
                "scheduling_administrator": "on",
            },
        )
        setup_link, _ = manual_setup_link(self, response)
        token = urlsplit(setup_link).path.rsplit("/", 2)[-2]
        self.client.logout()

        before_rejected_password = account_invitation_history_snapshot(
            get_user_model().objects.get(username="new-scheduler")
        )
        invalid_password = self.client.post(
            reverse("scheduler-invitation-accept", args=(token,)),
            {"new_password1": "short", "new_password2": "short"},
        )
        password_errors = invalid_password.context["form"].errors.as_data()["new_password2"]
        self.assertIn("password_too_short", [error.code for error in password_errors])
        pending_account = get_user_model().objects.get(username="new-scheduler")
        self.assertFalse(pending_account.is_active)
        self.assertFalse(pending_account.has_usable_password())
        self.assertFalse(pending_account.groups.exists())
        self.assertEqual(
            account_invitation_history_snapshot(pending_account), before_rejected_password
        )
        accepted = self.client.post(
            reverse("scheduler-invitation-accept", args=(token,)),
            {"new_password1": "valid-password", "new_password2": "valid-password"},
        )
        self.assertRedirects(accepted, reverse("scheduler-invitation-complete"))

        account = get_user_model().objects.get(username="new-scheduler")
        invitation = SchedulerInvitation.objects.get(account=account)
        self.assertTrue(account.is_active)
        self.assertTrue(account.check_password("valid-password"))
        self.assertTrue(account.groups.filter(name="Scheduling Administrators").exists())
        self.assertEqual(invitation.status, SchedulerInvitation.Status.ACCEPTED)
        self.client.post(
            reverse("sign-in"), {"username": account.username, "password": "valid-password"}
        )
        self.assertEqual(self.client.get(reverse("account-workspace")).status_code, 200)
        self.client.logout()
        before_replay = account_invitation_history_snapshot(account)
        replay = self.client.post(
            reverse("scheduler-invitation-accept", args=(token,)),
            {"new_password1": "replayed-password", "new_password2": "replayed-password"},
        )
        assert_invalid_link_feedback(self, replay, recovery_url=reverse("sign-in"))
        account.refresh_from_db()
        invitation.refresh_from_db()
        self.assertEqual(account_invitation_history_snapshot(account), before_replay)
        self.assertTrue(account.check_password("valid-password"))
        self.assertTrue(account.groups.filter(name="Scheduling Administrators").exists())
        self.assertEqual(invitation.status, SchedulerInvitation.Status.ACCEPTED)
        self.assertEqual(
            AccountChange.objects.filter(
                account=account, action=AccountChange.Action.ACCEPTED
            ).count(),
            1,
        )

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="smtp")
    @patch(
        "schedules.services.accounts.EmailMultiAlternatives.send", side_effect=OSError("offline")
    )
    def test_smtp_failure_keeps_one_recoverable_pending_account_without_exposing_link(self, send):
        self.client.force_login(self.scheduling_administrator)

        delivered_tokens = []

        def capture_delivery(*, invitation, token, request):
            delivered_tokens.append(token)
            return deliver_scheduler_invitation(
                invitation=invitation,
                token=token,
                request=request,
            )

        with patch(
            "schedules.views._invitations.deliver_scheduler_invitation",
            side_effect=capture_delivery,
        ):
            response = self.client.post(
                reverse("account-invite"),
                {"username": "new-scheduler", "email": "new@example.com"},
            )

        self.assertRedirects(response, reverse("account-workspace"), fetch_redirect_response=False)
        account = get_user_model().objects.get(username="new-scheduler")
        invitation = SchedulerInvitation.objects.get(account=account)
        self.assertFalse(account.is_active)
        self.assertEqual(invitation.delivery_status, SchedulerInvitation.DeliveryStatus.FAILED)
        self.assertEqual(SchedulerInvitation.objects.filter(account=account).count(), 1)
        self.assertTrue(send.called)
        self.assertEqual(len(delivered_tokens), 1)
        workspace = self.client.get(reverse("account-workspace"))
        assert_rendered_delivery_failure(self, workspace, token=delivered_tokens[0])

    @override_settings(SCHEDULER_INVITATION_DELIVERY_MODE="manual")
    def test_resend_supersedes_expired_or_revoked_link_and_revoke_blocks_acceptance(self):
        self.client.force_login(self.scheduling_administrator)
        initial = self.client.post(
            reverse("account-invite"), {"username": "new-scheduler", "email": "new@example.com"}
        )
        first_setup_link, _ = manual_setup_link(self, initial)
        first_token = urlsplit(first_setup_link).path.rsplit("/", 2)[-2]
        account = get_user_model().objects.get(username="new-scheduler")
        first = SchedulerInvitation.objects.get(account=account)
        first.expires_at = timezone.now() - timedelta(seconds=1)
        first.save(update_fields=("expires_at",))
        self.client.logout()
        before_expired_acceptance = account_invitation_history_snapshot(account)
        expired = self.client.post(
            reverse("scheduler-invitation-accept", args=(first_token,)),
            {"new_password1": "expired-password", "new_password2": "expired-password"},
        )
        assert_invalid_link_feedback(self, expired, recovery_url=reverse("sign-in"))
        account.refresh_from_db()
        self.assertEqual(account_invitation_history_snapshot(account), before_expired_acceptance)
        self.assertFalse(account.is_active)
        self.assertFalse(account.has_usable_password())
        self.client.force_login(self.scheduling_administrator)

        resent = self.client.post(reverse("account-invitation-resend", args=(account.pk,)))
        second_setup_link, _ = manual_setup_link(self, resent)
        second_token = urlsplit(second_setup_link).path.rsplit("/", 2)[-2]
        first.refresh_from_db()
        self.assertEqual(first.status, SchedulerInvitation.Status.SUPERSEDED)
        self.assertNotEqual(first_token, second_token)
        self.client.post(reverse("account-invitation-revoke", args=(account.pk,)))
        second = SchedulerInvitation.objects.filter(account=account).first()
        self.assertEqual(second.status, SchedulerInvitation.Status.REVOKED)
        self.client.logout()
        before_revoked_acceptance = account_invitation_history_snapshot(account)
        revoked = self.client.post(
            reverse("scheduler-invitation-accept", args=(second_token,)),
            {"new_password1": "revoked-password", "new_password2": "revoked-password"},
        )
        assert_invalid_link_feedback(self, revoked, recovery_url=reverse("sign-in"))
        account.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(account_invitation_history_snapshot(account), before_revoked_acceptance)
        self.assertFalse(account.is_active)
        self.assertFalse(account.has_usable_password())
        self.assertFalse(account.groups.exists())
        self.assertEqual(second.status, SchedulerInvitation.Status.REVOKED)
        self.assertEqual(
            AccountChange.objects.filter(
                account=account, action=AccountChange.Action.ACCEPTED
            ).count(),
            0,
        )

    def test_ordinary_schedulers_cannot_create_or_operate_invitations(self):
        user = get_user_model().objects.create_user(
            username="scheduler", password="test-password", email="scheduler@example.com"
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("account-invite")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("account-invite"), {"username": "new", "email": "new@example.com"}
            ).status_code,
            403,
        )

    def test_stale_physician_selection_rolls_back_the_account_and_invitation(self):
        linked_user = get_user_model().objects.create_user(
            username="linked", password="test-password", email="linked@example.com"
        )
        self.physician.user = linked_user
        self.physician.save(update_fields=("user",))

        with self.assertRaisesRegex(ValidationError, "already linked"):
            create_scheduler_invitation(
                username="new-scheduler",
                email="new@example.com",
                scheduling_administrator=False,
                physician=self.physician,
                scheduler=self.superuser,
            )

        self.assertFalse(get_user_model().objects.filter(username="new-scheduler").exists())
        self.assertFalse(SchedulerInvitation.objects.exists())

    def test_database_rejects_case_variant_account_emails(self):
        User = get_user_model()
        User.objects.create_user(
            username="existing", password="test-password", email="Existing@example.com"
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="case-variant", password="test-password", email="existing@example.com"
                )

    def test_invitation_maps_a_concurrent_case_variant_email_collision_to_email_validation(self):
        get_user_model().objects.create_user(
            username="existing", password="test-password", email="Existing@example.com"
        )

        with patch("schedules.services.accounts._email_in_use", side_effect=(False, True)):
            with self.assertRaises(ValidationError) as raised:
                create_scheduler_invitation(
                    username="new-scheduler",
                    email="existing@example.com",
                    scheduling_administrator=False,
                    physician=None,
                    scheduler=self.superuser,
                )

        self.assertIn("email", raised.exception.message_dict)
        self.assertFalse(get_user_model().objects.filter(username="new-scheduler").exists())
        self.assertFalse(SchedulerInvitation.objects.exists())

    @override_settings(
        SCHEDULER_INVITATION_DELIVERY_MODE="manual", SCHEDULER_INVITATION_MAX_AGE=2 * 24 * 60 * 60
    )
    def test_invitation_uses_the_configured_lifetime(self):
        self.client.force_login(self.scheduling_administrator)
        created_at = datetime(2025, 1, 15, 12, tzinfo=UTC)

        with patch("schedules.services.accounts.timezone.now", return_value=created_at):
            response = self.client.post(
                reverse("account-invite"),
                {"username": "new-scheduler", "email": "new@example.com"},
            )

        invitation = SchedulerInvitation.objects.get(account__username="new-scheduler")
        expected_deadline = created_at + timedelta(days=2)
        self.assertEqual(invitation.created_at, created_at)
        self.assertEqual(invitation.expires_at, expected_deadline)
        _, message = manual_setup_link(self, response)
        relative_deadline = re.search(r"\b(?:2\s*days?|48\s*hours?)\b", message, re.IGNORECASE)
        absolute_deadlines = (
            expected_deadline.isoformat(),
            expected_deadline.strftime("%Y-%m-%d %H:%M:%S %z"),
            expected_deadline.strftime("%Y-%m-%d %H:%M %z"),
            expected_deadline.strftime("%Y-%m-%d %H:%M:%S UTC"),
            expected_deadline.strftime("%Y-%m-%d %H:%M UTC"),
        )
        self.assertTrue(
            relative_deadline or any(deadline in message for deadline in absolute_deadlines)
        )
