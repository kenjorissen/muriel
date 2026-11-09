from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from schedules.forms.physicians import FacultyAvailabilityTemplateForm
from schedules.models import (
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.availability_categories import (
    correct_custom_availability_category,
    create_custom_availability_category,
    deactivate_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.physicians import (
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    deactivate_monthly_physician_availability_rule,
)
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    open_monthly_draft,
)
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_views import assert_rendered_heading, assert_rendered_navigation
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser


class CustomAvailabilityCategoryServiceTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.faculty = Physician.objects.create(display_name="Faculty physician")
        EffectiveRole.objects.create(
            physician=self.faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.other = Physician.objects.create(display_name="Community physician")

    def category(self, **overrides):
        values = {
            "name": "Sports Med Training Room",
            "impact": "not_available",
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_custom_availability_category(**values)

    def test_category_has_stable_identity_unique_usable_name_and_mapped_impact(self):
        category = self.category(
            name=" Sports Med Training Room ", impact="backup_only", reason="Clinic use"
        )

        self.assertEqual(category.name, "Sports Med Training Room")
        self.assertEqual(category.impact, CustomAvailabilityCategory.Impact.BACKUP_ONLY)
        self.assertTrue(category.is_active)
        self.assertIsNotNone(category.pk)
        with self.assertRaises(ValidationError):
            self.category(name="sports med training room")
        with self.assertRaises(ValidationError):
            self.category(name="   ")
        change = category.changes.get()
        self.assertEqual(change.changed_by, self.scheduler)
        self.assertEqual(change.reason, "Clinic use")
        self.assertEqual(change.recorded_value["impact"], "backup_only")

    def test_assignment_changes_are_retained_with_provenance_and_do_not_touch_templates(self):
        category = self.category()
        template = FacultyAvailabilityTemplate.objects.create(
            physician=self.faculty,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )

        set_custom_availability_category_physicians(
            category=category,
            physicians=[self.faculty],
            scheduler=self.scheduler,
            reason="Uses training room",
        )
        set_custom_availability_category_physicians(
            category=category,
            physicians=[],
            scheduler=self.scheduler,
            reason="Rotation ended",
        )

        assignment = CustomAvailabilityCategoryAssignment.objects.get(
            category=category, physician=self.faculty
        )
        self.assertFalse(assignment.is_active)
        self.assertEqual(
            template.slots["monday_am"],
            FacultyAvailabilityTemplate.base_selection(
                FacultyAvailabilityTemplate.AvailabilityState.OPEN
            ),
        )
        changes = list(category.changes.exclude(action="created").order_by("pk"))
        self.assertEqual(
            [change.action for change in changes],
            [
                CustomAvailabilityCategoryChange.Action.ASSIGNED,
                CustomAvailabilityCategoryChange.Action.UNASSIGNED,
            ],
        )
        self.assertEqual(changes[0].physician, self.faculty)
        self.assertEqual(changes[0].changed_by, self.scheduler)
        self.assertEqual(changes[1].reason, "Rotation ended")
        self.assertTrue(changes[0].recorded_value["is_active"])
        self.assertFalse(changes[1].recorded_value["is_active"])

    def test_renaming_and_deactivation_retain_the_category_and_assignment_history(self):
        category = self.category()
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )

        correct_custom_availability_category(
            category=category,
            name="Athletic Training Room",
            impact="available",
            scheduler=self.scheduler,
            reason="Updated name",
        )
        deactivate_custom_availability_category(
            category=category, scheduler=self.scheduler, reason="No longer offered"
        )

        category.refresh_from_db()
        self.assertEqual(category.name, "Athletic Training Room")
        self.assertFalse(category.is_active)
        self.assertTrue(category.assignments.get(physician=self.faculty).is_active)
        self.assertEqual(category.changes.count(), 4)
        self.assertEqual(
            category.changes.get(action="deactivated").previous_value["name"],
            "Athletic Training Room",
        )
        with self.assertRaises(ValidationError):
            category.delete()
        with self.assertRaises(ValidationError):
            category.assignments.get().delete()

    def test_only_authenticated_schedulers_can_change_categories_and_only_faculty_can_be_assigned(
        self,
    ):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        with self.assertRaises(PermissionDenied):
            self.category(scheduler=anonymous)
        category = self.category()
        with self.assertRaises(ValidationError):
            set_custom_availability_category_physicians(
                category=category, physicians=[self.other], scheduler=self.scheduler
            )
        self.assertFalse(category.assignments.exists())

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_unassignment_rejects_current_template_reference_without_partial_assignment_changes(
        self, now
    ):
        category = self.category()
        other = Physician.objects.create(display_name="Other Faculty")
        EffectiveRole.objects.create(
            physician=other,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        create_faculty_availability_template(
            physician=self.faculty,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            slots={
                slot: {"category_id": category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        history_count = category.changes.count()

        with self.assertRaises(ValidationError) as error:
            set_custom_availability_category_physicians(
                category=category, physicians=[other], scheduler=self.scheduler
            )

        message = str(error.exception)
        self.assertIn(self.faculty.display_name, message)
        self.assertIn("Faculty weekly template", message)
        self.assertIn("effective 2025-01-01 through 2025-01-31", message)
        self.assertNotIn("Monthly scheduling rule", message)
        self.assertTrue(category.assignments.get(physician=self.faculty).is_active)
        self.assertFalse(category.assignments.filter(physician=other).exists())
        self.assertEqual(category.changes.count(), history_count)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_unassignment_rejects_future_template_reference_without_partial_assignment_changes(
        self, now
    ):
        category = self.category()
        other = Physician.objects.create(display_name="Other Faculty")
        EffectiveRole.objects.create(
            physician=other,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        create_faculty_availability_template(
            physician=self.faculty,
            effective_start=date(2025, 2, 1),
            effective_end=None,
            slots={
                slot: {"category_id": category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        history_count = category.changes.count()

        with self.assertRaises(ValidationError) as error:
            set_custom_availability_category_physicians(
                category=category, physicians=[other], scheduler=self.scheduler
            )

        message = str(error.exception)
        self.assertIn(self.faculty.display_name, message)
        self.assertIn("Faculty weekly template", message)
        self.assertIn("effective 2025-02-01 through ongoing", message)
        self.assertNotIn("Monthly scheduling rule", message)
        self.assertTrue(category.assignments.get(physician=self.faculty).is_active)
        self.assertFalse(category.assignments.filter(physician=other).exists())
        self.assertEqual(category.changes.count(), history_count)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_unassignment_ignores_active_sources_using_a_distinct_category(self, now):
        category = self.category()
        other_category = self.category(name="Other Clinic", impact="available")
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=other_category, physicians=[self.faculty], scheduler=self.scheduler
        )
        current_template = create_faculty_availability_template(
            physician=self.faculty,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            slots={
                slot: {"category_id": other_category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        future_template = create_faculty_availability_template(
            physician=self.faculty,
            effective_start=date(2025, 2, 1),
            effective_end=None,
            slots={
                slot: {"category_id": other_category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        monthly_rule = create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=2,
            ordinals="all",
            periods=["PM"],
            availability_selection={"category_id": other_category.pk},
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        history_count = category.changes.count()

        set_custom_availability_category_physicians(
            category=category, physicians=[], scheduler=self.scheduler
        )

        self.assertFalse(category.assignments.get(physician=self.faculty).is_active)
        unassignment = category.changes.latest("pk")
        self.assertEqual(unassignment.action, CustomAvailabilityCategoryChange.Action.UNASSIGNED)
        self.assertEqual(unassignment.physician, self.faculty)
        self.assertEqual(category.changes.count(), history_count + 1)
        current_template.refresh_from_db()
        future_template.refresh_from_db()
        monthly_rule.refresh_from_db()
        self.assertEqual(
            current_template.slots["monday_am"],
            {"category_id": other_category.pk, "category_name": "Other Clinic"},
        )
        self.assertEqual(
            future_template.slots["monday_am"],
            {"category_id": other_category.pk, "category_name": "Other Clinic"},
        )
        self.assertEqual(
            monthly_rule.availability_selection,
            {"category_id": other_category.pk, "category_name": "Other Clinic"},
        )
        self.assertTrue(monthly_rule.is_active)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_unassignment_rejects_active_monthly_rule_without_partial_assignment_changes(self, now):
        category = self.category()
        other = Physician.objects.create(display_name="Other Faculty")
        EffectiveRole.objects.create(
            physician=other,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        rule = create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=2,
            ordinals="all",
            periods=["PM"],
            availability_selection={"category_id": category.pk},
            effective_start=date(2025, 1, 1),
            effective_end=None,
            annual_start_month=9,
            annual_end_month=5,
            scheduler=self.scheduler,
        )
        history_count = category.changes.count()

        with self.assertRaises(ValidationError) as error:
            set_custom_availability_category_physicians(
                category=category, physicians=[other], scheduler=self.scheduler
            )

        message = str(error.exception)
        self.assertTrue(rule.is_active)
        self.assertIn(self.faculty.display_name, message)
        self.assertIn("Monthly scheduling rule", message)
        self.assertNotIn("Faculty weekly template", message)
        self.assertTrue(category.assignments.get(physician=self.faculty).is_active)
        self.assertFalse(category.assignments.filter(physician=other).exists())
        self.assertEqual(category.changes.count(), history_count)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_unassignment_diagnostic_identifies_mixed_active_references_without_writes(self, now):
        category = self.category()
        other = Physician.objects.create(display_name="Other Faculty")
        EffectiveRole.objects.create(
            physician=other,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        create_faculty_availability_template(
            physician=self.faculty,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={
                slot: {"category_id": category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=2,
            ordinals="all",
            periods=["PM"],
            availability_selection={"category_id": category.pk},
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        history_count = category.changes.count()

        with self.assertRaises(ValidationError) as error:
            set_custom_availability_category_physicians(
                category=category, physicians=[other], scheduler=self.scheduler
            )

        message = str(error.exception)
        self.assertIn(self.faculty.display_name, message)
        self.assertIn("Faculty weekly template", message)
        self.assertIn("Monthly scheduling rule", message)
        self.assertTrue(category.assignments.get(physician=self.faculty).is_active)
        self.assertFalse(category.assignments.filter(physician=other).exists())
        self.assertEqual(category.changes.count(), history_count)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_expired_and_deactivated_monthly_rules_do_not_block_unassignment(self, now):
        category = self.category()
        EffectiveRole.objects.create(
            physician=self.faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=date(2024, 12, 31),
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": category.pk},
            effective_end=date(2024, 12, 31),
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=self.faculty,
            effective_start=None,
            effective_end=date(2024, 12, 31),
            slots={
                slot: {"category_id": category.pk}
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
        )
        deactivated = create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": category.pk},
            scheduler=self.scheduler,
        )
        deactivate_monthly_physician_availability_rule(rule=deactivated, scheduler=self.scheduler)

        set_custom_availability_category_physicians(
            category=category, physicians=[], scheduler=self.scheduler
        )

        self.assertFalse(category.assignments.get(physician=self.faculty).is_active)
        self.assertEqual(MonthlyPhysicianAvailabilityRule.objects.count(), 2)


class CustomAvailabilityCategoryViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.faculty = Physician.objects.create(display_name="Taylor Morgan")
        EffectiveRole.objects.create(
            physician=self.faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.nonfaculty = Physician.objects.create(display_name="Community physician")
        self.client.force_login(self.user)

    def test_physician_surface_presents_empty_and_populated_category_management_with_history(self):
        workspace_url = reverse("custom-availability-category-workspace")
        response = self.client.get(workspace_url)

        self.assertContains(response, "Schedule options")
        self.assertContains(response, "Open, Backup only, Patient care, and Off")
        self.assertContains(response, "Admin")
        self.assertContains(response, "Built-in")
        directory = self.client.get(reverse("physicians-directory"))
        self.assertContains(directory, "Schedule Options")
        self.assertNotContains(directory, "Manage schedule options")
        create_form = self.client.get(reverse("custom-availability-category-create"))
        self.assertContains(create_form, "Add schedule option")
        self.assertContains(create_form, "Back to schedule options")

        response = self.client.post(
            reverse("custom-availability-category-create"),
            {
                "name": "Sports Med Training Room",
                "impact": "not_available",
                "reason": "Clinic setup",
            },
        )
        category = CustomAvailabilityCategory.objects.get(name="Sports Med Training Room")
        self.assertRedirects(
            response, reverse("custom-availability-category-edit", args=(category.pk,))
        )
        edit_url = reverse("custom-availability-category-edit", args=(category.pk,))
        response = self.client.get(edit_url)
        self.assertContains(response, "Back to schedule options")
        self.assertNotContains(response, "Active schedule option.")
        self.assertContains(response, "Physician assignment")
        self.assertContains(response, "Taylor Morgan")
        self.assertFalse(
            response.context["assignment_form"]
            .fields["physicians"]
            .queryset.filter(pk=self.nonfaculty.pk)
            .exists()
        )
        self.assertContains(response, "Filter physicians")
        self.assertContains(
            response, 'class="form-helper" id="id_physicians-count" data-physician-picker-caption'
        )
        self.assertContains(response, "Selected physicians")
        self.assertContains(response, "schedules/physicians-picker.js")
        self.assertContains(response, "Deactivate schedule option")
        self.assertNotContains(response, f"#{category.pk}")

        response = self.client.post(
            edit_url,
            {"action": "assignments", "physicians": [self.faculty.pk], "reason": "Uses this room"},
        )
        self.assertRedirects(response, edit_url)
        workspace = self.client.get(workspace_url)
        schedule_options = _RenderedDataTable(workspace.content.decode(), "schedule-options")
        category_rows = [
            row
            for row in schedule_options.body_rows()
            if schedule_options.cell_for_header(row, "Schedule option").text() == category.name
        ]
        self.assertEqual(len(category_rows), 1)
        self.assertEqual(
            schedule_options.cell_for_header(category_rows[0], "Assigned physicians").text(),
            self.faculty.display_name,
        )
        history = self.client.get(reverse("custom-availability-category-history"))
        history_table = _RenderedDataTable(history.content.decode(), "schedule-option-history")
        assignment_rows = [
            row
            for row in history_table.body_rows()
            if history_table.cell_for_header(row, "Schedule option").text() == category.name
            and history_table.cell_for_header(row, "Physician").text() == self.faculty.display_name
        ]
        self.assertEqual(len(assignment_rows), 1)
        self.assertEqual(
            history_table.cell_for_header(assignment_rows[0], "Change").text(),
            "Assigned to physician",
        )
        self.assertEqual(
            history_table.cell_for_header(assignment_rows[0], "Note").text(), "Uses this room"
        )

        invalid = self.client.post(
            edit_url,
            {
                "action": "assignments",
                "physicians": [self.faculty.pk, self.nonfaculty.pk],
                "reason": "Keep this selection",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Select a valid choice")
        parser = _RenderedSurfaceParser()
        parser.feed(invalid.content.decode())
        selected = [
            element
            for element in parser.elements
            if element.tag == "input"
            and element.attributes.get("name") == "physicians"
            and element.attributes.get("value") == str(self.faculty.pk)
            and "checked" in element.attributes
        ]
        self.assertEqual(len(selected), 1)

        deactivate_custom_availability_category(category=category, scheduler=self.user)
        inactive = self.client.get(edit_url)
        self.assertContains(inactive, "Inactive schedule option retained for history.")
        self.assertNotContains(inactive, "Active schedule option.")

    def test_schedule_options_is_the_selected_final_physician_local_view(self):
        views = (
            (reverse("physicians-directory"), "All"),
            (reverse("physicians-directory") + "?view=faculty", "Faculty"),
            (
                reverse("physicians-directory") + "?view=community-rounders",
                "Community Rounders",
            ),
            (
                reverse("physicians-directory") + "?view=community-preceptors",
                "Community Preceptors",
            ),
            (reverse("physicians-directory") + "?view=casual-rounders", "Casual Rounders"),
            (
                reverse("physicians-directory") + "?view=external-patient-care",
                "External Patient Care",
            ),
            (reverse("physicians-directory") + "?view=inactive", "Inactive"),
            (reverse("custom-availability-category-workspace"), "Schedule Options"),
        )
        for url, current_label in views:
            with self.subTest(url=url):
                response = self.client.get(url)
                assert_rendered_navigation(
                    self,
                    response,
                    aria_label="Physician navigation",
                    expected=tuple((label, url) for url, label in views),
                    current=current_label,
                )

    def test_schedule_options_uses_local_navigation_introduction_and_standard_actions(self):
        response = self.client.get(reverse("custom-availability-category-workspace"))
        assert_rendered_heading(self, response, "Schedule options")
        assert_rendered_heading(self, response, "Schedule Options", level=2)
        table = _RenderedDataTable(response.content.decode(), "schedule-options")
        self.assertEqual(table.table.attributes.get("aria-label"), "Schedule options")
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        introductions = [
            element
            for element in parser.elements
            if element.tag == "p"
            and "workspace-introduction" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(introductions), 1)
        introduction = introductions[0].text()
        self.assertIn("supplement", introduction)
        self.assertIn("fixed", introduction)
        for state in ("Open", "Backup only", "Patient care", "Off"):
            with self.subTest(state=state):
                self.assertIn(state, introduction)
        action_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and "workspace-actions" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(action_sections), 1)
        action_section = action_sections[0]
        action_headings = [
            element
            for element in action_section.descendants()
            if element.tag == "h2" and " ".join(element.text().split()) == "Actions"
        ]
        self.assertEqual(len(action_headings), 1)
        add_option_actions = [
            element
            for element in action_section.descendants()
            if element.tag == "a"
            and " ".join(element.text().split()) == "Add schedule option"
            and _action_matches(
                element.attributes.get("href", ""),
                reverse("custom-availability-category-create"),
            )
        ]
        history_actions = [
            element
            for element in action_section.descendants()
            if element.tag == "a"
            and " ".join(element.text().split()) == "Schedule option change history"
            and _action_matches(
                element.attributes.get("href", ""),
                reverse("custom-availability-category-history"),
            )
        ]
        self.assertEqual(len(add_option_actions), 1)
        self.assertEqual(len(history_actions), 1)

    def test_category_management_requires_authentication(self):
        self.client.logout()

        self.assertEqual(
            self.client.get(reverse("custom-availability-category-workspace")).status_code, 302
        )
        self.assertEqual(
            self.client.post(
                reverse("custom-availability-category-create"),
                {"name": "Protected", "impact": "available"},
            ).status_code,
            302,
        )
        self.assertFalse(
            CustomAvailabilityCategory.objects.exclude(
                system_key=CustomAvailabilityCategory.SystemKey.ADMIN
            ).exists()
        )

    def test_admin_is_identified_as_builtin_but_keeps_explicit_faculty_assignment(self):
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        edit_url = reverse("custom-availability-category-edit", args=(admin.pk,))

        response = self.client.get(edit_url)
        self.assertContains(response, "Built-in system option")
        self.assertContains(response, "Physician assignment")
        self.assertNotContains(response, "Save schedule option")
        self.assertNotContains(response, "Deactivate schedule option")

        forged = self.client.post(
            edit_url,
            {"action": "definition", "name": "Changed", "impact": "available"},
        )
        self.assertEqual(forged.status_code, 200)
        admin.refresh_from_db()
        self.assertEqual(admin.name, "Admin")
        self.assertEqual(admin.impact, CustomAvailabilityCategory.Impact.NOT_AVAILABLE)

        with self.assertRaisesRegex(ValidationError, "Built-in schedule options"):
            correct_custom_availability_category(
                category=admin,
                name="Changed",
                impact="available",
                scheduler=self.user,
            )
        with self.assertRaisesRegex(ValidationError, "Built-in schedule options"):
            deactivate_custom_availability_category(category=admin, scheduler=self.user)

        admin.name = "Changed"
        with self.assertRaisesRegex(ValidationError, "name cannot be changed"):
            admin.full_clean()
        admin.refresh_from_db()

        for field, value, message in (
            ("system_key", None, "identity cannot be changed"),
            ("name", "Changed", "name cannot be changed"),
            ("impact", CustomAvailabilityCategory.Impact.AVAILABLE, "impact cannot be changed"),
            ("is_active", False, "cannot be deactivated"),
        ):
            with self.subTest(field=field):
                setattr(admin, field, value)
                with self.assertRaisesRegex(ValidationError, message):
                    admin.save(update_fields=(field,))
                admin.refresh_from_db()

        set_custom_availability_category_physicians(
            category=admin, physicians=[self.faculty], scheduler=self.user
        )
        self.assertTrue(admin.assignments.get(physician=self.faculty).is_active)


class CustomAvailabilityCategoryTemplateTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = Physician.objects.create(display_name="Sports faculty")
        self.other_physician = Physician.objects.create(display_name="Other faculty")
        for physician in (self.physician, self.other_physician):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start="2025-01-01",
            )
        self.category = create_custom_availability_category(
            name="Sports Med Training Room", impact="backup_only", scheduler=self.scheduler
        )
        self.other_category = create_custom_availability_category(
            name="Other Clinic", impact="available", scheduler=self.scheduler
        )
        self.additional_category = create_custom_availability_category(
            name="Research Conference", impact="available", scheduler=self.scheduler
        )
        self.inactive_category = create_custom_availability_category(
            name="Retired Clinic", impact="not_available", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=self.category, physicians=[self.physician], scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=self.other_category,
            physicians=[self.other_physician],
            scheduler=self.scheduler,
        )
        set_custom_availability_category_physicians(
            category=self.additional_category,
            physicians=[self.physician],
            scheduler=self.scheduler,
        )
        set_custom_availability_category_physicians(
            category=self.inactive_category,
            physicians=[self.physician],
            scheduler=self.scheduler,
        )
        deactivate_custom_availability_category(
            category=self.inactive_category, scheduler=self.scheduler
        )

    def slots(self, **overrides):
        values = {
            slot: FacultyAvailabilityTemplate.base_selection(
                FacultyAvailabilityTemplate.AvailabilityState.OPEN
            )
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        values.update(overrides)
        return values

    def test_template_selection_is_explicit_and_only_assigned_active_categories_are_offered(self):
        form = FacultyAvailabilityTemplateForm(physician=self.physician)
        choices = form.fields["monday_am"].choices

        base_choices = [
            ("base:open", "Open"),
            ("base:backup_only", "Backup only"),
            ("base:patient_care", "Patient care"),
            ("base:off", "Off"),
        ]
        self.assertEqual(
            [choice for choice in choices if choice[0].startswith("base:")], base_choices
        )
        choices_by_value = dict(choices)
        self.assertEqual(choices_by_value[f"category:{self.category.pk}"], self.category.name)
        self.assertEqual(
            choices_by_value[f"category:{self.additional_category.pk}"],
            self.additional_category.name,
        )
        self.assertNotIn(f"category:{self.other_category.pk}", choices_by_value)
        self.assertNotIn(f"category:{self.inactive_category.pk}", choices_by_value)
        template = create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=self.slots(monday_am={"category_id": self.category.pk}),
            scheduler=self.scheduler,
        )
        self.assertEqual(
            template.slots["monday_am"],
            {"category_id": self.category.pk, "category_name": "Sports Med Training Room"},
        )
        invalid = FacultyAvailabilityTemplate(
            physician=self.physician,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        with self.assertRaises(ValidationError):
            create_faculty_availability_template(
                physician=self.physician,
                effective_start=date(2025, 2, 1),
                effective_end=None,
                slots=self.slots(monday_am={"category_id": self.other_category.pk}),
                scheduler=self.scheduler,
            )

    def test_category_selection_preserves_its_label_while_its_impact_drives_availability(self):
        template = create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=self.slots(monday_am={"category_id": self.category.pk}),
            scheduler=self.scheduler,
        )
        availability = effective_preceptor_availability_on(self.physician, date(2025, 1, 6), "AM")

        self.assertEqual(availability.base_availability, "backup_only")
        self.assertEqual(availability.availability, "backup_only")
        self.assertEqual(availability.base_availability_display, "Sports Med Training Room")
        self.assertEqual(
            availability.sources[0].label,
            f"Faculty Schedule template {template.pk}: Sports Med Training Room",
        )
        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        candidate = next(
            alternative
            for alternative in assignment_alternatives_for_session(
                session, PreceptorAssignment.Responsibility.PRIMARY, 1
            )
            if alternative.physician == self.physician
        )
        self.assertEqual(candidate.tier, "backup_only")
        category_history = template.physician.changes.latest("pk")
        correct_custom_availability_category(
            category=self.category,
            name="Athletic Training Room",
            impact="not_available",
            scheduler=self.scheduler,
        )
        availability = effective_preceptor_availability_on(self.physician, date(2025, 1, 6), "AM")

        self.assertEqual(availability.availability, "not_available")
        self.assertEqual(availability.base_availability_display, "Sports Med Training Room")
        assign_preceptor(
            session=session,
            physician=self.physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        finding = next(
            finding
            for finding in draft_findings_for_session(session).findings
            if finding.code == "not_available_assignment"
        )
        self.assertEqual(finding.severity, "hard")
        category_history.refresh_from_db()
        self.assertEqual(
            category_history.recorded_value["slots"]["monday_am"]["category_name"],
            "Sports Med Training Room",
        )

    def test_monthly_category_rule_does_not_apply_outside_its_annual_window(self):
        template = create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=self.slots(),
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=2,
            ordinals="all",
            periods=["PM"],
            availability_selection={"category_id": self.category.pk},
            effective_start=date(2025, 1, 1),
            effective_end=None,
            annual_start_month=9,
            annual_end_month=5,
            scheduler=self.scheduler,
        )

        availability = effective_preceptor_availability_on(self.physician, date(2025, 6, 4), "PM")

        self.assertEqual(
            availability.availability_selection,
            {"base_state": "open"},
        )
        self.assertEqual(availability.availability, "available")
        self.assertEqual(availability.faculty_template.pk, template.pk)
        self.assertIsNone(availability.monthly_availability_rule)

    def test_template_form_retains_an_unassigned_option_only_in_its_recorded_slot(self):
        template = create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=self.slots(monday_am={"category_id": self.category.pk}),
            scheduler=self.scheduler,
        )
        assignment = self.category.assignments.get(physician=self.physician)
        assignment.is_active = False
        assignment.save(update_fields=("is_active",))

        form = FacultyAvailabilityTemplateForm(physician=self.physician, template=template)

        self.assertIn(
            (f"category:{self.category.pk}", self.category.name), form.fields["monday_am"].choices
        )
        self.assertNotIn(
            (f"category:{self.category.pk}", self.category.name), form.fields["monday_pm"].choices
        )
