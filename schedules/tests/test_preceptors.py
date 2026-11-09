from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from schedules.forms.late_docs import LateDocAssignmentForm
from schedules.forms.preceptors import AssignmentPhysicianChoiceField
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    Physician,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
    Vacation,
)
from schedules.services.academic_events import create_academic_event_occurrence
from schedules.services.community import create_community_preceptor_rule
from schedules.services.finding_acknowledgements import (
    acknowledge_finding,
    acknowledgement_for_finding,
    acknowledgements_for_findings,
    finding_identity,
    readiness_for_findings,
    remove_finding_acknowledgement,
)
from schedules.services.physicians import (
    add_role,
    correct_role,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
    end_role,
)
from schedules.services.preceptors import (
    DraftFinding,
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_finding_label,
    draft_findings_for_month,
    draft_findings_for_session,
    draft_findings_for_week,
    open_monthly_draft,
    open_weekly_draft,
    remove_preceptor_assignment,
    session_assignments,
    set_preceptor_assignment_lock,
    work_week_start,
)
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.vacations import create_vacation


class DraftFindingDefinitionTests(TestCase):
    def test_compact_label_is_owned_by_the_finding_definition(self):
        finding = DraftFinding(
            "missing_primary_position", "hard", None, None, (), {"position": "primary-1"}
        )

        self.assertEqual(draft_finding_label(finding.code), "Unfilled")
        self.assertEqual(finding.label, "Unfilled")
        self.assertEqual(finding.message, "Preceptor 1 is unfilled.")


class FindingAcknowledgementTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.other_session = self.session.period.sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.PM
        )
        self.faculty = self.preceptor("Faculty", EffectiveRole.Role.FACULTY)
        self.community = self.preceptor("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.replacement = self.preceptor("Replacement", EffectiveRole.Role.COMMUNITY_PRECEPTOR)

    def preceptor(self, name, role):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=role,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        return physician

    def assign(self, session, physician, position, *, override=False):
        return assign_preceptor(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=position,
            scheduler=self.scheduler,
            override=override,
        )

    def finding(self, code):
        return next(
            finding
            for finding in draft_findings_for_session(self.session).findings
            if finding.code == code and finding.severity == "hard"
        )

    def test_slot_acknowledgement_is_structured_idempotent_and_retained(self):
        finding = self.finding("missing_primary_position")
        identity = finding_identity(finding)

        self.assertEqual(identity.owner, {"kind": "preceptor_session", "id": self.session.pk})
        self.assertEqual(identity.occurrence["position"], "primary-1")
        self.assertEqual(identity.code, "missing_primary_position")
        self.assertEqual(identity.material_values, {})

        acknowledgement = acknowledge_finding(
            finding=finding, scheduler=self.scheduler, reason="  Covered by external staffing.  "
        )
        repeated = acknowledge_finding(
            finding=finding, scheduler=self.scheduler, reason="A later duplicate request"
        )

        self.assertEqual(acknowledgement.pk, repeated.pk)
        self.assertEqual(acknowledgement.reason, "Covered by external staffing.")
        self.assertEqual(acknowledgement.acknowledged_by, self.scheduler)
        self.assertIsNotNone(acknowledgement.acknowledged_at)
        self.assertTrue(acknowledgement.is_current)
        self.assertEqual(FindingAcknowledgement.objects.count(), 1)
        self.assertEqual(acknowledgements_for_findings((finding,))[identity.key], acknowledgement)
        with self.assertRaisesRegex(ValidationError, "cannot be deleted"):
            acknowledgement.delete()

    def test_blank_reason_and_non_hard_finding_are_rejected(self):
        finding = self.finding("missing_primary_position")
        with self.assertRaisesRegex(ValidationError, "nonblank reason"):
            acknowledge_finding(finding=finding, scheduler=self.scheduler, reason=" \n ")

        advisory = DraftFinding(
            "backup_only_assignment",
            "preference",
            self.session,
            None,
            (),
            {"position": "primary-1"},
        )
        with self.assertRaisesRegex(ValidationError, "Only current hard findings"):
            acknowledge_finding(finding=advisory, scheduler=self.scheduler, reason="Not blocking")

    def test_removing_a_current_acknowledgement_retains_provenance_and_restores_readiness(self):
        finding = self.finding("missing_primary_position")
        acknowledgement = acknowledge_finding(
            finding=finding,
            scheduler=self.scheduler,
            reason="Temporary external coverage.",
        )
        other_finding = next(
            candidate
            for candidate in draft_findings_for_session(self.session).findings
            if candidate.code == "missing_primary_position"
            and candidate.explanation["position"] == "primary-2"
        )
        other_acknowledgement = acknowledge_finding(
            finding=other_finding,
            scheduler=self.scheduler,
            reason="Another coverage exception.",
        )

        with self.assertRaisesRegex(ValidationError, "no longer current"):
            remove_finding_acknowledgement(
                finding=finding,
                acknowledgement_id=other_acknowledgement.pk,
                scheduler=self.scheduler,
            )
        acknowledgement.refresh_from_db()
        other_acknowledgement.refresh_from_db()
        self.assertTrue(acknowledgement.is_current)
        self.assertTrue(other_acknowledgement.is_current)

        removed = remove_finding_acknowledgement(
            finding=finding,
            acknowledgement_id=acknowledgement.pk,
            scheduler=self.scheduler,
        )

        acknowledgement.refresh_from_db()
        self.assertEqual(removed.pk, acknowledgement.pk)
        self.assertFalse(acknowledgement.is_current)
        self.assertEqual(acknowledgement.reason, "Temporary external coverage.")
        self.assertEqual(acknowledgement.acknowledged_by, self.scheduler)
        self.assertEqual(acknowledgement.removed_by, self.scheduler)
        self.assertIsNotNone(acknowledgement.removed_at)
        self.assertEqual(acknowledgement.removed_at, acknowledgement.no_longer_applies_at)
        self.assertTrue(acknowledgement.was_deliberately_removed)
        self.assertIsNone(acknowledgement_for_finding(finding))
        self.assertEqual(
            readiness_for_findings((finding,)).unacknowledged_hard_findings, (finding,)
        )

        with self.assertRaisesRegex(ValidationError, "no longer current"):
            remove_finding_acknowledgement(
                finding=finding,
                acknowledgement_id=acknowledgement.pk,
                scheduler=self.scheduler,
            )

    def test_resolution_retires_a_slot_acknowledgement_and_recurrence_needs_a_new_one(self):
        acknowledgement = acknowledge_finding(
            finding=self.finding("missing_primary_position"),
            scheduler=self.scheduler,
            reason="Temporary coverage gap.",
        )
        self.assign(self.other_session, self.faculty, 1)
        acknowledgement.refresh_from_db()
        self.assertTrue(acknowledgement.is_current)

        assignment = self.assign(self.session, self.faculty, 1)
        acknowledgement.refresh_from_db()
        self.assertIsNotNone(acknowledgement.no_longer_applies_at)
        self.assertFalse(acknowledgement.is_current)

        remove_preceptor_assignment(assignment=assignment, scheduler=self.scheduler)
        recurring = self.finding("missing_primary_position")
        self.assertIsNone(acknowledgement_for_finding(recurring))

        replacement = acknowledge_finding(
            finding=recurring, scheduler=self.scheduler, reason="The gap returned."
        )
        self.assertNotEqual(replacement.pk, acknowledgement.pk)
        self.assertEqual(FindingAcknowledgement.objects.count(), 2)

    def test_removing_an_acknowledgement_that_no_longer_applies_preserves_automatic_retirement(
        self,
    ):
        finding = self.finding("missing_primary_position")
        acknowledgement = acknowledge_finding(
            finding=finding,
            scheduler=self.scheduler,
            reason="Temporary coverage gap.",
        )

        self.assign(self.session, self.faculty, 1)

        acknowledgement.refresh_from_db()
        self.assertFalse(acknowledgement.is_current)
        self.assertIsNone(acknowledgement.removed_at)
        self.assertIsNone(acknowledgement.removed_by)
        with self.assertRaisesRegex(ValidationError, "no longer current"):
            remove_finding_acknowledgement(
                finding=finding,
                acknowledgement_id=acknowledgement.pk,
                scheduler=self.scheduler,
            )
        acknowledgement.refresh_from_db()
        self.assertIsNone(acknowledgement.removed_at)
        self.assertIsNone(acknowledgement.removed_by)

    def test_material_composition_change_retires_the_acknowledgement(self):
        self.assign(self.session, self.faculty, 1)
        self.assign(self.session, self.community, 2)
        finding = self.finding("insufficient_faculty_equivalent_coverage")
        acknowledgement = acknowledge_finding(
            finding=finding, scheduler=self.scheduler, reason="Community coverage is intentional."
        )

        self.assign(self.session, self.replacement, 2)
        acknowledgement.refresh_from_db()

        self.assertEqual(
            finding_identity(finding).material_values,
            {"assigned_physician_ids": [self.faculty.pk, self.community.pk]},
        )
        self.assertIsNotNone(acknowledgement.no_longer_applies_at)
        self.assertIsNone(
            acknowledgement_for_finding(self.finding("insufficient_faculty_equivalent_coverage"))
        )

    def test_holiday_identity_excludes_the_displayed_closure_text(self):
        holiday_session = open_monthly_draft(date(2026, 12, 1)).sessions.get(
            date=date(2026, 12, 24), period_of_day=PreceptorSession.PeriodOfDay.PM
        )
        assignment = PreceptorAssignment.objects.create(
            session=holiday_session,
            physician=self.faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        finding = draft_findings_for_session(holiday_session).findings[0]

        self.assertEqual(finding.code, "holiday_closed_assignment")
        self.assertEqual(
            finding_identity(finding).material_values,
            {"assignment_id": assignment.pk, "physician_ids": [self.faculty.pk]},
        )

    def test_replacing_an_availability_source_retires_the_acknowledgement(self):
        slots = {slot: "off" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        template = create_faculty_availability_template(
            physician=self.faculty,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        self.assign(self.session, self.faculty, 1, override=True)
        finding = self.finding("not_available_assignment")
        acknowledgement = acknowledge_finding(
            finding=finding,
            scheduler=self.scheduler,
            reason="The weekly template is temporarily unavailable.",
        )

        self.assertEqual(
            finding_identity(finding).material_values["availability_sources"],
            [{"source_type": "faculty_template", "source_id": template.pk}],
        )

        monthly_rule = create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        acknowledgement.refresh_from_db()
        replacement = self.finding("not_available_assignment")

        self.assertIsNotNone(acknowledgement.no_longer_applies_at)
        self.assertEqual(
            finding_identity(replacement).material_values["availability_sources"],
            [{"source_type": "monthly_availability_rule", "source_id": monthly_rule.pk}],
        )
        self.assertIsNone(acknowledgement_for_finding(replacement))
        self.assertEqual(
            readiness_for_findings((replacement,)).unacknowledged_hard_findings,
            (replacement,),
        )


class OpenMonthlyDraftTests(TestCase):
    def test_creates_each_weekday_am_and_pm_and_is_idempotent(self):
        period = open_monthly_draft(date(2025, 2, 18))

        self.assertEqual(period.month, date(2025, 2, 1))
        self.assertEqual(period.sessions.count(), 40)
        self.assertEqual(
            set(period.sessions.values_list("period_of_day", flat=True)),
            {PreceptorSession.PeriodOfDay.AM, PreceptorSession.PeriodOfDay.PM},
        )
        self.assertFalse(period.sessions.filter(date__week_day__in=(1, 7)).exists())

        self.assertEqual(open_monthly_draft(date(2025, 2, 1)).pk, period.pk)
        self.assertEqual(SchedulePeriod.objects.count(), 1)
        self.assertEqual(PreceptorSession.objects.count(), 40)

    def test_handles_short_long_and_leap_year_months(self):
        cases = {
            date(2025, 2, 1): 40,
            date(2025, 8, 1): 42,
            date(2024, 2, 1): 42,
        }

        for month, expected_session_count in cases.items():
            with self.subTest(month=month):
                self.assertEqual(open_monthly_draft(month).sessions.count(), expected_session_count)


class OpenWeeklyDraftTests(TestCase):
    def test_normalizes_to_monday_and_creates_the_ten_shared_weekday_sessions(self):
        sessions = open_weekly_draft(date(2025, 1, 1))

        self.assertEqual(work_week_start(date(2025, 1, 1)), date(2024, 12, 30))
        self.assertEqual(len(sessions), 10)
        self.assertEqual(
            {session.date for session in sessions},
            {
                date(2024, 12, 30),
                date(2024, 12, 31),
                date(2025, 1, 1),
                date(2025, 1, 2),
                date(2025, 1, 3),
            },
        )
        self.assertEqual(SchedulePeriod.objects.count(), 2)
        self.assertEqual(
            PreceptorSession.objects.filter(
                date__range=(date(2024, 12, 30), date(2025, 1, 3))
            ).count(),
            10,
        )

        repeated = open_weekly_draft(date(2024, 12, 30))
        self.assertEqual([session.pk for session in repeated], [session.pk for session in sessions])
        self.assertEqual(
            PreceptorSession.objects.filter(
                date__range=(date(2024, 12, 30), date(2025, 1, 3))
            ).count(),
            10,
        )


class ManualDraftAssignmentServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.faculty = self.preceptor("Faculty", EffectiveRole.Role.FACULTY)
        self.community = self.preceptor("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.other = self.preceptor("Other", EffectiveRole.Role.FACULTY)

    def preceptor(self, name, role):
        physician = create_physician(
            display_name=name, scheduler=self.scheduler, reason="Initial entry"
        )
        add_role(
            physician=physician,
            role=role,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Preceptor roster",
        )
        return physician

    def assign(
        self,
        physician,
        responsibility="primary",
        primary_position=1,
        reason="Manual assignment",
        override=False,
    ):
        return assign_preceptor(
            session=self.session,
            physician=physician,
            responsibility=responsibility,
            primary_position=primary_position,
            scheduler=self.scheduler,
            reason=reason,
            override=override,
        )

    def test_empty_and_incomplete_draft_positions_are_saved_without_validation(self):
        self.assertEqual(session_assignments(self.session), ())

        self.assign(self.faculty)

        self.assertEqual(self.session.assignments.count(), 1)
        self.assertEqual(session_assignments(self.session)[0].responsibility, "primary")

    def test_explicit_primary_secondary_slots_and_date_specific_role_attribute_query(self):
        correct_role(
            effective_role=self.community.roles.get(),
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
            reason="Coverage role attribute",
        )
        self.assign(self.community, primary_position=1)
        self.assign(self.other, primary_position=2)
        self.assign(self.faculty, responsibility="secondary", primary_position=None)

        statuses = session_assignments(self.session)

        self.assertEqual(
            [(status.responsibility, status.primary_position) for status in statuses],
            [("primary", 1), ("primary", 2), ("secondary", None)],
        )
        self.assertTrue(statuses[2].faculty_equivalent)
        self.assertTrue(statuses[2].ordinarily_eligible_to_precept)

    def test_secondary_rejects_a_faculty_equivalent_community_preceptor_without_history(self):
        correct_role(
            effective_role=self.community.roles.get(),
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
            reason="Coverage role attribute",
        )
        history_count = PreceptorAssignmentChange.objects.count()

        with self.assertRaisesRegex(
            ValidationError, "Secondary assignments require an effective Faculty"
        ):
            self.assign(self.community, responsibility="secondary", primary_position=None)

        self.assertFalse(self.session.assignments.exists())
        self.assertEqual(PreceptorAssignmentChange.objects.count(), history_count)

    def test_replacement_removal_and_lock_changes_retain_prior_values_and_are_idempotent(self):
        assignment = self.assign(self.faculty)
        assignment = set_preceptor_assignment_lock(
            assignment=assignment, is_locked=True, scheduler=self.scheduler, reason="Keep visible"
        )
        replacement = self.assign(self.community, reason="Correct selection")
        set_preceptor_assignment_lock(
            assignment=replacement, is_locked=True, scheduler=self.scheduler, reason="Repeated lock"
        )
        remove_preceptor_assignment(
            assignment=replacement, scheduler=self.scheduler, reason="No longer needed"
        )

        changes = list(
            PreceptorAssignmentChange.objects.filter(session=self.session).order_by("pk")
        )
        self.assertEqual(
            [change.action for change in changes],
            ["assigned", "lock_changed", "assigned", "removed"],
        )
        self.assertEqual(changes[2].previous_value["physician_id"], self.faculty.pk)
        self.assertEqual(changes[2].recorded_value["physician_id"], self.community.pk)
        self.assertTrue(changes[3].previous_value["is_locked"])
        self.assertIsNone(changes[3].recorded_value)
        self.assertEqual(changes[3].changed_by, self.scheduler)
        self.assertEqual(changes[3].reason, "No longer needed")
        self.assertEqual(self.session.assignments.count(), 0)

    def test_duplicate_and_invalid_slot_rejections_are_atomic(self):
        self.assign(self.faculty)
        assignment_count = self.session.assignments.count()
        history_count = PreceptorAssignmentChange.objects.count()

        for values in (
            {"physician": self.faculty, "responsibility": "primary", "primary_position": 2},
            {"physician": self.other, "responsibility": "primary", "primary_position": 3},
            {"physician": self.other, "responsibility": "secondary", "primary_position": 1},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.assign(**values)

        self.assertEqual(self.session.assignments.count(), assignment_count)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), history_count)

    def test_crafted_ineligible_create_is_rejected_without_assignment_or_history(self):
        ineligible = create_physician(
            display_name="Crafted ineligible", scheduler=self.scheduler, reason="Initial entry"
        )

        with self.assertRaises(ValidationError):
            self.assign(ineligible)

        self.assertEqual(self.session.assignments.count(), 0)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), 0)

    def test_explicit_override_accepts_ineligible_primary_and_nonfaculty_secondary(self):
        ineligible = create_physician(
            display_name="Authoritative primary", scheduler=self.scheduler, reason="Initial entry"
        )

        with self.assertRaises(ValidationError):
            self.assign(ineligible)

        primary = self.assign(ineligible, override=True)
        secondary = self.assign(
            self.community,
            responsibility="secondary",
            primary_position=None,
            override=True,
        )

        self.assertEqual(primary.physician, ineligible)
        self.assertEqual(secondary.physician, self.community)
        self.assertEqual(self.session.assignments.count(), 2)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), 2)

    def test_explicit_override_retains_slot_validation(self):
        with self.assertRaisesRegex(ValidationError, "two primary positions"):
            self.assign(self.faculty, primary_position=3, override=True)

        self.assertFalse(self.session.assignments.exists())
        self.assertEqual(PreceptorAssignmentChange.objects.count(), 0)

    def test_stale_ineligible_replacement_is_rejected_without_assignment_or_history_change(self):
        assignment = self.assign(self.faculty)
        stale = self.preceptor("Stale", EffectiveRole.Role.FACULTY)
        stale_role = stale.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=stale_role,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        history_count = PreceptorAssignmentChange.objects.count()

        with self.assertRaises(ValidationError):
            self.assign(stale, reason="Stale replacement")

        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, self.faculty)
        self.assertEqual(self.session.assignments.count(), 1)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), history_count)

    def test_assignment_survives_later_role_correction_and_reports_current_inconsistency(self):
        assignment = self.assign(self.faculty)
        faculty_role = self.faculty.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=faculty_role,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )

        status = session_assignments(self.session)[0]

        self.assertEqual(status.assignment.pk, assignment.pk)
        self.assertEqual(status.physician, self.faculty)
        self.assertEqual(status.responsibility, "primary")
        self.assertFalse(status.ordinarily_eligible_to_precept)

    def test_past_correction_and_authenticated_scheduler_requirement(self):
        past_session = open_monthly_draft(date(2024, 2, 1)).sessions.get(
            date=date(2024, 2, 1), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        historical = create_physician(
            display_name="Historical", scheduler=self.scheduler, reason="Initial entry"
        )
        add_role(
            physician=historical,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Historical roster",
        )
        assignment = assign_preceptor(
            session=past_session,
            physician=historical,
            responsibility="primary",
            primary_position=1,
            scheduler=self.scheduler,
            reason="Historical correction",
        )
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        self.assertEqual(assignment.physician, historical)
        with self.assertRaises(PermissionDenied):
            set_preceptor_assignment_lock(
                assignment=assignment, is_locked=True, scheduler=anonymous, reason="Unauthorized"
            )
        self.assertFalse(PreceptorAssignment.objects.get(pk=assignment.pk).is_locked)


class AssignmentAlternativesServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )

    def physician(self, name, role=None):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        if role:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        return physician

    def faculty(self, name, availability="open"):
        physician = self.physician(name, EffectiveRole.Role.FACULTY)
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def community(self, name):
        return self.physician(name, EffectiveRole.Role.COMMUNITY_PRECEPTOR)

    def test_returns_all_four_tiers_with_explanations_and_stable_ordering(self):
        expected = self.community("Expected Community")
        create_community_preceptor_rule(
            physician=expected,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )

        community_casual = self.community("Casual Community")
        add_role(
            physician=community_casual,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community_casual,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        create_casual_rounder_rule(
            physician=community_casual,
            weekday=0,
            ordinals=[1],
            scheduler=self.scheduler,
        )

        self.faculty("alpha faculty")
        tie_first = self.faculty("Same Name")
        tie_second = self.faculty("same name")
        self.faculty("Backup faculty", availability="backup_only")
        patient_care = self.faculty("Patient care faculty", availability="patient_care")
        normal_rounder = self.faculty("Normal rounder")
        casual = self.physician("Casual cover")
        create_casual_rounder_rule(
            physician=casual,
            weekday=0,
            ordinals=[1],
            scheduler=self.scheduler,
        )
        record_normal_rounder(
            week=date(2025, 2, 3), physician=normal_rounder, scheduler=self.scheduler
        )

        blocked = self.community("Blocked expected")
        blocked_rule = create_community_preceptor_rule(
            physician=blocked,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        blocked_vacation = create_vacation(
            physician=blocked,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        self.community("Unmatched community")
        self.faculty("Unavailable faculty", availability="off")

        result = assignment_alternatives_for_session(
            self.session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )

        self.assertEqual(
            [tier.key for tier in result.tiers],
            [
                "available_expected_community",
                "available_other",
                "backup_only",
                "not_available",
            ],
        )
        self.assertEqual(
            [alternative.physician.display_name for alternative in result.tiers[0].alternatives],
            ["Casual Community", "Expected Community"],
        )
        self.assertEqual(
            [alternative.physician.display_name for alternative in result.tiers[1].alternatives],
            ["alpha faculty", tie_first.display_name, tie_second.display_name],
        )
        self.assertEqual(
            [alternative.physician.display_name for alternative in result.tiers[2].alternatives],
            ["Backup faculty", "Normal rounder", "Patient care faculty"],
        )
        self.assertEqual(
            [alternative.physician.display_name for alternative in result.tiers[3].alternatives],
            [
                "Blocked expected",
                "Unavailable faculty",
                "Unmatched community",
            ],
        )

        expected_alternative = next(
            alternative for alternative in result if alternative.physician == expected
        )
        self.assertEqual(
            expected_alternative.effective_category,
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
        )
        self.assertTrue(expected_alternative.is_expected_community_commitment)
        self.assertEqual(expected_alternative.availability, "available")
        self.assertEqual(expected_alternative.faculty_equivalent, False)
        self.assertEqual(expected_alternative.faculty_equivalent_reason, None)

        casual_alternative = next(
            alternative for alternative in result if alternative.physician == community_casual
        )
        self.assertEqual(casual_alternative.tier, "available_expected_community")

        normal_alternative = next(
            alternative for alternative in result if alternative.physician == normal_rounder
        )
        self.assertEqual(normal_alternative.availability, "backup_only")
        self.assertEqual(normal_alternative.tier, "backup_only")

        blocked_alternative = next(
            alternative for alternative in result if alternative.physician == blocked
        )
        self.assertEqual(blocked_alternative.tier, "not_available")
        self.assertTrue(blocked_alternative.is_expected_community_commitment)
        self.assertEqual(
            {(source.source_type, source.source_id) for source in blocked_alternative.sources},
            {
                ("community_preceptor_rule", blocked_rule.pk),
                ("vacation", blocked_vacation.pk),
            },
        )
        self.assertIn("blocked", blocked_alternative.availability_reason)
        patient_care_alternative = next(
            alternative for alternative in result if alternative.physician == patient_care
        )
        self.assertEqual(patient_care_alternative.base_availability, "patient_care")
        self.assertEqual(patient_care_alternative.availability, "backup_only")
        self.assertEqual(patient_care_alternative.tier, "backup_only")
        self.assertLess(tie_first.pk, tie_second.pk)

    def test_soft_academic_event_is_available_after_clean_candidates_and_creates_preference(self):
        clean = self.faculty("Clean faculty")
        clean_expected = self.community("Clean expected Community")
        create_community_preceptor_rule(
            physician=clean_expected,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        soft = self.community("Soft expected Community")
        create_community_preceptor_rule(
            physician=soft,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        calendar_only = self.faculty("Calendar-only event faculty")
        definition = AcademicEventDefinition.objects.create(
            base_name="Soft event", start_academic_year=2024
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            participants=(soft,),
            scheduler=self.scheduler,
        )
        create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Calendar-only event", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            participants=(calendar_only,),
            scheduler=self.scheduler,
        )

        alternatives = assignment_alternatives_for_session(
            self.session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )
        by_physician = {alternative.physician: alternative for alternative in alternatives}
        self.assertEqual(by_physician[clean].tier, "available_other")
        self.assertEqual(by_physician[clean_expected].tier, "available_expected_community")
        self.assertEqual(by_physician[calendar_only].tier, "available_other")
        self.assertFalse(
            any(
                source.source_type == "academic_event"
                for source in by_physician[calendar_only].sources
            )
        )
        self.assertEqual(by_physician[soft].availability, "available")
        self.assertEqual(by_physician[soft].tier, "available_soft_academic_event")
        self.assertTrue(by_physician[soft].is_expected_community_commitment)
        self.assertIn("soft academic event", by_physician[soft].selection_consequence.casefold())
        self.assertIn("preference", by_physician[soft].selection_consequence.casefold())
        self.assertLess(
            alternatives.alternatives.index(by_physician[clean_expected]),
            alternatives.alternatives.index(by_physician[soft]),
        )
        self.assertLess(
            alternatives.alternatives.index(by_physician[clean]),
            alternatives.alternatives.index(by_physician[soft]),
        )
        assign_preceptor(
            session=self.session,
            physician=soft,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        soft_findings = [
            finding
            for finding in draft_findings_for_session(self.session).findings
            if finding.code == "soft_academic_event_assignment"
        ]
        self.assertEqual(len(soft_findings), 1)
        self.assertEqual(soft_findings[0].severity, "preference")
        self.assertEqual(soft_findings[0].session, self.session)
        self.assertEqual(soft_findings[0].physicians, (soft,))

    def test_normal_rounder_community_preceptor_is_unavailable_for_affected_session(self):
        community_rounder = self.community("Community normal rounder")
        add_role(
            physician=community_rounder,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start=date(2025, 2, 3),
            effective_end=date(2025, 2, 9),
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community_rounder,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        record_normal_rounder(
            week=date(2025, 2, 3), physician=community_rounder, scheduler=self.scheduler
        )

        result = assignment_alternatives_for_session(
            self.session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )
        alternative = next(
            alternative for alternative in result if alternative.physician == community_rounder
        )

        self.assertEqual(
            (alternative.availability, alternative.tier), ("not_available", "not_available")
        )

    def test_excludes_other_assignments_and_keeps_ineligible_current_state_separate(self):
        current = self.faculty("Current")
        assigned_elsewhere = self.faculty("Assigned elsewhere")
        assign_preceptor(
            session=self.session,
            physician=current,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=self.session,
            physician=assigned_elsewhere,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )
        correct_role(
            effective_role=current.roles.get(role=EffectiveRole.Role.FACULTY),
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )

        before = {
            model: model.objects.count()
            for model in (
                Physician,
                EffectiveRole,
                FacultyAvailabilityTemplate,
                PreceptorSession,
                PreceptorAssignment,
                PreceptorAssignmentChange,
            )
        }
        result = assignment_alternatives_for_session(self.session, position="primary-1")
        repeated = assignment_alternatives_for_session(self.session, position="primary-1")

        self.assertIsNotNone(result.current)
        self.assertEqual(result.current.physician, current)
        self.assertFalse(result.current.ordinarily_eligible_to_precept)
        self.assertNotIn(current, [alternative.physician for alternative in result])
        self.assertNotIn(
            assigned_elsewhere,
            [alternative.physician for alternative in result],
        )
        self.assertEqual(
            [(alternative.physician.pk, alternative.tier) for alternative in result],
            [(alternative.physician.pk, alternative.tier) for alternative in repeated],
        )
        self.assertEqual(
            before,
            {model: model.objects.count() for model in before},
        )

    def test_secondary_candidates_include_only_faculty(self):
        faculty = self.faculty("Faculty")
        community = self.community("Faculty-equivalent Community")
        correct_role(
            effective_role=community.roles.get(),
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
        )

        alternatives = assignment_alternatives_for_session(self.session, position="secondary")

        self.assertEqual([alternative.physician for alternative in alternatives], [faculty])
        self.assertNotIn(community, [alternative.physician for alternative in alternatives])


class AssignmentAlternativePresentationContractTests(TestCase):
    def test_choice_labels_are_names_only_while_tiers_and_values_are_preserved(self):
        available = SimpleNamespace(
            physician=SimpleNamespace(pk=1, display_name="Available physician"),
            effective_category_display="Faculty",
            faculty_equivalent_display="Yes",
            expected_display="Expected Community Preceptor",
            status_text="Available for assignment.",
        )
        unavailable = SimpleNamespace(
            physician=SimpleNamespace(pk=2, display_name="Unavailable physician"),
            effective_category_display="Faculty",
            faculty_equivalent_display="No",
            expected_display="",
            status_text="Not available; selection records a hard conflict.",
        )
        alternatives = SimpleNamespace(
            current=None,
            tiers=(
                SimpleNamespace(label="Available Faculty", alternatives=(available,)),
                SimpleNamespace(label="Not-available Faculty", alternatives=(unavailable,)),
            ),
        )

        field = AssignmentPhysicianChoiceField(alternatives)

        self.assertEqual(
            list(field.choices),
            [
                ("Available Faculty", [("1", "Available physician")]),
                ("Not-available Faculty", [("2", "Unavailable physician")]),
            ],
        )

    def test_late_doc_choice_labels_are_names_only_while_tiers_and_values_are_preserved(self):
        available = SimpleNamespace(
            physician=SimpleNamespace(pk=1, display_name="Available physician"),
            tier_label="Available Faculty",
            availability_reason="Available Monday PM.",
        )
        unavailable = SimpleNamespace(
            physician=SimpleNamespace(pk=2, display_name="Unavailable physician"),
            tier_label="Not-available Faculty",
            availability_reason="Not available Monday PM; selection records a hard finding.",
        )

        form = LateDocAssignmentForm(
            candidates=SimpleNamespace(current=None, candidates=(available, unavailable))
        )

        self.assertEqual(
            list(form.fields["physician"].choices),
            [
                ("Available Faculty", [("1", "Available physician")]),
                ("Not-available Faculty", [("2", "Unavailable physician")]),
            ],
        )

    def test_tiers_are_rendered_in_service_order_as_a_one_column_grid(self):
        schedules_root = Path(__file__).resolve().parents[1]
        template = (
            schedules_root / "templates" / "schedules" / "_assignment_alternative_details.html"
        ).read_text()
        stylesheet = (schedules_root / "static" / "schedules" / "application.css").read_text()

        tier_loop = "{% for tier in alternatives.tiers %}"
        self.assertEqual(template.count(tier_loop), 1)
        self.assertIn("Assignment Options", template)
        self.assertLess(template.index("{% if alternatives.current %}"), template.index(tier_loop))
        self.assertLess(
            template.index(tier_loop),
            template.index('<fieldset class="alternative-tier', template.index(tier_loop)),
        )
        self.assertIn(
            ".alternative-grid { display: grid; grid-template-columns: minmax(0, 1fr); "
            "gap: .75rem; }",
            stylesheet,
        )
        self.assertEqual(stylesheet.count(".alternative-grid"), 1)
        self.assertIn(
            ".grid, .dates { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); "
            "gap: .75rem; }",
            stylesheet,
        )


class DraftFindingsTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )

    def physician(self, name, role=None):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        if role:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        return physician

    def faculty(self, name, availability="open"):
        physician = self.physician(name, EffectiveRole.Role.FACULTY)
        slots = {slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        return physician

    def assign(self, physician, responsibility="primary", primary_position=1):
        return assign_preceptor(
            session=self.session,
            physician=physician,
            responsibility=responsibility,
            primary_position=primary_position,
            scheduler=self.scheduler,
        )

    def test_primary_coverage_precedes_faculty_equivalence_and_keeps_assigned_problems(
        self,
    ):
        faculty = self.faculty("Faculty")
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)

        empty = draft_findings_for_session(self.session)
        self.assertEqual(
            [finding.code for finding in empty.findings],
            ["missing_primary_position", "missing_primary_position"],
        )
        self.assertEqual(
            [finding.explanation["position"] for finding in empty.findings],
            ["primary-1", "primary-2"],
        )

        self.assign(faculty)
        self.assertEqual(
            [finding.code for finding in draft_findings_for_session(self.session).findings],
            ["missing_primary_position"],
        )
        self.assertEqual(
            draft_findings_for_session(self.session).findings[0].explanation["position"],
            "primary-2",
        )
        self.assign(community, primary_position=2)
        self.assertEqual(
            [finding.code for finding in draft_findings_for_session(self.session).findings],
            ["insufficient_faculty_equivalent_coverage", "not_available_assignment"],
        )
        correct_role(
            effective_role=community.roles.get(),
            effective_start=date(2025, 1, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
        )
        self.assertNotIn(
            "insufficient_faculty_equivalent_coverage",
            [finding.code for finding in draft_findings_for_session(self.session).findings],
        )

    def test_unfilled_coverage_keeps_assigned_physician_findings(self):
        unavailable = self.faculty("Unavailable", availability="off")
        self.assign(unavailable)

        self.assertEqual(
            [finding.code for finding in draft_findings_for_session(self.session).findings],
            ["missing_primary_position", "not_available_assignment"],
        )

    def test_faculty_secondary_counts_but_optional_secondary_does_not_create_coverage_finding(
        self,
    ):
        first = self.faculty("First")
        second = self.physician("Second", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        qualified_secondary = self.faculty("Qualified")
        self.assign(first)
        self.assign(second, primary_position=2)
        self.assign(qualified_secondary, responsibility="secondary", primary_position=None)

        codes = [finding.code for finding in draft_findings_for_session(self.session).findings]

        self.assertNotIn("missing_primary_position", codes)
        self.assertNotIn("insufficient_faculty_equivalent_coverage", codes)

    def test_invalid_role_and_effective_availability_findings_keep_assignments_and_sources(self):
        available = self.faculty("Available")
        unavailable = self.faculty("Unavailable", availability="patient_care")
        external_role = add_role(
            physician=unavailable,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start=self.session.date,
            effective_end=self.session.date,
            scheduler=self.scheduler,
        )
        invalid = self.physician("Invalid", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.assign(available)
        assign_preceptor(
            session=self.session,
            physician=unavailable,
            responsibility="primary",
            primary_position=2,
            scheduler=self.scheduler,
            override=True,
        )
        invalid_assignment = PreceptorAssignment.objects.create(
            session=self.session,
            physician=invalid,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
        )
        end_role(
            effective_role=EffectiveRole.objects.get(physician=invalid),
            effective_end=date(2025, 2, 2),
            scheduler=self.scheduler,
        )
        vacation = create_vacation(
            physician=available,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day="AM",
            scheduler=self.scheduler,
        )

        findings = draft_findings_for_session(self.session)
        codes = [finding.code for finding in findings.findings]

        self.assertIn("role_ineligible_assignment", codes)
        self.assertEqual(
            PreceptorAssignment.objects.get(pk=invalid_assignment.pk).physician, invalid
        )
        unavailable_finding = next(
            finding for finding in findings.findings if finding.physicians == (unavailable,)
        )
        self.assertEqual(unavailable_finding.code, "not_available_assignment")
        self.assertEqual(unavailable_finding.explanation["base_availability"], "patient_care")
        self.assertEqual(
            [(source.source_type, source.source_id) for source in unavailable_finding.sources],
            [
                ("faculty_template", unavailable.faculty_availability_templates.get().pk),
                ("external_patient_care", external_role.pk),
            ],
        )
        available_finding = next(
            finding for finding in findings.findings if finding.physicians == (available,)
        )
        self.assertEqual(available_finding.code, "not_available_assignment")
        self.assertIn(
            ("vacation", vacation.pk),
            [(source.source_type, source.source_id) for source in available_finding.sources],
        )

    def test_retained_secondary_community_assignment_has_a_clear_hard_finding(self):
        community = self.physician("Retained Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        assignment = PreceptorAssignment.objects.create(
            session=self.session,
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
        )

        finding = next(
            finding
            for finding in draft_findings_for_session(self.session).findings
            if finding.code == "role_responsibility_mismatch"
        )

        self.assertEqual(finding.severity, "hard")
        self.assertEqual(finding.physicians, (community,))
        self.assertEqual(finding.explanation["assignment_id"], assignment.pk)
        self.assertEqual(
            PreceptorAssignment.objects.get(pk=assignment.pk).physician,
            community,
        )

    def test_backup_and_community_expectations_are_preferences_only_when_available_and_unassigned(
        self,
    ):
        first = self.faculty("First")
        second = self.faculty("Second")
        backup = self.faculty("Backup", availability="backup_only")
        expected = self.physician("Expected", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        also_expected = self.physician("Also expected", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        blocked = self.physician("Blocked", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.assign(first)
        self.assign(second, primary_position=2)
        self.assign(backup, responsibility="secondary", primary_position=None)
        for physician in (expected, also_expected, blocked):
            create_community_preceptor_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                scheduler=self.scheduler,
            )
        create_vacation(
            physician=blocked,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day="AM",
            scheduler=self.scheduler,
        )

        findings = draft_findings_for_session(self.session)
        preference_physicians = [
            finding.physicians for finding in findings.findings if finding.severity == "preference"
        ]

        self.assertIn((backup,), preference_physicians)
        self.assertIn((expected,), preference_physicians)
        self.assertIn((also_expected,), preference_physicians)
        self.assertNotIn((blocked,), preference_physicians)
        self.assign(expected, primary_position=2)
        self.assertNotIn(
            (expected,),
            [finding.physicians for finding in draft_findings_for_session(self.session).findings],
        )
        unmatched = self.physician("Unmatched", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        create_community_preceptor_rule(
            physician=unmatched,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        self.assertNotIn(
            (unmatched,),
            [finding.physicians for finding in draft_findings_for_session(self.session).findings],
        )
        self.assertTrue(
            all(finding.severity in ("hard", "preference") for finding in findings.findings)
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_week_month_order_totals_and_queries_are_read_only(self, _now):
        before = {
            model: model.objects.count()
            for model in (
                SchedulePeriod,
                PreceptorSession,
                PreceptorAssignment,
                PreceptorAssignmentChange,
            )
        }

        week = draft_findings_for_week(self.session.date)
        month = draft_findings_for_month(date(2025, 2, 1))

        self.assertEqual(week.findings[0].code, "missing_normal_rounder")
        self.assertEqual(week.hard_total, 22)
        self.assertEqual(week.preference_total, 0)
        self.assertTrue(week.has_blocking_findings)
        self.assertEqual(
            [finding.code for finding in month.findings],
            [finding.code for finding in draft_findings_for_month(date(2025, 2, 1)).findings],
        )
        self.assertEqual(
            before,
            {model: model.objects.count() for model in before},
        )
        normal_rounder = self.faculty("Normal rounder")
        record_normal_rounder(
            week=self.session.date, physician=normal_rounder, scheduler=self.scheduler
        )
        self.assertNotIn(
            "missing_normal_rounder",
            [finding.code for finding in draft_findings_for_week(self.session.date).findings],
        )
