from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from schedules.forms.accounts import AccountChangeForm, AccountCreationForm
from schedules.models import Physician, PhysicianChange
from schedules.services.physicians import (
    create_physician,
    link_physician_user,
    physician_for_user,
    unlink_physician_user,
)
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


class PhysicianAccountIdentityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="manager", password="test-password", email="manager@example.com"
        )
        self.manager.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.scheduler = User.objects.create_user(
            username="scheduler", password="test-password", email="scheduler@example.com"
        )
        self.account = User.objects.create_user(
            username="physician-account", password="test-password", email="physician@example.com"
        )
        self.physician_name = "Account physician"
        self.physician = create_physician(display_name=self.physician_name, scheduler=self.manager)

    def test_link_replace_unlink_and_resolution_are_retained(self):
        replacement = get_user_model().objects.create_user(
            username="replacement", password="test-password", email="replacement@example.com"
        )

        link_physician_user(
            physician=self.physician,
            user=self.account,
            scheduler=self.manager,
            reason="Initial account link",
        )
        self.assertEqual(physician_for_user(self.account), self.physician)
        link_physician_user(
            physician=self.physician,
            user=replacement,
            scheduler=self.manager,
            reason="Correct account",
        )
        self.physician.refresh_from_db()
        physician_before = Physician.objects.values().get(pk=self.physician.pk)
        accounts_before = list(get_user_model().objects.order_by("pk").values())
        before_unlink = timezone.now()
        unlink_physician_user(
            physician=self.physician, scheduler=self.manager, reason="Account removed"
        )
        after_unlink = timezone.now()

        self.physician.refresh_from_db()
        self.assertIsNone(self.physician.user)
        changes = PhysicianChange.objects.filter(
            physician=self.physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
        )
        self.assertEqual(changes.count(), 3)
        change = changes.latest("pk")
        self.assertEqual(
            change.previous_value,
            {
                "user_id": replacement.pk,
                "email": "replacement@example.com",
                "username": "replacement",
                "is_active": True,
            },
        )
        self.assertIsNone(change.recorded_value)
        self.assertEqual(change.changed_by, self.manager)
        self.assertEqual(change.reason, "Account removed")
        self.assertGreaterEqual(change.changed_at, before_unlink)
        self.assertLessEqual(change.changed_at, after_unlink)
        self.assertEqual(
            Physician.objects.values().get(pk=self.physician.pk),
            physician_before | {"user_id": None},
        )
        self.assertEqual(list(get_user_model().objects.order_by("pk").values()), accounts_before)

    def test_unlink_already_unlinked_physician_preserves_history(self):
        history_before = list(PhysicianChange.objects.order_by("pk").values())

        result = unlink_physician_user(physician=self.physician, scheduler=self.manager)

        self.assertEqual(result.pk, self.physician.pk)
        self.assertIsNone(result.user_id)
        self.physician.refresh_from_db()
        self.assertIsNone(self.physician.user_id)
        self.assertEqual(list(PhysicianChange.objects.order_by("pk").values()), history_before)

    def test_unlink_requires_scheduling_administrator_even_for_noop(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    link_physician_user(
                        physician=self.physician, user=self.account, scheduler=self.manager
                    )
                history_before = list(PhysicianChange.objects.order_by("pk").values())
                with self.assertRaises(PermissionDenied):
                    unlink_physician_user(physician=self.physician, scheduler=self.scheduler)
                self.physician.refresh_from_db()
                self.assertEqual(self.physician.user_id, self.account.pk if linked else None)
                self.assertEqual(
                    list(PhysicianChange.objects.order_by("pk").values()), history_before
                )

    def test_unlink_history_failure_restores_link(self):
        link_physician_user(physician=self.physician, user=self.account, scheduler=self.manager)
        history_before = list(PhysicianChange.objects.order_by("pk").values())

        with (
            patch.object(
                PhysicianChange.objects, "create", side_effect=RuntimeError("history unavailable")
            ),
            self.assertRaisesRegex(RuntimeError, "history unavailable"),
        ):
            unlink_physician_user(physician=self.physician, scheduler=self.manager)

        self.physician.refresh_from_db()
        self.assertEqual(self.physician.user_id, self.account.pk)
        self.assertEqual(list(PhysicianChange.objects.order_by("pk").values()), history_before)

    def test_unlink_outer_rollback_restores_link_and_history(self):
        link_physician_user(physician=self.physician, user=self.account, scheduler=self.manager)
        history_before = list(PhysicianChange.objects.order_by("pk").values())

        with transaction.atomic():
            result = unlink_physician_user(physician=self.physician, scheduler=self.manager)
            self.assertIsNone(result.user_id)
            self.assertEqual(PhysicianChange.objects.count(), len(history_before) + 1)
            transaction.set_rollback(True)

        self.physician.refresh_from_db()
        self.assertEqual(self.physician.user_id, self.account.pk)
        self.assertEqual(list(PhysicianChange.objects.order_by("pk").values()), history_before)

    def test_link_rejects_blank_email_and_existing_link(self):
        blank = get_user_model().objects.create_user(
            username="blank", password="test-password", email=""
        )
        other_physician = create_physician(display_name="Other physician", scheduler=self.manager)

        with self.assertRaisesRegex(ValidationError, "configured email"):
            link_physician_user(physician=self.physician, user=blank, scheduler=self.manager)
        link_physician_user(physician=self.physician, user=self.account, scheduler=self.manager)
        with self.assertRaisesRegex(ValidationError, "another physician"):
            link_physician_user(
                physician=other_physician, user=self.account, scheduler=self.manager
            )

    def test_link_requires_scheduling_administrator_and_protects_the_linked_account(self):
        with self.assertRaises(PermissionDenied):
            link_physician_user(
                physician=self.physician, user=self.account, scheduler=self.scheduler
            )
        link_physician_user(physician=self.physician, user=self.account, scheduler=self.manager)

        with self.assertRaises(ProtectedError):
            self.account.delete()
        self.account.is_active = False
        self.account.save(update_fields=("is_active",))
        self.physician.refresh_from_db()
        self.assertEqual(self.physician.user, self.account)

    def test_detail_shows_account_context_and_only_manager_can_change_link(self):
        self.client.force_login(self.scheduler)
        detail_url = reverse("physician-detail", args=(self.physician.pk,))
        self.assertContains(self.client.get(detail_url), "No account linked.")
        denied = self.client.post(
            detail_url,
            {"action": "account", "user": self.account.pk, "reason": "Denied"},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIsNone(physician_for_user(self.account))

        self.client.force_login(self.manager)
        response = self.client.post(
            detail_url,
            {"action": "account", "user": self.account.pk, "reason": "Authorized"},
        )
        self.assertRedirects(response, detail_url)
        detail = self.client.get(detail_url)
        parser = _RenderedSurfaceParser()
        parser.feed(detail.content.decode())
        page_headings = [element for element in parser.elements if element.tag == "h1"]
        self.assertEqual(len(page_headings), 1)
        self.assertEqual(page_headings[0].text(), self.physician_name)
        account_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and element.attributes.get("aria-labelledby") == "account-heading"
        ]
        self.assertEqual(len(account_sections), 1)
        account_section = account_sections[0]
        account_heading = [
            element
            for element in account_section.descendants()
            if element.tag == "h2" and element.attributes.get("id") == "account-heading"
        ]
        self.assertEqual(len(account_heading), 1)
        account_summary_paragraphs = [
            element
            for element in account_section.descendants()
            if element.tag == "p"
            and not any(ancestor.tag == "form" for ancestor in element.ancestors())
        ]
        official_email = [
            paragraph
            for paragraph in account_summary_paragraphs
            if "official email" in paragraph.text().casefold()
        ]
        username_status = [
            paragraph
            for paragraph in account_summary_paragraphs
            if "username" in paragraph.text().casefold()
        ]
        self.assertEqual(len(official_email), 1)
        self.assertEqual(len(username_status), 1)
        self.assertIn(self.account.email, official_email[0].text())
        self.assertIn(self.account.username, username_status[0].text())
        self.assertRegex(username_status[0].text().casefold(), r"\bactive\b")
        account_forms = [
            element
            for element in account_section.descendants()
            if element.tag == "form"
            and any(
                child.attributes.get("name") == "action"
                and child.attributes.get("value") == "account"
                for child in element.descendants()
            )
        ]
        self.assertEqual(len(account_forms), 1)
        self.assertTrue(
            any(
                control.attributes.get("name") == "user"
                for control in account_forms[0].descendants()
            )
        )

    def test_django_admin_account_forms_reject_case_insensitive_email_collisions(self):
        get_user_model().objects.create_user(
            username="existing", password="test-password", email="taken@example.com"
        )
        change_form = AccountChangeForm(
            data={"username": self.account.username, "email": "TAKEN@example.com"},
            instance=self.account,
        )
        creation_form = AccountCreationForm(
            data={
                "username": "new-account",
                "email": "taken@example.com",
                "password1": "test-password-123",
                "password2": "test-password-123",
            }
        )

        self.assertFalse(change_form.is_valid())
        self.assertFalse(creation_form.is_valid())
        self.assertIn("email", change_form.errors)
        self.assertIn("email", creation_form.errors)
        for form in (change_form, creation_form):
            errors = form.errors.as_data()["email"]
            self.assertTrue(errors)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock conformance")
class PhysicianAccountUnlinkLockTests(TransactionTestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_superuser(
            username="manager", email="manager@example.com", password="test-password"
        )
        self.account = get_user_model().objects.create_user(
            username="physician-account", email="physician@example.com"
        )
        self.physician = Physician.objects.create(display_name="Linked", user=self.account)

    def _probe_locks(self):
        # Django connections are thread-local. Probe each row independently,
        # bypassing the application's Python lock and shared mutation authority.
        locked = []
        try:
            for row in (self.physician, self.account):
                try:
                    with transaction.atomic():
                        type(row).objects.select_for_update(nowait=True).get(pk=row.pk)
                except OperationalError as error:
                    if getattr(error.__cause__, "sqlstate", None) != "55P03":
                        raise
                    locked.append(True)
                else:
                    locked.append(False)
            return locked
        finally:
            connection.close()

    def _assert_lock_lifetime(self, *, rollback):
        with ThreadPoolExecutor(max_workers=1) as probe:
            self.assertEqual(probe.submit(self._probe_locks).result(timeout=10), [False, False])
            with transaction.atomic():
                unlink_physician_user(physician=self.physician, scheduler=self.manager)
                self.assertEqual(probe.submit(self._probe_locks).result(timeout=10), [True, True])
                if rollback:
                    transaction.set_rollback(True)
            self.assertEqual(probe.submit(self._probe_locks).result(timeout=10), [False, False])
        self.physician.refresh_from_db()
        self.assertEqual(self.physician.user_id, self.account.pk if rollback else None)
        self.assertEqual(
            PhysicianChange.objects.filter(
                physician=self.physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
            ).count(),
            0 if rollback else 1,
        )

    def test_both_rows_stay_locked_until_outer_commit(self):
        self._assert_lock_lifetime(rollback=False)

    def test_both_rows_stay_locked_until_outer_rollback(self):
        self._assert_lock_lifetime(rollback=True)
