import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from schedules.models import EffectiveRole, FacultyAvailabilityTemplate, Physician, PhysicianChange
from schedules.services.availability_categories import (
    create_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.physicians import (
    add_role,
    correct_physician_identity,
    correct_role,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
    end_role,
)
from schedules.services.rounding import create_casual_rounder_rule
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


def capacity_summary_paragraphs(section, labels):
    """Return non-form capacity summaries with one bounded semantic label."""
    return [
        paragraph
        for paragraph in section.descendants()
        if paragraph.tag == "p"
        and not any(ancestor.tag == "form" for ancestor in paragraph.ancestors())
        and "capacity" in paragraph.text().casefold()
        and any(
            re.search(rf"\b{re.escape(label)}\b", paragraph.text().casefold()) for label in labels
        )
    ]


def rendered_capacity_values(paragraph):
    """Read complete signed decimal values without matching an unsigned suffix."""
    return {
        Decimal(value.replace("\u2212", "-"))
        for value in re.findall(
            r"(?<![\w.\-\u2212])[-\u2212]?\d+(?:\.\d+)?(?![\w.])", paragraph.text()
        )
    }


class PhysicianManagementAuthorityTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            username="scheduling-administrator", password="test-password"
        )
        self.manager.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_invalid_detail_actions_retain_only_their_own_note_and_labels(self):
        physician = create_physician(display_name="Unchanged", scheduler=self.manager)
        self.client.force_login(self.manager)
        url = reverse("physician-detail", args=(physician.pk,))
        initial_history = PhysicianChange.objects.filter(physician=physician).count()
        for action, invalid in (
            ("account", {"user": "not-an-account"}),
            ("capacity_override", {"use_override": "on", "capacity_override": ""}),
            ("identity", {"display_name": ""}),
        ):
            with self.subTest(action=action):
                note = f"Retain {action} note"
                response = self.client.post(url, {"action": action, "reason": note, **invalid})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context[f"{action}_form"].errors)
                parser = _RenderedSurfaceParser()
                parser.feed(response.content.decode())
                for other in ("account", "capacity_override", "identity"):
                    self.assertEqual(response.context[f"{other}_form"].is_bound, other == action)
                    forms = [
                        element
                        for element in parser.elements
                        if element.tag == "form"
                        and any(
                            child.attributes.get("name") == "action"
                            and child.attributes.get("value") == other
                            for child in element.descendants()
                        )
                    ]
                    self.assertEqual(len(forms), 1)
                    controls = [
                        child
                        for child in forms[0].descendants()
                        if child.tag == "textarea" and child.attributes.get("name") == "reason"
                    ]
                    self.assertEqual(len(controls), 1)
                    control = controls[0]
                    self.assertEqual(
                        control.text().removeprefix("\n"), note if other == action else ""
                    )
                    control_id = control.attributes.get("id")
                    self.assertTrue(control_id)
                    self.assertEqual(
                        sum(
                            element.attributes.get("id") == control_id
                            for element in parser.elements
                        ),
                        1,
                    )
                    self.assertTrue(
                        any(
                            child.tag == "label" and child.attributes.get("for") == control_id
                            for child in forms[0].descendants()
                        )
                    )
                    for helper_id in control.attributes.get("aria-describedby", "").split():
                        self.assertEqual(
                            sum(
                                element.attributes.get("id") == helper_id
                                for element in parser.elements
                            ),
                            1,
                        )
                        self.assertTrue(
                            any(
                                child.attributes.get("id") == helper_id
                                for child in forms[0].descendants()
                            )
                        )
        physician.refresh_from_db()
        self.assertEqual(physician.display_name, "Unchanged")
        self.assertIsNone(physician.user_id)
        self.assertIsNone(physician.faculty_precepting_capacity_override)
        self.assertEqual(
            PhysicianChange.objects.filter(physician=physician).count(), initial_history
        )

    def test_migration_creates_the_scheduling_administration_permission_and_group(self):
        permission = Permission.objects.get(
            content_type__app_label="schedules",
            codename="manage_scheduling_administration",
        )

        self.assertEqual(permission.content_type.model, "physician")
        self.assertTrue(
            Group.objects.get(name="Scheduling Administrators")
            .permissions.filter(pk=permission.pk)
            .exists()
        )

    def test_manager_and_superuser_can_mutate_physician_identity_and_roles(self):
        physician = create_physician(display_name="Original", scheduler=self.manager)
        role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.manager,
        )
        correct_physician_identity(
            physician=physician, display_name="Corrected", scheduler=self.manager
        )
        correct_role(
            effective_role=role,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 12, 31),
            scheduler=self.manager,
        )
        superuser = get_user_model().objects.create_superuser(
            username="superuser", password="test-password", email="superuser@example.com"
        )
        end_role(effective_role=role, effective_end=date(2025, 6, 30), scheduler=superuser)

        physician.refresh_from_db()
        role.refresh_from_db()
        self.assertEqual(physician.display_name, "Corrected")
        self.assertEqual(role.effective_end, date(2025, 6, 30))
        self.assertEqual(PhysicianChange.objects.filter(physician=physician).count(), 5)

    def test_ordinary_scheduler_cannot_change_physicians_or_roles(self):
        physician = create_physician(display_name="Protected", scheduler=self.manager)
        role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.manager,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaises(PermissionDenied):
            create_physician(display_name="Denied", scheduler=self.scheduler)
        with self.assertRaises(PermissionDenied):
            correct_physician_identity(
                physician=physician, display_name="Changed", scheduler=self.scheduler
            )
        with self.assertRaises(PermissionDenied):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.CASUAL_ROUNDER,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        with self.assertRaises(PermissionDenied):
            correct_role(
                effective_role=role,
                effective_start=date(2025, 2, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        with self.assertRaises(PermissionDenied):
            end_role(
                effective_role=role, effective_end=date(2025, 12, 31), scheduler=self.scheduler
            )

        physician.refresh_from_db()
        role.refresh_from_db()
        self.assertFalse(Physician.objects.filter(display_name="Denied").exists())
        self.assertEqual(physician.display_name, "Protected")
        self.assertEqual(role.effective_start, date(2025, 1, 1))
        self.assertIsNone(role.effective_end)
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_ordinary_scheduler_retains_scheduling_source_mutations(self):
        faculty = create_physician(display_name="Faculty", scheduler=self.manager)
        community = create_physician(display_name="Community", scheduler=self.manager)
        add_role(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.manager,
        )
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.manager,
        )
        add_role(
            physician=community,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.manager,
        )
        category = create_custom_availability_category(
            name="Teaching", impact="available", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[faculty], scheduler=self.scheduler
        )
        template = create_faculty_availability_template(
            physician=faculty,
            effective_start=None,
            effective_end=None,
            slots={
                "monday_am": {"category_id": category.pk},
                **{
                    slot: "off"
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                    if slot != "monday_am"
                },
            },
            scheduler=self.scheduler,
        )
        monthly_rule = create_monthly_physician_availability_rule(
            physician=faculty,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": category.pk},
            scheduler=self.scheduler,
        )
        community_rule = create_community_preceptor_rule(
            physician=community,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        casual_rule = create_casual_rounder_rule(
            physician=community, weekday=1, ordinals=[1], scheduler=self.scheduler
        )

        self.assertEqual(template.physician, faculty)
        self.assertEqual(monthly_rule.physician, faculty)
        self.assertEqual(community_rule.physician, community)
        self.assertEqual(casual_rule.physician, community)

    def test_http_boundary_hides_controls_denies_mutations_and_allows_manager(self):
        physician = create_physician(display_name="HTTP", scheduler=self.manager)
        self.client.force_login(self.scheduler)

        directory = self.client.get(reverse("physicians-directory"))
        detail = self.client.get(reverse("physician-detail", args=(physician.pk,)))
        self.assertNotContains(directory, "Add physician")
        self.assertNotContains(detail, "Update identity")
        self.assertNotContains(detail, "Add role")
        self.assertEqual(
            self.client.get(reverse("physician-history", args=(physician.pk,))).status_code, 200
        )
        self.assertEqual(self.client.get(reverse("physicians-history")).status_code, 200)
        self.assertEqual(self.client.get(reverse("physician-create")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("physician-role-add", args=(physician.pk,))).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("physician-detail", args=(physician.pk,)),
                {"display_name": "Denied", "reason": "No authority"},
            ).status_code,
            403,
        )
        physician.refresh_from_db()
        self.assertEqual(physician.display_name, "HTTP")
        self.assertEqual(PhysicianChange.objects.filter(physician=physician).count(), 1)

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-01",
                "effective_end": "",
            },
        )
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        self.assertTrue(physician.roles.exists())

    def test_capacity_override_detail_control_requires_manager_and_shows_current_value(self):
        physician = create_physician(display_name="Capacity", scheduler=self.manager)
        detail_url = reverse("physician-detail", args=(physician.pk,))

        def assert_capacity_help(*, control, form, form_instance):
            control_id = control.attributes.get("id")
            self.assertTrue(control_id)
            labels = [
                element
                for element in form.descendants()
                if element.tag == "label" and element.attributes.get("for") == control_id
            ]
            self.assertEqual(len(labels), 1)
            self.assertIn(form_instance.fields["capacity_override"].label, labels[0].text())
            helper_id = f"{control_id}_helptext"
            self.assertIn(helper_id, control.attributes.get("aria-describedby", "").split())
            helpers = [
                element
                for element in form.descendants()
                if element.attributes.get("id") == helper_id
            ]
            self.assertEqual(len(helpers), 1)
            helper_text = helpers[0].text().strip()
            self.assertTrue(helper_text)
            self.assertEqual(helper_text, form_instance.fields["capacity_override"].help_text)

        self.client.force_login(self.scheduler)
        response = self.client.get(detail_url)
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        self.assertFalse(
            any(
                element.tag == "input" and element.attributes.get("name") == "capacity_override"
                for element in parser.elements
            )
        )
        readonly_capacity_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and element.attributes.get("aria-labelledby") == "faculty-capacity-heading"
        ]
        self.assertEqual(len(readonly_capacity_sections), 1)
        readonly_schedule_derived = capacity_summary_paragraphs(
            readonly_capacity_sections[0],
            ("schedule-derived", "schedule derived", "computed", "from schedule"),
        )
        self.assertEqual(len(readonly_schedule_derived), 1)
        self.assertEqual(rendered_capacity_values(readonly_schedule_derived[0]), {Decimal("0")})
        denied = self.client.post(
            detail_url,
            {"action": "capacity_override", "use_override": "on", "capacity_override": "4.5"},
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.manager)
        initial = self.client.get(detail_url)
        parser = _RenderedSurfaceParser()
        parser.feed(initial.content.decode())
        initial_controls = [
            element
            for element in parser.elements
            if element.tag == "input" and element.attributes.get("name") == "capacity_override"
        ]
        self.assertEqual(len(initial_controls), 1)
        self.assertIn("disabled", initial_controls[0].attributes)
        initial_capacity_form = next(
            element
            for element in parser.elements
            if element.tag == "form"
            and any(
                child.attributes.get("name") == "action"
                and child.attributes.get("value") == "capacity_override"
                for child in element.descendants()
            )
        )
        assert_capacity_help(
            control=initial_controls[0],
            form=initial_capacity_form,
            form_instance=initial.context["capacity_override_form"],
        )
        invalid = self.client.post(
            detail_url,
            {
                "action": "capacity_override",
                "use_override": "on",
                "capacity_override": "10.1",
                "reason": "Capacity needs review.",
            },
        )
        self.assertIn("capacity_override", invalid.context["capacity_override_form"].errors)
        parser = _RenderedSurfaceParser()
        parser.feed(invalid.content.decode())
        invalid_controls = [
            element
            for element in parser.elements
            if element.tag == "input" and element.attributes.get("name") == "capacity_override"
        ]
        self.assertEqual(len(invalid_controls), 1)
        self.assertNotIn("disabled", invalid_controls[0].attributes)
        self.assertEqual(invalid_controls[0].attributes.get("aria-invalid"), "true")
        self.assertEqual(invalid_controls[0].attributes.get("value"), "10.1")
        invalid_control_group = next(
            (
                ancestor
                for ancestor in invalid_controls[0].ancestors()
                if "faculty-capacity-override-control"
                in ancestor.attributes.get("class", "").split()
            ),
            None,
        )
        self.assertIsNotNone(invalid_control_group)
        capacity_errors = [
            element
            for element in invalid_control_group.descendants()
            if element.tag == "ul" and "errorlist" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(capacity_errors), 1)
        self.assertTrue(capacity_errors[0].text().strip())
        invalid_capacity_form = next(
            element
            for element in parser.elements
            if element.tag == "form"
            and any(
                child.attributes.get("name") == "action"
                and child.attributes.get("value") == "capacity_override"
                for child in element.descendants()
            )
        )
        assert_capacity_help(
            control=invalid_controls[0],
            form=invalid_capacity_form,
            form_instance=invalid.context["capacity_override_form"],
        )
        invalid_reason = [
            element
            for element in invalid_capacity_form.descendants()
            if element.tag == "textarea" and element.attributes.get("name") == "reason"
        ]
        self.assertEqual(len(invalid_reason), 1)
        self.assertEqual(invalid_reason[0].text().strip(), "Capacity needs review.")
        saved = self.client.post(
            detail_url,
            {
                "action": "capacity_override",
                "use_override": "on",
                "capacity_override": "4.5",
                "reason": "Reduced clinical load.",
            },
        )
        self.assertRedirects(saved, detail_url)
        physician.refresh_from_db()
        self.assertEqual(str(physician.faculty_precepting_capacity_override), "4.5")
        enabled = self.client.get(detail_url)
        parser = _RenderedSurfaceParser()
        parser.feed(enabled.content.decode())
        enabled_controls = [
            element
            for element in parser.elements
            if element.tag == "input" and element.attributes.get("name") == "capacity_override"
        ]
        self.assertEqual(len(enabled_controls), 1)
        self.assertNotIn("disabled", enabled_controls[0].attributes)
        self.assertEqual(enabled_controls[0].attributes.get("value"), "4.5")
        capacity_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and element.attributes.get("aria-labelledby") == "faculty-capacity-heading"
        ]
        self.assertEqual(len(capacity_sections), 1)
        schedule_derived = capacity_summary_paragraphs(
            capacity_sections[0],
            ("schedule-derived", "schedule derived", "computed", "from schedule"),
        )
        current_effective = capacity_summary_paragraphs(
            capacity_sections[0],
            ("current", "effective", "override"),
        )
        self.assertEqual(len(schedule_derived), 1)
        self.assertEqual(len(current_effective), 1)
        self.assertIsNot(schedule_derived[0], current_effective[0])
        self.assertEqual(rendered_capacity_values(schedule_derived[0]), {Decimal("0")})
        self.assertEqual(rendered_capacity_values(current_effective[0]), {Decimal("4.5")})

    def test_physician_detail_groups_authorized_sections_with_physician_details(self):
        physician = create_physician(display_name="Stacked", scheduler=self.manager)
        detail_url = reverse("physician-detail", args=(physician.pk,))
        for user, can_edit_identity in ((self.scheduler, False), (self.manager, True)):
            with self.subTest(user=user):
                self.client.force_login(user)
                parser = _RenderedSurfaceParser()
                parser.feed(self.client.get(detail_url).content.decode())
                details = [
                    element
                    for element in parser.elements
                    if element.attributes.get("aria-label") == "Physician details"
                ]
                self.assertEqual(len(details), 1)
                headings = {
                    element.attributes.get("id"): element
                    for element in details[0].descendants()
                    if element.tag == "h2"
                }
                for heading in ("account-heading", "roles-heading", "scheduling-status-heading"):
                    self.assertIn(heading, headings)
                self.assertEqual("identity-heading" in headings, can_edit_identity)
