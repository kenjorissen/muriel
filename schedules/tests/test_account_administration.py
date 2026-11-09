import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from schedules.models import AccountChange, PhysicianChange, SchedulerInvitation
from schedules.services.accounts import update_scheduler_account
from schedules.services.physicians import create_physician, link_physician_user
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


def recorded_email_values(history, row):
    """Return complete email values from one rendered account-history record."""
    return re.findall(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.+-])",
        history.cell_for_header(row, "Recorded value").text(),
    )


def communicates_empty_account_history(element):
    """Whether one empty history row conveys the absence of account changes."""
    words = set(re.findall(r"[a-z]+", element.text().casefold()))
    return (
        bool(words & {"no", "none", "empty", "nothing", "without"})
        and bool(words & {"account", "history", "record", "recorded"})
        and bool(words & {"change", "changes", "entry", "entries", "activity", "record", "records"})
    )


class AccountAdministrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username="administrator", password="test-password", email="administrator@example.com"
        )
        self.scheduler = User.objects.create_user(
            username="scheduler", password="test-password", email="scheduler@example.com"
        )
        self.scheduling_administrator = User.objects.create_user(
            username="manager", password="test-password", email="manager@example.com"
        )
        self.scheduling_administrator.groups.add(
            Group.objects.get(name="Scheduling Administrators")
        )
        self.other_scheduler = User.objects.create_user(
            username="other", password="test-password", email="other@example.com"
        )
        self.staff = User.objects.create_user(
            username="staff", password="test-password", email="staff@example.com", is_staff=True
        )
        self.physician = create_physician(display_name="Linked physician", scheduler=self.superuser)
        self.other_physician = create_physician(
            display_name="Other physician", scheduler=self.superuser
        )

    def test_service_updates_supported_fields_and_retains_redacted_history(self):
        update_scheduler_account(
            account=self.scheduler,
            username="corrected-scheduler",
            email="CORRECTED@example.com",
            is_active=False,
            scheduling_administrator=True,
            physician=self.physician,
            scheduler=self.scheduling_administrator,
            reason="Corrected roster",
        )

        self.scheduler.refresh_from_db()
        self.physician.refresh_from_db()
        change = AccountChange.objects.get(account=self.scheduler)
        self.assertEqual(self.scheduler.username, "corrected-scheduler")
        self.assertEqual(self.scheduler.email, "CORRECTED@example.com")
        self.assertFalse(self.scheduler.is_active)
        self.assertTrue(self.scheduler.groups.filter(name="Scheduling Administrators").exists())
        self.assertEqual(self.physician.user, self.scheduler)
        self.assertEqual(change.action, AccountChange.Action.UPDATED)
        self.assertEqual(change.changed_by, self.scheduling_administrator)
        self.assertEqual(change.reason, "Corrected roster")
        self.assertEqual(change.previous_value["physician"], None)
        self.assertEqual(change.recorded_value["physician"]["id"], self.physician.pk)
        self.assertNotIn("password", str(change.previous_value))
        self.assertNotIn("password", str(change.recorded_value))
        self.assertTrue(
            PhysicianChange.objects.filter(
                physician=self.physician, subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
            ).exists()
        )

    def test_service_rejects_ordinary_schedulers_and_protected_django_targets(self):
        with self.assertRaises(PermissionDenied):
            update_scheduler_account(
                account=self.scheduler,
                username="denied",
                email="denied@example.com",
                is_active=True,
                scheduling_administrator=False,
                physician=None,
                scheduler=self.scheduler,
            )
        with self.assertRaises(PermissionDenied):
            update_scheduler_account(
                account=self.staff,
                username="staff",
                email="staff@example.com",
                is_active=True,
                scheduling_administrator=False,
                physician=None,
                scheduler=self.superuser,
            )
        with self.assertRaises(PermissionDenied):
            update_scheduler_account(
                account=self.superuser,
                username="administrator",
                email="administrator@example.com",
                is_active=True,
                scheduling_administrator=False,
                physician=None,
                scheduler=self.superuser,
            )
        with self.assertRaises(PermissionDenied):
            update_scheduler_account(
                account=self.staff,
                username="staff",
                email="staff@example.com",
                is_active=True,
                scheduling_administrator=False,
                physician=None,
                scheduler=self.scheduling_administrator,
            )
        with self.assertRaises(ValidationError) as error:
            update_scheduler_account(
                account=self.scheduler,
                username="changed",
                email=self.other_scheduler.email.upper(),
                is_active=False,
                scheduling_administrator=True,
                physician=self.physician,
                scheduler=self.superuser,
            )
        self.assertIn("email", error.exception.message_dict)

        self.scheduler.refresh_from_db()
        self.physician.refresh_from_db()
        self.assertEqual(self.scheduler.username, "scheduler")
        self.assertTrue(self.scheduler.is_active)
        self.assertFalse(AccountChange.objects.exists())

    def test_scheduling_administrator_can_delegate_and_demote_themselves(self):
        update_scheduler_account(
            account=self.other_scheduler,
            username="other",
            email="other@example.com",
            is_active=True,
            scheduling_administrator=True,
            physician=None,
            scheduler=self.scheduling_administrator,
            reason="Delegate account administration",
        )
        self.other_scheduler.refresh_from_db()
        self.assertTrue(
            self.other_scheduler.groups.filter(name="Scheduling Administrators").exists()
        )

        update_scheduler_account(
            account=self.scheduling_administrator,
            username="manager",
            email="manager@example.com",
            is_active=False,
            scheduling_administrator=False,
            physician=None,
            scheduler=self.scheduling_administrator,
            reason="Leave account administration",
        )

        self.scheduling_administrator.refresh_from_db()
        self.assertFalse(self.scheduling_administrator.is_active)
        self.assertFalse(
            self.scheduling_administrator.groups.filter(name="Scheduling Administrators").exists()
        )
        self.assertEqual(
            AccountChange.objects.get(account=self.scheduling_administrator).changed_by,
            self.scheduling_administrator,
        )

        update_scheduler_account(
            account=self.scheduler,
            username="scheduler-updated-by-delegate",
            email="scheduler@example.com",
            is_active=True,
            scheduling_administrator=False,
            physician=None,
            scheduler=self.other_scheduler,
        )
        self.scheduler.refresh_from_db()
        self.assertEqual(self.scheduler.username, "scheduler-updated-by-delegate")

    def test_service_rejects_stale_physician_conflict_without_partial_change(self):
        link_physician_user(
            physician=self.physician, user=self.other_scheduler, scheduler=self.superuser
        )

        with self.assertRaisesRegex(ValidationError, "already linked"):
            update_scheduler_account(
                account=self.scheduler,
                username="changed",
                email="changed@example.com",
                is_active=False,
                scheduling_administrator=True,
                physician=self.physician,
                scheduler=self.superuser,
            )

        self.scheduler.refresh_from_db()
        self.physician.refresh_from_db()
        self.assertEqual(self.scheduler.username, "scheduler")
        self.assertEqual(self.scheduler.email, "scheduler@example.com")
        self.assertTrue(self.scheduler.is_active)
        self.assertEqual(self.physician.user, self.other_scheduler)
        self.assertFalse(AccountChange.objects.exists())

    def test_service_replaces_an_explicit_physician_link_and_retains_both_physician_changes(self):
        link_physician_user(physician=self.physician, user=self.scheduler, scheduler=self.superuser)

        update_scheduler_account(
            account=self.scheduler,
            username="scheduler",
            email="scheduler@example.com",
            is_active=True,
            scheduling_administrator=False,
            physician=self.other_physician,
            scheduler=self.superuser,
        )

        self.physician.refresh_from_db()
        self.other_physician.refresh_from_db()
        self.assertIsNone(self.physician.user)
        self.assertEqual(self.other_physician.user, self.scheduler)
        self.assertEqual(
            AccountChange.objects.get().recorded_value["physician"]["id"], self.other_physician.pk
        )
        self.assertEqual(
            PhysicianChange.objects.filter(
                subject=PhysicianChange.Subject.ACCOUNT_IDENTITY
            ).count(),
            3,
        )

    def test_superuser_workspace_renders_inventory_history_and_only_supported_controls(self):
        self.client.force_login(self.superuser)

        workspace = self.client.get(reverse("account-workspace"))
        history = self.client.get(reverse("account-history"))
        edit = self.client.get(reverse("account-edit", args=(self.scheduler.pk,)))
        administrator = self.client.get(reverse("account-edit", args=(self.staff.pk,)))

        self.assertContains(workspace, "Account Administration")
        self.assertContains(workspace, "scheduler@example.com")
        self.assertContains(workspace, "Accounts")
        self.assertContains(workspace, "Invite user")
        self.assertNotContains(workspace, "Invite scheduler")
        accounts = _RenderedDataTable(workspace.content.decode(), table_class="account-table")
        administrator_rows = [
            row
            for row in accounts.body_rows()
            if accounts.cell_for_header(row, "Username").text() == self.staff.username
            and accounts.cell_for_header(row, "Official email").text() == self.staff.email
        ]
        self.assertEqual(len(administrator_rows), 1)
        self.assertIn(
            "Django administrator",
            accounts.cell_for_header(administrator_rows[0], "Authority").text(),
        )
        administrator_actions = accounts.cell_for_header(administrator_rows[0], "Action")
        action_targets = {
            element.attributes["href"]
            for element in administrator_actions.descendants_named("a")
            if "href" in element.attributes
        } | {
            element.attributes["action"]
            for element in administrator_actions.descendants_named("form")
            if "action" in element.attributes
        }
        self.assertFalse(
            {
                reverse("account-edit", args=(self.staff.pk,)),
                reverse("account-invite"),
                reverse("account-invitation-resend", args=(self.staff.pk,)),
                reverse("account-invitation-revoke", args=(self.staff.pk,)),
            }
            & action_targets
        )
        invitation = self.client.get(reverse("account-invite"))
        self.assertContains(invitation, 'name="username"')
        self.assertContains(invitation, 'name="email"')
        empty_history = _RenderedDataTable(history.content.decode(), "account-history")
        empty_rows = empty_history.body_rows()
        self.assertEqual(len(empty_rows), 1)
        self.assertFalse(any(row.children_named("th") for row in empty_rows))
        empty_feedback = empty_rows[0].children_named("td")
        self.assertEqual(len(empty_feedback), 1)
        self.assertTrue(communicates_empty_account_history(empty_feedback[0]))
        self.assertContains(edit, "Official email")
        self.assertContains(edit, "Scheduling Administrator")
        self.assertNotContains(edit, 'name="password"')
        self.assertContains(administrator, "read-only here")

    def test_account_workspace_uses_surface_content_stack_for_flash_messages(self):
        self.client.force_login(self.superuser)

        response = self.client.post(reverse("account-invitation-revoke", args=(self.scheduler.pk,)))
        self.assertRedirects(response, reverse("account-workspace"), fetch_redirect_response=False)

        workspace = self.client.get(reverse("account-workspace"))
        parser = _RenderedSurfaceParser()
        parser.feed(workspace.content.decode())
        refusal_feedback = [
            element
            for element in parser.elements
            if element.tag == "p"
            and any(
                ancestor.tag == "section"
                and "surface-tone--attention" in ancestor.attributes.get("class", "").split()
                for ancestor in element.ancestors()
            )
            and any(term in element.text().casefold() for term in ("no", "without", "not"))
            and any(
                term in element.text().casefold() for term in ("pending", "active", "invitation")
            )
        ]
        self.assertEqual(len(refusal_feedback), 1)
        self.scheduler.refresh_from_db()
        self.assertTrue(self.scheduler.is_active)
        self.assertFalse(SchedulerInvitation.objects.filter(account=self.scheduler).exists())

    def test_account_history_renders_a_change_with_its_own_account_and_action(self):
        update_scheduler_account(
            account=self.scheduler,
            username="updated-scheduler",
            email="updated@example.com",
            is_active=True,
            scheduling_administrator=False,
            physician=None,
            scheduler=self.scheduling_administrator,
            reason="Correct account contact details",
        )
        update_scheduler_account(
            account=self.other_scheduler,
            username="other-updated-scheduler",
            email="other-updated@example.com",
            is_active=True,
            scheduling_administrator=False,
            physician=None,
            scheduler=self.scheduling_administrator,
            reason="Correct another account contact detail",
        )
        self.client.force_login(self.scheduling_administrator)

        response = self.client.get(reverse("account-history"))

        history = _RenderedDataTable(response.content.decode(), "account-history")
        for account, email in (
            ("updated-scheduler", "updated@example.com"),
            ("other-updated-scheduler", "other-updated@example.com"),
        ):
            with self.subTest(account=account):
                rows = [
                    row
                    for row in history.body_rows()
                    if history.cell_for_header(row, "Account").text() == account
                ]
                self.assertEqual(len(rows), 1)
                self.assertRegex(
                    history.cell_for_header(rows[0], "Action").text(),
                    r"(?i)\b(?:updated|changed|edited)\b",
                )
                self.assertEqual(recorded_email_values(history, rows[0]), [email])

    def test_direct_requests_and_normal_header_enforce_accounts_authority_boundary(self):
        self.client.force_login(self.scheduler)
        self.assertNotContains(self.client.get(reverse("monthly-workspace")), "Accounts")
        self.assertEqual(self.client.get(reverse("account-workspace")).status_code, 403)
        self.assertEqual(self.client.get(reverse("account-history")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("account-edit", args=(self.other_scheduler.pk,)),
                {
                    "username": "crafted",
                    "email": "crafted@example.com",
                    "is_active": "",
                    "scheduling_administrator": "on",
                    "physician": self.physician.pk,
                },
            ).status_code,
            403,
        )
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.post(
                reverse("account-edit", args=(self.staff.pk,)),
                {"username": "crafted", "email": "crafted@example.com"},
            ).status_code,
            403,
        )
        self.other_scheduler.refresh_from_db()
        self.assertEqual(self.other_scheduler.username, "other")

    def test_scheduling_administrator_can_access_accounts_and_edit_an_ordinary_account(self):
        self.client.force_login(self.scheduling_administrator)

        self.assertContains(self.client.get(reverse("monthly-workspace")), "Accounts")
        self.assertEqual(self.client.get(reverse("account-workspace")).status_code, 200)
        self.assertEqual(self.client.get(reverse("account-history")).status_code, 200)
        response = self.client.post(
            reverse("account-edit", args=(self.other_scheduler.pk,)),
            {
                "username": "updated-by-manager",
                "email": "updated@example.com",
                "is_active": "on",
                "scheduling_administrator": "on",
                "physician": "",
            },
        )

        self.assertRedirects(response, reverse("account-workspace"))
        self.other_scheduler.refresh_from_db()
        self.assertEqual(self.other_scheduler.username, "updated-by-manager")
        self.assertTrue(
            self.other_scheduler.groups.filter(name="Scheduling Administrators").exists()
        )
