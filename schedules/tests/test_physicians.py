from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase

from schedules.forms.physicians import FacultyPreceptingCapacityOverrideForm
from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.services.physicians import (
    add_role,
    correct_faculty_availability_template,
    correct_physician_identity,
    correct_role,
    create_faculty_availability_template,
    create_physician,
    end_role,
    faculty_availability_on,
    faculty_precepting_capacity_for_week,
    physicians_status_on,
    set_faculty_precepting_capacity_override,
    start_later_faculty_availability_template,
)


class PhysicianServiceOwnerTests(SimpleTestCase):
    def test_role_choice_values_and_labels(self):
        self.assertEqual(
            (
                EffectiveRole.Role.COMMUNITY_PRECEPTOR.value,
                EffectiveRole.Role.COMMUNITY_PRECEPTOR.label,
            ),
            ("community_preceptor", "Community Preceptor"),
        )
        self.assertEqual(
            (EffectiveRole.Role.CASUAL_ROUNDER.value, EffectiveRole.Role.CASUAL_ROUNDER.label),
            ("casual_rounder", "Casual Rounder"),
        )
        self.assertEqual(
            (EffectiveRole.Role.FLOAT_PRECEPTOR.value, EffectiveRole.Role.FLOAT_PRECEPTOR.label),
            ("float_preceptor", "Float Preceptor"),
        )
        self.assertEqual(
            (
                EffectiveRole.Role.EXTERNAL_PATIENT_CARE.value,
                EffectiveRole.Role.EXTERNAL_PATIENT_CARE.label,
            ),
            ("external_patient_care", "External Patient Care"),
        )

    def test_capacity_override_form_accepts_tenths_and_requires_a_value_when_enabled(self):
        enabled = FacultyPreceptingCapacityOverrideForm(
            {"use_override": "on", "capacity_override": "4.5"}
        )
        missing = FacultyPreceptingCapacityOverrideForm({"use_override": "on"})
        out_of_range = FacultyPreceptingCapacityOverrideForm(
            {"use_override": "on", "capacity_override": "10.1"}
        )
        disabled = FacultyPreceptingCapacityOverrideForm({"capacity_override": "4.5"})

        self.assertTrue(enabled.is_valid())
        self.assertEqual(enabled.cleaned_data["capacity_override"], Decimal("4.5"))
        self.assertFalse(missing.is_valid())
        self.assertIn("capacity_override", missing.errors)
        self.assertFalse(out_of_range.is_valid())
        self.assertIn("capacity_override", out_of_range.errors)
        self.assertTrue(disabled.is_valid())
        self.assertIsNone(disabled.cleaned_data["capacity_override"])


class PhysiciansSchedulingServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def create_physician(self, name="Alex Kim"):
        return create_physician(display_name=name, scheduler=self.scheduler, reason="Initial entry")

    def test_duplicate_display_names_are_distinct_physicians(self):
        first = self.create_physician()
        second = self.create_physician()

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.display_name, second.display_name)

    def test_current_faculty_capacity_override_is_retained_and_weekly_capacity_is_computed(self):
        physician = self.create_physician()
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )

        self.assertEqual(
            faculty_precepting_capacity_for_week(physician, date(2025, 2, 5)), Decimal("10")
        )
        set_faculty_precepting_capacity_override(
            physician=physician,
            capacity_override=Decimal("4.5"),
            scheduler=self.scheduler,
            reason="Reduced clinical load.",
        )
        physician.refresh_from_db()
        change = PhysicianChange.objects.filter(physician=physician).first()

        self.assertEqual(physician.faculty_precepting_capacity_override, Decimal("4.5"))
        self.assertEqual(
            change.subject, PhysicianChange.Subject.FACULTY_PRECEPTING_CAPACITY_OVERRIDE
        )
        self.assertEqual(change.previous_value, {"faculty_precepting_capacity_override": None})
        self.assertEqual(change.recorded_value, {"faculty_precepting_capacity_override": "4.5"})
        self.assertEqual(change.changed_by, self.scheduler)

        set_faculty_precepting_capacity_override(
            physician=physician, capacity_override=None, scheduler=self.scheduler
        )
        physician.refresh_from_db()
        self.assertIsNone(physician.faculty_precepting_capacity_override)

    def test_roles_validate_inclusive_ranges_and_reject_overlaps(self):
        physician = self.create_physician()
        role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
            reason="January faculty service",
        )

        self.assertTrue(
            physicians_status_on(physician, date(2025, 1, 1)).ordinarily_eligible_to_precept
        )
        self.assertTrue(physicians_status_on(physician, date(2025, 1, 31)).faculty_equivalent)
        self.assertFalse(
            physicians_status_on(physician, date(2025, 2, 1)).ordinarily_eligible_to_precept
        )
        with self.assertRaises(ValidationError):
            add_role(
                physician=physician,
                role=role.role,
                effective_start=date(2025, 1, 31),
                effective_end=None,
                scheduler=self.scheduler,
                reason="Overlapping entry",
            )
        with self.assertRaises(ValidationError):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start=date(2025, 2, 2),
                effective_end=date(2025, 2, 1),
                scheduler=self.scheduler,
                reason="Invalid dates",
            )

    def test_date_specific_role_semantics_and_role_attribute_reasons(self):
        faculty = self.create_physician("Faculty")
        community = self.create_physician("Community")
        casual = self.create_physician("Casual")
        add_role(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Faculty",
        )
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
            reason="Community",
        )
        add_role(
            physician=casual,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Rounding",
        )
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 2, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
            reason="Approved coverage",
        )

        self.assertEqual(
            physicians_status_on(faculty, date(2025, 1, 15)).faculty_equivalent_reason,
            "Effective Faculty role",
        )
        community_before = physicians_status_on(community, date(2025, 1, 15))
        self.assertTrue(community_before.ordinarily_eligible_to_precept)
        self.assertFalse(community_before.faculty_equivalent)
        community_after = physicians_status_on(community, date(2025, 2, 1))
        self.assertTrue(community_after.faculty_equivalent)
        self.assertIn("Community Preceptor role", community_after.faculty_equivalent_reason)
        casual_status = physicians_status_on(casual, date(2025, 1, 15))
        self.assertFalse(casual_status.ordinarily_eligible_to_precept)
        self.assertFalse(casual_status.faculty_equivalent)

    def test_float_role_requires_complete_underlying_coverage(self):
        physician = self.create_physician("Float coverage")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )

        with self.assertRaisesRegex(ValidationError, "Every Float Preceptor date"):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FLOAT_PRECEPTOR,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 2, 1),
                scheduler=self.scheduler,
            )

        without_underlying_role = self.create_physician("Uncovered Float")
        with self.assertRaisesRegex(ValidationError, "Every Float Preceptor date"):
            add_role(
                physician=without_underlying_role,
                role=EffectiveRole.Role.FLOAT_PRECEPTOR,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 1, 31),
                scheduler=self.scheduler,
            )

    def test_float_role_allows_adjacent_underlying_roles_without_extra_attributes(self):
        physician = self.create_physician("Adjacent Float coverage")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 15),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 16),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        float_role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FLOAT_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            faculty_equivalent=True,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )

        self.assertFalse(float_role.faculty_equivalent)
        self.assertEqual(float_role.faculty_leadership, "")
        self.assertEqual(
            set(physicians_status_on(physician, date(2025, 1, 20)).effective_roles),
            {EffectiveRole.Role.COMMUNITY_PRECEPTOR, EffectiveRole.Role.FLOAT_PRECEPTOR},
        )
        self.assertFalse(physicians_status_on(physician, date(2025, 1, 20)).faculty_equivalent)

    def test_underlying_role_correction_cannot_orphan_float_coverage(self):
        physician = self.create_physician("Retained Float coverage")
        faculty = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FLOAT_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "Every Float Preceptor date"):
            correct_role(
                effective_role=faculty,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 1, 30),
                scheduler=self.scheduler,
            )

        faculty.refresh_from_db()
        self.assertEqual(faculty.effective_end, date(2025, 1, 31))
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_external_patient_care_requires_complete_faculty_coverage(self):
        physician = self.create_physician("External patient care coverage")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 15),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 16),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )

        external_role = add_role(
            physician=physician,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            faculty_equivalent=True,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )

        self.assertFalse(external_role.faculty_equivalent)
        self.assertEqual(external_role.faculty_leadership, "")
        status = physicians_status_on(physician, date(2025, 1, 20))
        self.assertEqual(
            set(status.effective_roles),
            {EffectiveRole.Role.FACULTY, EffectiveRole.Role.EXTERNAL_PATIENT_CARE},
        )
        self.assertTrue(status.ordinarily_eligible_to_precept)
        self.assertTrue(status.faculty_equivalent)

        uncovered_physician = self.create_physician("Uncovered external patient care")
        with self.assertRaisesRegex(ValidationError, "Every External Patient Care date"):
            add_role(
                physician=uncovered_physician,
                role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 1, 31),
                scheduler=self.scheduler,
            )
        partially_covered_physician = self.create_physician("Partially covered external care")
        add_role(
            physician=partially_covered_physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        with self.assertRaisesRegex(ValidationError, "Every External Patient Care date"):
            add_role(
                physician=partially_covered_physician,
                role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 2, 1),
                scheduler=self.scheduler,
            )

    def test_faculty_correction_cannot_orphan_external_patient_care(self):
        physician = self.create_physician("Retained external patient care")
        faculty = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "Every External Patient Care date"):
            correct_role(
                effective_role=faculty,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 1, 30),
                scheduler=self.scheduler,
            )

        faculty.refresh_from_db()
        self.assertEqual(faculty.effective_end, date(2025, 1, 31))
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_role_attributes_are_role_specific_and_recorded_in_history(self):
        community = self.create_physician("Community")
        community_role = add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )
        casual = add_role(
            physician=community,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )
        faculty = self.create_physician("Faculty")
        faculty_role = add_role(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )
        other_faculty = self.create_physician("Other faculty")
        add_role(
            physician=other_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            scheduler=self.scheduler,
        )

        self.assertTrue(community_role.faculty_equivalent)
        self.assertEqual(community_role.faculty_leadership, "")
        self.assertFalse(casual.faculty_equivalent)
        self.assertEqual(casual.faculty_leadership, "")
        self.assertFalse(faculty_role.faculty_equivalent)
        self.assertEqual(
            faculty_role.faculty_leadership,
            EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        recorded_value = (
            PhysicianChange.objects.filter(physician=faculty, subject=PhysicianChange.Subject.ROLE)
            .latest("pk")
            .recorded_value
        )
        self.assertEqual(recorded_value["faculty_leadership"], "program_director")
        end_role(
            effective_role=faculty_role,
            effective_end=date(2025, 12, 31),
            scheduler=self.scheduler,
        )
        self.assertEqual(
            PhysicianChange.objects.filter(physician=faculty, subject=PhysicianChange.Subject.ROLE)
            .latest("pk")
            .recorded_value["faculty_leadership"],
            "program_director",
        )

    def test_faculty_category_conflicts_are_symmetric_and_inclusive_but_adjacent_ranges_are_valid(
        self,
    ):
        for first_role, second_role, incompatible_label in (
            (
                EffectiveRole.Role.FACULTY,
                EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                "community-preceptor",
            ),
            (
                EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                EffectiveRole.Role.FACULTY,
                "faculty",
            ),
            (
                EffectiveRole.Role.FACULTY,
                EffectiveRole.Role.CASUAL_ROUNDER,
                "casual-rounder",
            ),
            (
                EffectiveRole.Role.CASUAL_ROUNDER,
                EffectiveRole.Role.FACULTY,
                "faculty",
            ),
        ):
            with self.subTest(first_role=first_role, second_role=second_role):
                physician = self.create_physician(first_role)
                add_role(
                    physician=physician,
                    role=first_role,
                    effective_start=date(2025, 1, 1),
                    effective_end=date(2025, 1, 31),
                    scheduler=self.scheduler,
                )
                history_count = PhysicianChange.objects.count()
                with self.assertRaisesRegex(ValidationError, incompatible_label):
                    add_role(
                        physician=physician,
                        role=second_role,
                        effective_start=date(2025, 1, 31),
                        effective_end=None,
                        scheduler=self.scheduler,
                    )
                self.assertEqual(physician.roles.count(), 1)
                self.assertEqual(PhysicianChange.objects.count(), history_count)

                adjacent = add_role(
                    physician=physician,
                    role=second_role,
                    effective_start=date(2025, 2, 1),
                    effective_end=None,
                    scheduler=self.scheduler,
                )
                self.assertEqual(adjacent.effective_start, date(2025, 2, 1))

    def test_community_and_casual_roles_can_overlap_without_extra_eligibility(self):
        physician = self.create_physician("Community casual")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )

        status = physicians_status_on(physician, date(2025, 1, 15))
        self.assertEqual(
            set(status.effective_roles),
            {EffectiveRole.Role.COMMUNITY_PRECEPTOR, EffectiveRole.Role.CASUAL_ROUNDER},
        )
        self.assertTrue(status.ordinarily_eligible_to_precept)
        self.assertFalse(status.faculty_equivalent)
        self.assertEqual(status.preceptor_reason, "Effective community-preceptor role")

    def test_community_rounder_overlaps_independent_roles_but_not_faculty(self):
        physician = self.create_physician("Community rounder")
        for role in (
            EffectiveRole.Role.COMMUNITY_ROUNDER,
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.CASUAL_ROUNDER,
        ):
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )

        with self.assertRaisesRegex(ValidationError, "community-rounder"):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )

        self.assertEqual(
            set(physicians_status_on(physician, date(2025, 1, 15)).effective_roles),
            {
                EffectiveRole.Role.COMMUNITY_ROUNDER,
                EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                EffectiveRole.Role.CASUAL_ROUNDER,
            },
        )

    def test_physician_creation_with_initial_roles_is_atomic(self):
        history_count = PhysicianChange.objects.count()
        with self.assertRaisesRegex(ValidationError, "community-preceptor"):
            create_physician(
                display_name="Rejected initial roles",
                roles=[
                    {
                        "role": EffectiveRole.Role.FACULTY,
                        "effective_start": date(2025, 1, 1),
                        "effective_end": None,
                    },
                    {
                        "role": EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                        "effective_start": date(2025, 1, 1),
                        "effective_end": None,
                    },
                ],
                scheduler=self.scheduler,
            )

        self.assertFalse(Physician.objects.filter(display_name="Rejected initial roles").exists())
        self.assertEqual(PhysicianChange.objects.count(), history_count)

        physician = create_physician(
            display_name="Initial faculty",
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            scheduler=self.scheduler,
        )
        self.assertEqual(physician.roles.get().role, EffectiveRole.Role.FACULTY)

    def test_role_correction_rejects_category_conflict_without_mutation_or_history(self):
        physician = self.create_physician("Corrected role")
        casual = add_role(
            physician=physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 2, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "community-preceptor"):
            correct_role(
                effective_role=casual,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 15),
                effective_end=date(2025, 2, 15),
                scheduler=self.scheduler,
            )

        casual.refresh_from_db()
        self.assertEqual(casual.role, EffectiveRole.Role.CASUAL_ROUNDER)
        self.assertEqual(casual.effective_start, date(2025, 1, 1))
        self.assertEqual(casual.effective_end, date(2025, 1, 31))
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_corrections_and_deactivation_retain_prior_value_and_provenance(self):
        physician = self.create_physician("Original name")
        role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Original role",
        )
        correct_physician_identity(
            physician=physician,
            display_name="Corrected name",
            scheduler=self.scheduler,
            reason="Correcting prior record",
        )
        end_role(
            effective_role=role,
            effective_end=date(2024, 12, 31),
            scheduler=self.scheduler,
            reason="Historical correction",
        )

        role_change = PhysicianChange.objects.filter(
            physician=physician, subject=PhysicianChange.Subject.ROLE
        ).latest("pk")
        self.assertEqual(role_change.previous_value["effective_end"], None)
        self.assertEqual(role_change.recorded_value["effective_end"], "2024-12-31")
        self.assertEqual(role_change.changed_by, self.scheduler)
        self.assertEqual(role_change.reason, "Historical correction")
        self.assertFalse(
            physicians_status_on(physician, date(2025, 1, 1)).ordinarily_eligible_to_precept
        )
        self.assertTrue(EffectiveRole.objects.filter(pk=role.pk).exists())

    def test_physicians_changes_require_an_authenticated_scheduler(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        with self.assertRaises(PermissionDenied):
            create_physician(display_name="No record", scheduler=anonymous, reason="No access")


class FacultyAvailabilityServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = create_physician(
            display_name="Faculty", scheduler=self.scheduler, reason="Initial entry"
        )
        add_role(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Faculty service",
        )

    def slots(self, **overrides):
        values = {
            slot: FacultyAvailabilityTemplate.AvailabilityState.OFF
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        values.update(overrides)
        return values

    def create_template(self, **overrides):
        values = {
            "physician": self.physician,
            "effective_start": None,
            "effective_end": None,
            "slots": self.slots(),
            "scheduler": self.scheduler,
            "reason": "Initial Faculty Schedule",
        }
        values.update(overrides)
        return create_faculty_availability_template(**values)

    def test_resolves_all_template_states_with_manual_template_provenance(self):
        template = self.create_template(
            slots=self.slots(
                monday_am=FacultyAvailabilityTemplate.AvailabilityState.OPEN,
                tuesday_pm=FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
                wednesday_am=FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE,
            )
        )

        available = faculty_availability_on(self.physician, date(2025, 1, 6), "AM")
        backup_only = faculty_availability_on(self.physician, date(2025, 1, 7), "PM")
        patient_care = faculty_availability_on(self.physician, date(2025, 1, 8), "AM")
        not_available = faculty_availability_on(self.physician, date(2025, 1, 9), "AM")

        self.assertEqual(available.base_availability, "open")
        self.assertEqual(available.source, "manual")
        self.assertEqual(available.supporting_template, template)
        self.assertTrue(available.faculty_eligible)
        self.assertEqual(backup_only.base_availability, "backup_only")
        self.assertTrue(backup_only.backup_only)
        self.assertEqual(patient_care.base_availability, "patient_care")
        self.assertEqual(patient_care.base_availability_display, "Patient care")
        self.assertEqual(not_available.base_availability, "off")

    def test_weekend_availability_is_not_applicable_with_or_without_a_template(self):
        self.create_template(slots=self.slots(monday_am="open"))
        no_template_physician = Physician.objects.create(display_name="No template faculty")
        EffectiveRole.objects.create(
            physician=no_template_physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        before = (
            SchedulePeriod.objects.count(),
            PreceptorSession.objects.count(),
            PreceptorAssignment.objects.count(),
            PhysicianChange.objects.count(),
        )

        for on_date, physician in (
            (date(2025, 1, 4), self.physician),
            (date(2025, 1, 5), no_template_physician),
        ):
            with self.subTest(on_date=on_date, physician=physician):
                result = faculty_availability_on(physician, on_date, "AM")
                self.assertFalse(result.is_applicable)
                self.assertIsNone(result.base_availability)
                self.assertEqual(
                    result.base_availability_display,
                    "Not applicable — no weekend preceptor session",
                )
                self.assertFalse(result.backup_only)
                self.assertIsNone(result.source)
                self.assertIsNone(result.supporting_template)
                self.assertTrue(result.faculty_eligible)

        self.assertEqual(
            (
                SchedulePeriod.objects.count(),
                PreceptorSession.objects.count(),
                PreceptorAssignment.objects.count(),
                PhysicianChange.objects.count(),
            ),
            before,
        )

    def test_blank_bounded_and_midweek_ranges_resolve_inclusively(self):
        unbounded = self.create_template(
            effective_end=date(2025, 1, 8),
            slots=self.slots(wednesday_pm=FacultyAvailabilityTemplate.AvailabilityState.OPEN),
        )

        self.assertEqual(
            faculty_availability_on(self.physician, date(2024, 12, 30), "AM").supporting_template,
            unbounded,
        )
        self.assertEqual(
            faculty_availability_on(self.physician, date(2025, 1, 8), "PM").base_availability,
            "open",
        )
        self.assertEqual(
            faculty_availability_on(self.physician, date(2025, 1, 9), "AM").base_availability,
            "off",
        )

    def test_non_overlapping_templates_and_invalid_submissions_are_atomic(self):
        first = self.create_template(
            effective_start=date(2025, 1, 1), effective_end=date(2025, 1, 31)
        )
        second = self.create_template(
            effective_start=date(2025, 2, 1), effective_end=None, reason="February pattern"
        )
        history_count = PhysicianChange.objects.filter(
            physician=self.physician, subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE
        ).count()

        self.assertNotEqual(first.pk, second.pk)
        for values in (
            {"effective_start": date(2025, 1, 31), "effective_end": None, "slots": self.slots()},
            {
                "effective_start": date(2025, 3, 1),
                "effective_end": date(2025, 2, 28),
                "slots": self.slots(),
            },
            {
                "effective_start": date(2025, 3, 1),
                "effective_end": None,
                "slots": self.slots(monday_am="unknown"),
            },
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.create_template(**values)

        self.assertEqual(
            FacultyAvailabilityTemplate.objects.filter(physician=self.physician).count(), 2
        )
        self.assertEqual(
            PhysicianChange.objects.filter(
                physician=self.physician,
                subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
            ).count(),
            history_count,
        )

    def test_no_template_defaults_to_not_available_and_role_eligibility_is_separate(self):
        no_template = faculty_availability_on(self.physician, date(2025, 1, 6), "AM")
        self.assertTrue(no_template.is_applicable)
        self.assertEqual(no_template.base_availability, "off")
        self.assertTrue(no_template.faculty_eligible)

        template = self.create_template(slots=self.slots(monday_am="open"))
        role = self.physician.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=role,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
            reason="Corrected historical faculty role",
        )
        result = faculty_availability_on(self.physician, date(2025, 2, 3), "AM")
        self.assertEqual(result.supporting_template, template)
        self.assertEqual(result.base_availability, "open")
        self.assertFalse(result.faculty_eligible)

    def test_correction_retains_prior_values_and_unchanged_submission_is_idempotent(self):
        role = self.physician.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=role,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Corrected historical faculty role",
        )
        template = self.create_template(
            effective_start=date(2024, 1, 1),
            slots=self.slots(monday_am="open"),
        )
        corrected_slots = self.slots(tuesday_pm="patient_care")
        correct_faculty_availability_template(
            template=template,
            effective_start=date(2024, 1, 1),
            effective_end=date(2024, 12, 31),
            slots=corrected_slots,
            scheduler=self.scheduler,
            reason="Historical correction",
        )
        change = PhysicianChange.objects.filter(
            physician=self.physician, subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE
        ).latest("pk")
        self.assertEqual(change.previous_value["effective_end"], None)
        self.assertEqual(change.previous_value["slots"]["monday_am"], {"base_state": "open"})
        self.assertEqual(change.recorded_value["effective_end"], "2024-12-31")
        self.assertEqual(
            change.recorded_value["slots"]["tuesday_pm"], {"base_state": "patient_care"}
        )
        self.assertEqual(change.changed_by, self.scheduler)
        self.assertEqual(change.reason, "Historical correction")

        history_count = PhysicianChange.objects.filter(
            physician=self.physician, subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE
        ).count()
        correct_faculty_availability_template(
            template=template,
            effective_start=date(2024, 1, 1),
            effective_end=date(2024, 12, 31),
            slots=corrected_slots,
            scheduler=self.scheduler,
            reason="Repeated submit",
        )
        self.assertEqual(
            PhysicianChange.objects.filter(
                physician=self.physician,
                subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
            ).count(),
            history_count,
        )

    def test_start_later_template_corrects_the_latest_template_and_records_both_changes(self):
        preceding = self.create_template(
            effective_start=date(2025, 1, 1),
            slots=self.slots(monday_am="open"),
        )

        later = start_later_faculty_availability_template(
            template=preceding,
            effective_start=date(2025, 2, 1),
            slots=self.slots(tuesday_pm="patient_care"),
            scheduler=self.scheduler,
            reason="February pattern",
        )

        preceding.refresh_from_db()
        self.assertEqual(preceding.effective_end, date(2025, 1, 31))
        self.assertEqual((later.effective_start, later.effective_end), (date(2025, 2, 1), None))
        self.assertEqual(later.slots["tuesday_pm"], {"base_state": "patient_care"})
        correction, creation = PhysicianChange.objects.filter(
            physician=self.physician,
            subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
        ).order_by("pk")[1:]
        self.assertEqual(correction.subject_id, preceding.pk)
        self.assertEqual(correction.previous_value["effective_end"], None)
        self.assertEqual(correction.recorded_value["effective_end"], "2025-01-31")
        self.assertEqual(creation.subject_id, later.pk)
        self.assertIsNone(creation.previous_value)
        self.assertEqual(creation.recorded_value["effective_start"], "2025-02-01")

    def test_start_later_rejects_invalid_or_stale_templates_without_changing_the_timeline(self):
        first = self.create_template(
            effective_start=date(2025, 1, 1), effective_end=date(2025, 1, 31)
        )
        latest = self.create_template(effective_start=date(2025, 2, 1))

        def state():
            return (
                list(
                    FacultyAvailabilityTemplate.objects.filter(physician=self.physician)
                    .order_by("effective_start", "pk")
                    .values_list("pk", "effective_start", "effective_end", "slots")
                ),
                list(
                    PhysicianChange.objects.filter(physician=self.physician)
                    .order_by("pk")
                    .values_list("subject_id", "previous_value", "recorded_value")
                ),
            )

        before = state()
        for template, transition, slots in (
            (first, date(2025, 1, 15), self.slots()),
            (latest, date(2025, 2, 1), self.slots()),
            (latest, date(2025, 3, 1), self.slots(monday_am="unknown")),
        ):
            with self.subTest(template=template.pk, transition=transition):
                with self.assertRaises(ValidationError):
                    start_later_faculty_availability_template(
                        template=template,
                        effective_start=transition,
                        slots=slots,
                        scheduler=self.scheduler,
                    )
                self.assertEqual(state(), before)

    def test_templates_require_a_faculty_record_and_an_authenticated_scheduler(self):
        nonfaculty = create_physician(
            display_name="Not faculty", scheduler=self.scheduler, reason="Initial entry"
        )
        with self.assertRaises(ValidationError):
            self.create_template(physician=nonfaculty)
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        with self.assertRaises(PermissionDenied):
            self.create_template(scheduler=anonymous)
        self.assertEqual(FacultyAvailabilityTemplate.objects.count(), 0)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_historical_template_can_retain_an_unassigned_option_but_cannot_extend(self, now):
        from schedules.services.availability_categories import (
            create_custom_availability_category,
            set_custom_availability_category_physicians,
        )

        category = create_custom_availability_category(
            name="Retained option", impact="available", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.physician], scheduler=self.scheduler
        )
        template = self.create_template(slots=self.slots(monday_am={"category_id": category.pk}))
        assignment = category.assignments.get(physician=self.physician)
        assignment.is_active = False
        assignment.save(update_fields=("is_active",))

        correct_faculty_availability_template(
            template=template,
            effective_start=None,
            effective_end=date(2024, 12, 31),
            slots=self.slots(monday_am={"category_id": category.pk}),
            scheduler=self.scheduler,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "Current and future schedule facts"):
            correct_faculty_availability_template(
                template=template,
                effective_start=None,
                effective_end=None,
                slots=self.slots(monday_am={"category_id": category.pk}),
                scheduler=self.scheduler,
            )

        template.refresh_from_db()
        self.assertEqual(template.effective_end, date(2024, 12, 31))
        self.assertEqual(PhysicianChange.objects.count(), history_count)
