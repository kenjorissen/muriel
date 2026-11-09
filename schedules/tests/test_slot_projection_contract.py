from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    PreceptorAssignment,
)
from schedules.services.academic_events import create_academic_event_occurrence
from schedules.services.availability import effective_preceptor_availability_for_physicians
from schedules.services.community import create_community_preceptor_rule
from schedules.services.interviews import (
    assign_interview_shift,
    create_interview_shift,
    interview_assignment_candidates,
    interview_assignment_picker_candidates_on,
    interview_findings_for_shift,
    remove_interview_shift,
    unassign_interview_shift,
)
from schedules.services.late_docs import assign_late_doc
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    draft_findings_for_week,
    open_weekly_draft,
    remove_preceptor_assignment,
)
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.slot_projection import RequestedSlot, slot_projection
from schedules.services.vacations import create_vacation


class SlotProjectionContractTests(TestCase):
    week = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def physician(self, name, role=None, *, faculty_equivalent=False, leadership=""):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        if role:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                faculty_equivalent=faculty_equivalent,
                faculty_leadership=leadership,
                scheduler=self.scheduler,
            )
        return physician

    def faculty(self, name, availability="open", **slot_overrides):
        physician = self.physician(name, EffectiveRole.Role.FACULTY)
        slots = {slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        slots.update(slot_overrides)
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        return physician

    def test_representative_roster_preserves_role_base_state_and_ordered_sources(self):
        faculty = self.faculty("Faculty", monday_pm="backup_only")
        director = self.physician(
            "Program Director",
            EffectiveRole.Role.FACULTY,
            leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        create_faculty_availability_template(
            physician=director,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        community_rule = create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        equivalent = self.physician(
            "Faculty-equivalent Community",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
        )
        equivalent_rule = create_community_preceptor_rule(
            physician=equivalent,
            weekday=0,
            ordinals=[1],
            periods=["PM"],
            scheduler=self.scheduler,
        )
        backup = self.faculty("Backup-only", availability="backup_only")
        unavailable = self.faculty("Unavailable")
        no_role = self.physician("No applicable role")
        vacation = create_vacation(
            physician=unavailable,
            start_date=self.week,
            end_date=self.week,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Academic Event", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=self.week,
            end_date=self.week,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            participants=[unavailable],
            scheduler=self.scheduler,
        )
        normal_rounder = record_normal_rounder(
            week=self.week, physician=unavailable, scheduler=self.scheduler
        )
        casual_rule = create_casual_rounder_rule(
            physician=no_role, weekday=0, ordinals=[1], scheduler=self.scheduler
        )

        roster = (faculty, director, community, equivalent, backup, unavailable, no_role)
        am = effective_preceptor_availability_for_physicians(roster, self.week, "AM")
        pm = effective_preceptor_availability_for_physicians(roster, self.week, "PM")

        self.assertEqual(
            [
                (
                    result.physician.display_name,
                    result.effective_category,
                    result.ordinarily_eligible_to_precept,
                    result.faculty_equivalent,
                    result.base_availability,
                    result.availability,
                    result.is_expected_community_commitment,
                )
                for result in am
            ],
            [
                ("Faculty", "faculty", True, True, "open", "available", False),
                (
                    "Program Director",
                    "faculty",
                    True,
                    True,
                    "open",
                    "available",
                    False,
                ),
                ("Community", "community_preceptor", True, False, "available", "available", True),
                (
                    "Faculty-equivalent Community",
                    "community_preceptor",
                    True,
                    True,
                    "not_available",
                    "not_available",
                    False,
                ),
                (
                    "Backup-only",
                    "faculty",
                    True,
                    True,
                    "backup_only",
                    "backup_only",
                    False,
                ),
                (
                    "Unavailable",
                    "faculty",
                    True,
                    True,
                    "open",
                    "not_available",
                    False,
                ),
                ("No applicable role", None, False, False, None, None, False),
            ],
        )
        self.assertEqual(
            [
                (result.physician.display_name, result.base_availability, result.availability)
                for result in pm
            ],
            [
                ("Faculty", "backup_only", "backup_only"),
                ("Program Director", "open", "available"),
                ("Community", "not_available", "not_available"),
                ("Faculty-equivalent Community", "available", "available"),
                ("Backup-only", "backup_only", "backup_only"),
                ("Unavailable", "open", "backup_only"),
                ("No applicable role", None, None),
            ],
        )

        blocked = next(result for result in am if result.physician == unavailable)
        self.assertEqual(blocked.vacations, (vacation,))
        self.assertEqual(blocked.academic_events, (event,))
        self.assertEqual(blocked.normal_rounder, normal_rounder)
        self.assertEqual(blocked.casual_rounder_rules, (casual_rule,))
        self.assertEqual(
            [(source.source_type, source.source_id, source.reason) for source in blocked.sources],
            [
                (
                    "faculty_template",
                    unavailable.faculty_availability_templates.get().pk,
                    "base_availability",
                ),
                ("vacation", vacation.pk, "vacation"),
                ("academic_event", event.pk, "academic_event"),
                ("normal_rounder", normal_rounder.pk, "normal_rounding"),
                ("casual_rounder_rule", casual_rule.pk, "casual_rounding_coverage"),
            ],
        )
        self.assertEqual(
            [
                source.source_id
                for source in next(result for result in am if result.physician == community).sources
            ],
            [community_rule.pk],
        )
        self.assertEqual(
            [
                source.source_id
                for source in next(
                    result for result in pm if result.physician == equivalent
                ).sources
            ],
            [equivalent_rule.pk],
        )

    def test_patient_care_projection_separates_base_from_effective_availability(self):
        physician = self.faculty("Patient care", availability="patient_care")

        row = next(
            row
            for row in slot_projection((RequestedSlot(self.week, "AM"),))
            if row.physician == physician
        )

        self.assertEqual(row.base_availability, "patient_care")
        self.assertEqual(row.availability, "backup_only")
        self.assertEqual(
            (row.sources[0].source_type, row.sources[0].contribution),
            ("faculty_template", "patient_care"),
        )

    def test_bounded_projection_has_one_deterministic_row_per_physician_and_slot(self):
        zulu = self.faculty("Zulu")
        alpha = self.physician("Alpha")
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        create_vacation(
            physician=alpha,
            start_date=self.week,
            end_date=self.week,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Academic Event", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=self.week,
            end_date=self.week,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            participants=[zulu],
            scheduler=self.scheduler,
        )
        collective_event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="All Faculty Event", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=self.week,
            end_date=self.week,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            all_available_faculty=True,
            scheduler=self.scheduler,
        )
        requested_slots = (
            RequestedSlot(self.week, "PM"),
            RequestedSlot(self.week, "AM"),
            RequestedSlot(self.week, "AM"),
        )

        with CaptureQueriesContext(connection) as queries:
            rows = slot_projection(requested_slots)

        self.assertEqual(len(rows), 6)
        self.assertLessEqual(len(queries), 12)
        self.assertEqual(
            [(row.slot.period_of_day, row.physician.display_name) for row in rows],
            [
                ("AM", "Alpha"),
                ("AM", "Community"),
                ("AM", "Zulu"),
                ("PM", "Alpha"),
                ("PM", "Community"),
                ("PM", "Zulu"),
            ],
        )
        self.assertEqual(
            [role.role for role in rows[1].effective_roles],
            [EffectiveRole.Role.COMMUNITY_PRECEPTOR],
        )
        self.assertTrue(rows[1].ordinarily_eligible_to_precept)
        self.assertTrue(rows[1].is_expected_community_commitment)
        self.assertEqual(rows[0].availability, None)
        self.assertEqual(rows[0].sources[0].reason, "vacation")
        self.assertEqual(rows[2].availability, "not_available")
        self.assertCountEqual(rows[2].tier_zero.academic_events, (event, collective_event))
        for row in rows:
            self.assertEqual(
                row.tier_zero,
                effective_preceptor_availability_for_physicians(
                    (row.physician,), row.slot.on_date, row.slot.period_of_day
                )[0],
            )

    def test_tier_one_occupancy_is_bounded_to_slots_and_excludes_current_targets(self):
        am_preceptor = self.faculty("AM Preceptor", availability="off")
        pm_preceptor = self.faculty("PM Preceptor")
        interviewer = self.faculty("Interviewer", availability="off")
        replacement = self.faculty("Replacement")
        sessions = open_weekly_draft(self.week)
        am_session = next(session for session in sessions if session.period_of_day == "AM")
        pm_session = next(session for session in sessions if session.period_of_day == "PM")
        am_assignment = assign_preceptor(
            session=am_session,
            physician=am_preceptor,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        pm_assignment = assign_preceptor(
            session=pm_session,
            physician=pm_preceptor,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(academic_year=2024, date=self.week, scheduler=self.scheduler)
        assign_interview_shift(
            shift=shift, physician=interviewer, scheduler=self.scheduler, override=True
        )
        slots = (RequestedSlot(self.week, "AM"), RequestedSlot(self.week, "PM"))

        with CaptureQueriesContext(connection) as queries:
            rows = slot_projection(slots)

        row_for = {(row.slot.period_of_day, row.physician): row for row in rows}
        self.assertLessEqual(len(queries), 11)
        self.assertEqual(row_for["AM", am_preceptor].availability, "not_available")
        self.assertEqual(row_for["AM", am_preceptor].preceptor_assignment, am_assignment)
        self.assertEqual(
            (
                row_for["AM", am_preceptor].preceptor_assignment.pk,
                row_for["AM", am_preceptor].preceptor_assignment.session_id,
                row_for["AM", am_preceptor].preceptor_assignment.responsibility,
                row_for["AM", am_preceptor].preceptor_assignment.primary_position,
            ),
            (am_assignment.pk, am_session.pk, "primary", 1),
        )
        self.assertEqual(row_for["PM", pm_preceptor].preceptor_assignment, pm_assignment)
        self.assertEqual(row_for["AM", interviewer].availability, "not_available")
        self.assertEqual(row_for["AM", interviewer].interview_shift, shift)
        self.assertEqual(
            (
                row_for["AM", interviewer].interview_shift.pk,
                row_for["AM", interviewer].interview_shift.date,
            ),
            (shift.pk, self.week),
        )
        self.assertIsNone(row_for["PM", interviewer].interview_shift)

        excluded = {
            (row.slot.period_of_day, row.physician): row
            for row in slot_projection(
                slots,
                exclude_preceptor_assignment=am_assignment,
                exclude_interview_shift=shift,
            )
        }
        self.assertIsNone(excluded["AM", am_preceptor].preceptor_assignment)
        self.assertIsNone(excluded["AM", interviewer].interview_shift)
        self.assertEqual(excluded["PM", pm_preceptor].preceptor_assignment, pm_assignment)

        assign_preceptor(
            session=am_session,
            physician=replacement,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_interview_shift(
            shift=shift, physician=replacement, scheduler=self.scheduler, override=True
        )
        reassigned = {
            row.physician: row for row in slot_projection((RequestedSlot(self.week, "AM"),))
        }
        self.assertIsNone(reassigned[am_preceptor].preceptor_assignment)
        self.assertEqual(reassigned[replacement].preceptor_assignment.pk, am_assignment.pk)
        self.assertIsNone(reassigned[interviewer].interview_shift)
        self.assertEqual(reassigned[replacement].interview_shift, shift)

        remove_preceptor_assignment(assignment=am_assignment, scheduler=self.scheduler)
        unassign_interview_shift(shift=shift, scheduler=self.scheduler)
        released = {
            row.physician: row for row in slot_projection((RequestedSlot(self.week, "AM"),))
        }
        self.assertIsNone(released[replacement].preceptor_assignment)
        self.assertIsNone(released[replacement].interview_shift)

        assign_interview_shift(shift=shift, physician=replacement, scheduler=self.scheduler)
        remove_interview_shift(shift=shift, scheduler=self.scheduler)
        inactive = {
            row.physician: row for row in slot_projection((RequestedSlot(self.week, "AM"),))
        }
        self.assertIsNone(inactive[replacement].interview_shift)

    def test_program_director_interview_commitments_are_structured_and_release_on_removal(self):
        director = self.faculty("Program Director")
        second_director = self.faculty("Second Program Director")
        for physician in (director, second_director):
            role = physician.roles.get(role=EffectiveRole.Role.FACULTY)
            role.faculty_leadership = EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
            role.save(update_fields=("faculty_leadership",))
        other_faculty = self.faculty("Other Faculty")
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        shift = create_interview_shift(academic_year=2024, date=self.week, scheduler=self.scheduler)

        rows = {row.physician: row for row in slot_projection((RequestedSlot(self.week, "AM"),))}
        for physician in (director, second_director):
            self.assertIsNone(rows[physician].interview_shift)
            self.assertEqual(
                [
                    (
                        conflict.code,
                        conflict.source_type,
                        conflict.source_id,
                        conflict.record,
                        conflict.finding_explanation,
                        conflict.label,
                        conflict.reason,
                    )
                    for conflict in rows[physician].precepting_conflicts
                ],
                [
                    (
                        "program_director_interview_commitment",
                        "interview_shift",
                        shift.pk,
                        shift,
                        {"interview_shift_id": shift.pk, "interview_date": self.week},
                        "Program Director Interview",
                        "Committed to this AM Interview shift as Program Director.",
                    )
                ],
            )
        self.assertEqual(rows[other_faculty].precepting_conflicts, ())
        director_alternative = next(
            alternative
            for alternative in assignment_alternatives_for_session(
                session, PreceptorAssignment.Responsibility.PRIMARY, 1
            )
            if alternative.physician == director
        )
        self.assertEqual(director_alternative.tier, "not_available")
        self.assertEqual(
            [reason.code for reason in director_alternative.reasons],
            ["availability_source", "program_director_interview_commitment"],
        )
        self.assertIn(
            director,
            [
                candidate.physician
                for candidate in interview_assignment_candidates(shift).candidates
            ],
        )
        assignment = assign_preceptor(
            session=session,
            physician=director,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        findings = draft_findings_for_session(session)
        commitment_finding = next(
            finding
            for finding in findings.findings
            if finding.code == "program_director_interview_commitment"
        )
        self.assertEqual(commitment_finding.explanation["interview_shift_id"], shift.pk)
        self.assertEqual(commitment_finding.explanation["interview_date"], self.week)
        self.assertEqual(assignment.physician, director)

        assign_interview_shift(
            shift=shift, physician=director, scheduler=self.scheduler, override=True
        )
        assigned = {
            row.physician: row for row in slot_projection((RequestedSlot(self.week, "AM"),))
        }
        self.assertEqual(assigned[director].interview_shift, shift)
        self.assertEqual(
            [conflict.code for conflict in assigned[director].precepting_conflicts],
            ["interview_assignment_conflict"],
        )
        self.assertEqual(
            [conflict.code for conflict in assigned[second_director].precepting_conflicts],
            ["program_director_interview_commitment"],
        )

        unassign_interview_shift(shift=shift, scheduler=self.scheduler)
        self.assertTrue(
            next(
                row
                for row in slot_projection((RequestedSlot(self.week, "AM"),))
                if row.physician == director
            ).precepting_conflicts
        )
        remove_interview_shift(shift=shift, scheduler=self.scheduler)
        self.assertFalse(
            next(
                row
                for row in slot_projection((RequestedSlot(self.week, "AM"),))
                if row.physician == director
            ).precepting_conflicts
        )

    def test_candidate_membership_reasons_and_sibling_release_behavior(self):
        director = self.faculty("Program Director")
        director_role = director.roles.get()
        director_role.faculty_leadership = EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        director_role.save(update_fields=("faculty_leadership",))
        current = self.faculty("Current")
        am_conflict = self.faculty("AM conflict")
        pm_nonconflict = self.faculty("PM non-conflict")
        backup = self.faculty("Backup", availability="backup_only")
        unavailable = self.faculty("Unavailable", availability="off")
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        equivalent = self.physician(
            "Faculty-equivalent Community",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
        )
        sessions = open_weekly_draft(self.week)
        am_session = next(session for session in sessions if session.period_of_day == "AM")
        pm_session = next(session for session in sessions if session.period_of_day == "PM")
        current_assignment = assign_preceptor(
            session=am_session,
            physician=current,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        conflict_assignment = assign_preceptor(
            session=am_session,
            physician=am_conflict,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=am_session,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=pm_session,
            physician=pm_nonconflict,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(academic_year=2024, date=self.week, scheduler=self.scheduler)

        alternatives = assignment_alternatives_for_session(
            am_session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )
        self.assertEqual(alternatives.current.assignment, current_assignment)
        self.assertEqual(alternatives.current.tier, "current")
        self.assertNotIn(current, [alternative.physician for alternative in alternatives])
        self.assertNotIn(am_conflict, [alternative.physician for alternative in alternatives])
        self.assertIn(pm_nonconflict, [alternative.physician for alternative in alternatives])
        self.assertEqual(
            [
                candidate.physician
                for candidate in interview_assignment_candidates(shift).candidates
            ],
            [director, pm_nonconflict, backup],
        )
        self.assertIn(community, [candidate.physician for candidate in alternatives])
        self.assertIn(equivalent, [candidate.physician for candidate in alternatives])

        picker = {
            candidate.physician: candidate
            for candidate in interview_assignment_picker_candidates_on(self.week)
        }
        self.assertEqual(picker[am_conflict].tier, "override_required")
        self.assertEqual(
            picker[am_conflict].availability_reason,
            "Conflicts with an AM Preceptor assignment; selection records a hard finding.",
        )
        self.assertEqual(picker[unavailable].tier, "override_required")
        self.assertEqual(
            picker[unavailable].availability_reason,
            "Tier 0 unavailable and conflicts with an AM Preceptor assignment; "
            "selection records a hard finding.",
        )

        with self.assertRaisesRegex(ValidationError, "explicit override"):
            assign_interview_shift(shift=shift, physician=am_conflict, scheduler=self.scheduler)
        assign_interview_shift(shift=shift, physician=director, scheduler=self.scheduler)
        direct_interviewer = next(
            alternative
            for alternative in assignment_alternatives_for_session(
                am_session, PreceptorAssignment.Responsibility.PRIMARY, 1
            )
            if alternative.physician == director
        )
        self.assertEqual(direct_interviewer.tier, "not_available")
        self.assertIn(
            "interview_assignment_conflict", [reason.code for reason in direct_interviewer.reasons]
        )
        unassign_interview_shift(shift=shift, scheduler=self.scheduler)
        self.assertIn(
            director,
            [
                alternative.physician
                for alternative in assignment_alternatives_for_session(
                    am_session, PreceptorAssignment.Responsibility.PRIMARY, 1
                )
            ],
        )
        assign_interview_shift(shift=shift, physician=director, scheduler=self.scheduler)
        remove_interview_shift(shift=shift, scheduler=self.scheduler)
        self.assertIn(
            director,
            [
                alternative.physician
                for alternative in assignment_alternatives_for_session(
                    am_session, PreceptorAssignment.Responsibility.PRIMARY, 1
                )
            ],
        )
        remove_preceptor_assignment(assignment=conflict_assignment, scheduler=self.scheduler)
        self.assertIn(
            am_conflict,
            [
                candidate.physician
                for candidate in interview_assignment_picker_candidates_on(self.week)
            ],
        )

    def test_candidate_and_finding_consumers_read_each_slot_through_one_projection(self):
        faculty = self.faculty("Faculty")
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assignment = assign_preceptor(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(academic_year=2024, date=self.week, scheduler=self.scheduler)
        assign_interview_shift(
            shift=shift, physician=faculty, scheduler=self.scheduler, override=True
        )

        with CaptureQueriesContext(connection) as preceptor_candidate_queries:
            assignment_alternatives_for_session(
                session, PreceptorAssignment.Responsibility.PRIMARY, 2
            )
        with CaptureQueriesContext(connection) as preceptor_finding_queries:
            draft_findings_for_session(session)
        with CaptureQueriesContext(connection) as interview_candidate_queries:
            interview_assignment_candidates(shift)
        with CaptureQueriesContext(connection) as interview_finding_queries:
            interview_findings_for_shift(shift)

        # One projection plus each consumer's retained target/assignment lookup stays bounded.
        self.assertLessEqual(len(preceptor_candidate_queries), 13)
        self.assertLessEqual(len(preceptor_finding_queries), 13)
        self.assertLessEqual(len(interview_candidate_queries), 13)
        self.assertLessEqual(len(interview_finding_queries), 13)
        self.assertEqual(assignment.physician, faculty)

    def test_availability_and_week_findings_keep_bounded_query_counts(self):
        first = self.faculty("First")
        second = self.faculty("Second")
        rounder = self.faculty("Rounder")
        backup = self.faculty("Backup", availability="backup_only")
        expected = self.physician("Expected Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        create_community_preceptor_rule(
            physician=expected,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        record_normal_rounder(week=self.week, physician=rounder, scheduler=self.scheduler)
        sessions = open_weekly_draft(self.week)
        for session in sessions:
            assign_preceptor(
                session=session,
                physician=first,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
                scheduler=self.scheduler,
            )
            assign_preceptor(
                session=session,
                physician=second,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=2,
                scheduler=self.scheduler,
            )
        assign_late_doc(on_date=self.week, physician=first, scheduler=self.scheduler)
        requested_physicians = (first, second, rounder, backup, expected)

        with CaptureQueriesContext(connection) as one_slot_queries:
            availabilities = effective_preceptor_availability_for_physicians(
                requested_physicians, self.week, "AM"
            )
        with CaptureQueriesContext(connection) as week_queries:
            findings = draft_findings_for_week(self.week)
        self.assertEqual(
            [finding.code for finding in findings.findings], ["unassigned_community_commitment"]
        )
        # Retain the bounded projection query budgets independently of publication.
        self.assertLessEqual(len(one_slot_queries), 44)
        self.assertLessEqual(len(week_queries), 25)
        self.assertEqual(
            tuple(availability.physician for availability in availabilities), requested_physicians
        )

        additional_faculty = tuple(
            self.faculty(f"Additional faculty {index}") for index in range(12)
        )
        with CaptureQueriesContext(connection) as scaled_queries:
            scaled_availabilities = effective_preceptor_availability_for_physicians(
                requested_physicians + additional_faculty, self.week, "AM"
            )

        self.assertLessEqual(len(scaled_queries), len(one_slot_queries))
        self.assertEqual(
            tuple(availability.physician for availability in scaled_availabilities),
            requested_physicians + additional_faculty,
        )
