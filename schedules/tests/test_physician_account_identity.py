from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse

from schedules.forms.accounts import AccountChangeForm, AccountCreationForm
from schedules.models import PhysicianChange
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
        unlink_physician_user(
            physician=self.physician, scheduler=self.manager, reason="Account removed"
        )

        self.physician.refresh_from_db()
        self.assertIsNone(self.physician.user)
        changes = PhysicianChange.objects.filter(
            physician=self.physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
        )
        self.assertEqual(changes.count(), 3)
        self.assertEqual(changes.latest("pk").previous_value["email"], "replacement@example.com")
        self.assertIsNone(changes.latest("pk").recorded_value)

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
