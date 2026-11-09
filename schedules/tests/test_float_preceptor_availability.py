from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.availability_categories import (
    ensure_system_schedule_options,
    set_custom_availability_category_physicians,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.late_docs import (
    assign_late_doc,
    late_doc_assignment_candidates_on,
    remove_late_doc_assignment,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.precepting_solver import (
    SolveDateRange,
    preflight_precepting_solve,
    produce_fill_proposal,
)
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    draft_findings_for_week,
    open_weekly_draft,
    precepting_candidate_assessment,
    remove_preceptor_assignment,
)
from schedules.services.vacations import create_vacation


class FloatPreceptorAvailabilityTests(TestCase):
    monday = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.admin = ensure_system_schedule_options()
        self.sessions = open_weekly_draft(self.monday)
        self.am_session = next(
            session
            for session in self.sessions
            if session.date == self.monday
            and session.period_of_day == PreceptorSession.PeriodOfDay.AM
        )
        self.pm_session = next(
            session
            for session in self.sessions
            if session.date == self.monday
            and session.period_of_day == PreceptorSession.PeriodOfDay.PM
        )

    def physician(self, name, *roles):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        for role in roles:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        return physician

    def schedule(self, physician, state="off", *, admin_on_monday=False):
        slots = {
            slot: FacultyAvailabilityTemplate.base_selection(state)
            for slot, _label in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        if admin_on_monday:
            slots["monday_am"] = {"category_id": self.admin.pk}
            slots["monday_pm"] = {"category_id": self.admin.pk}
        return create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )

    def test_tier_zero_composes_admin_float_without_replacing_ordinary_availability(self):
        faculty_float = self.physician(
            "Faculty Float", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        self.schedule(faculty_float, state="open")
        community_float = self.physician(
            "Community Float",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        self.schedule(community_float, admin_on_monday=True)
        recurrence = create_community_preceptor_rule(
            physician=community_float,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        unmatched_community_float = self.physician(
            "Unmatched Community Float",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        self.schedule(unmatched_community_float, admin_on_monday=True)
        float_only = self.physician(
            "Float only", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        template = self.schedule(float_only, admin_on_monday=True)
        ordinary_admin = self.physician("Ordinary Admin", EffectiveRole.Role.FACULTY)
        set_custom_availability_category_physicians(
            category=self.admin, physicians=[ordinary_admin], scheduler=self.scheduler
        )
        self.schedule(ordinary_admin, admin_on_monday=True)

        self.assertEqual(
            effective_preceptor_availability_on(faculty_float, self.monday, "AM").availability,
            "available",
        )
        community = effective_preceptor_availability_on(community_float, self.monday, "AM")
        self.assertEqual(community.availability, "available")
        self.assertEqual(community.community_preceptor_rule, recurrence)
        unmatched_community = effective_preceptor_availability_on(
            unmatched_community_float, self.monday, "AM"
        )
        self.assertTrue(unmatched_community.is_float_only)
        self.assertFalse(unmatched_community.faculty_equivalent)
        float_availability = effective_preceptor_availability_on(float_only, self.monday, "AM")
        self.assertTrue(float_availability.is_float_only)
        self.assertEqual(float_availability.availability_display, "Available as Float")
        self.assertIn(
            ("faculty_template", template.pk),
            {(source.source_type, source.source_id) for source in float_availability.sources},
        )
        self.assertEqual(
            effective_preceptor_availability_on(ordinary_admin, self.monday, "AM").availability,
            "not_available",
        )

    def test_manual_float_choices_are_valid_but_hard_conflicts_and_solver_still_win(self):
        float_faculty = self.physician(
            "Float Faculty", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        self.schedule(float_faculty, state="open", admin_on_monday=True)
        hard_conflict = self.physician(
            "Blocked Float", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        self.schedule(hard_conflict, admin_on_monday=True)
        create_vacation(
            physician=hard_conflict,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="AM",
            scheduler=self.scheduler,
        )

        alternatives = assignment_alternatives_for_session(self.am_session, position="primary-1")
        float_choice = next(item for item in alternatives if item.physician == float_faculty)
        blocked_choice = next(item for item in alternatives if item.physician == hard_conflict)
        self.assertEqual(float_choice.tier, "available_as_float")
        self.assertTrue(float_choice.manually_selectable)
        self.assertFalse(float_choice.automatic_eligible)
        self.assertIn("Available as Float", float_choice.availability_reason)
        self.assertEqual(blocked_choice.tier, "not_available")

        am_assignment = assign_preceptor(
            session=self.am_session,
            physician=float_faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        pm_assignment = assign_preceptor(
            session=self.pm_session,
            physician=float_faculty,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )
        late_doc_assignment = assign_late_doc(
            on_date=self.monday, physician=float_faculty, scheduler=self.scheduler
        )
        self.assertNotIn(
            "not_available_assignment",
            [finding.code for finding in draft_findings_for_session(self.am_session).findings],
        )
        self.assertEqual(
            late_doc_assignment_candidates_on(self.monday).current.availability, "float_only"
        )

        remove_preceptor_assignment(assignment=am_assignment, scheduler=self.scheduler)
        remove_preceptor_assignment(assignment=pm_assignment, scheduler=self.scheduler)
        remove_late_doc_assignment(assignment=late_doc_assignment, scheduler=self.scheduler)

        preflight = preflight_precepting_solve(
            SolveDateRange(self.monday, date(2025, 2, 4)),
            balance_window=SolveDateRange(self.monday, date(2025, 2, 4)),
        )
        proposal = produce_fill_proposal(preflight.solver_input, solve_seed="float-availability")
        self.assertFalse(
            any(
                assignment.physician_id == float_faculty.pk and assignment.date == self.monday
                for assignment in proposal.assignments
            )
        )
        self.assertTrue(
            any(
                assignment.physician_id == float_faculty.pk and assignment.date == date(2025, 2, 4)
                for assignment in proposal.assignments
            )
        )

    def test_community_float_only_availability_allows_secondary_without_changing_role_facts(self):
        community_float = self.physician(
            "Community Float",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        self.schedule(community_float, admin_on_monday=True)
        ordinary_community = self.physician(
            "Ordinary Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR
        )

        assessment = precepting_candidate_assessment(
            self.am_session,
            community_float,
            PreceptorAssignment.Responsibility.SECONDARY,
            None,
        )
        choice = next(
            item
            for item in assignment_alternatives_for_session(self.am_session, position="secondary")
            if item.physician == community_float
        )

        self.assertTrue(assessment.manual_float_eligible)
        self.assertTrue(assessment.manually_selectable)
        self.assertFalse(assessment.automatic_eligible)
        self.assertNotIn(
            "role_responsibility_mismatch", [reason.code for reason in assessment.reasons]
        )
        self.assertEqual(choice.tier, "available_as_float")
        self.assertIn("Admin", choice.availability_reason)
        self.assertEqual(choice.effective_category, EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.assertFalse(choice.faculty_equivalent)

        assign_preceptor(
            session=self.am_session,
            physician=community_float,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )

        self.assertNotIn(
            "role_responsibility_mismatch",
            [finding.code for finding in draft_findings_for_session(self.am_session).findings],
        )
        with self.assertRaisesRegex(ValidationError, "effective Faculty role"):
            assign_preceptor(
                session=self.pm_session,
                physician=ordinary_community,
                responsibility=PreceptorAssignment.Responsibility.SECONDARY,
                primary_position=None,
                scheduler=self.scheduler,
            )

    def test_community_float_only_availability_allows_manual_late_doc_without_role_finding(
        self,
    ):
        automatic = self.physician("Automatic", EffectiveRole.Role.FACULTY)
        self.schedule(automatic, state="open")
        community_float = self.physician(
            "Community Float",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        self.schedule(community_float, admin_on_monday=True)
        ordinary_community = self.physician(
            "Ordinary Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR
        )

        candidates = late_doc_assignment_candidates_on(self.monday)
        choice = next(item for item in candidates if item.physician == community_float)
        self.assertEqual(choice.tier, "available_as_float")
        self.assertIn("Admin", choice.availability_reason)
        self.assertFalse(choice.faculty_equivalent)
        self.assertNotIn(ordinary_community, [item.physician for item in candidates])

        assignment = assign_late_doc(
            on_date=self.monday, physician=community_float, scheduler=self.scheduler
        )
        self.assertEqual(assignment.physician, community_float)
        self.assertNotIn(
            "role_ineligible_late_doc",
            [finding.code for finding in draft_findings_for_week(self.monday).findings],
        )
        remove_late_doc_assignment(assignment=assignment, scheduler=self.scheduler)

        solver_input = preflight_precepting_solve(
            SolveDateRange(self.monday, self.monday),
            balance_window=SolveDateRange(self.monday, self.monday),
        )
        proposal = produce_fill_proposal(solver_input.solver_input)
        late_doc = next(item for item in proposal.late_doc_assignments if item.date == self.monday)
        self.assertNotEqual(late_doc.physician_id, community_float.pk)
        exclusion = next(
            item
            for item in late_doc.explanation.hard_exclusions
            if item.physician_id == community_float.pk
        )
        self.assertEqual(
            [(reason.code, reason.effect, reason.severity) for reason in exclusion.reasons],
            [("manual_float_availability", "manual_only", None)],
        )

    def test_schedule_bot_explains_manual_float_exclusion_from_precepting(self):
        automatic = self.physician("Automatic", EffectiveRole.Role.FACULTY)
        self.schedule(automatic, state="open")
        float_only = self.physician(
            "Manual Float", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        self.schedule(float_only, admin_on_monday=True)

        solver_input = preflight_precepting_solve(
            SolveDateRange(self.monday, self.monday),
            balance_window=SolveDateRange(self.monday, self.monday),
        )
        proposal = produce_fill_proposal(solver_input.solver_input)
        assignment = next(
            item
            for item in proposal.assignments
            if item.date == self.monday and item.period_of_day == "AM"
        )
        exclusion = next(
            item
            for item in assignment.explanation.hard_exclusions
            if item.physician_id == float_only.pk
        )

        self.assertEqual(
            [(reason.code, reason.effect, reason.severity) for reason in exclusion.reasons],
            [("manual_float_availability", "manual_only", None)],
        )
        self.assertIn("Admin", exclusion.reasons[0].detail)

    def test_schedule_bot_explains_manual_float_exclusion_from_late_doc(self):
        automatic = self.physician("Automatic", EffectiveRole.Role.FACULTY)
        self.schedule(automatic, state="open")
        float_only = self.physician(
            "Manual Float", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )
        self.schedule(float_only, admin_on_monday=True)

        solver_input = preflight_precepting_solve(
            SolveDateRange(self.monday, self.monday),
            balance_window=SolveDateRange(self.monday, self.monday),
        )
        proposal = produce_fill_proposal(solver_input.solver_input)
        assignment = next(
            item for item in proposal.late_doc_assignments if item.date == self.monday
        )
        self.assertNotEqual(assignment.physician_id, float_only.pk)
        exclusion = next(
            item
            for item in assignment.explanation.hard_exclusions
            if item.physician_id == float_only.pk
        )

        self.assertEqual(
            [(reason.code, reason.effect, reason.severity) for reason in exclusion.reasons],
            [("manual_float_availability", "manual_only", None)],
        )
        self.assertIn("Admin", exclusion.reasons[0].detail)
        admin_source = next(
            source
            for source in effective_preceptor_availability_on(float_only, self.monday, "PM").sources
            if source.reason == "manual_float_availability"
        )
        self.assertEqual(
            exclusion.reasons[0].source_identity,
            (admin_source.source_type, admin_source.source_id),
        )
