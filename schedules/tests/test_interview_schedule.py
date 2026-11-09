from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    InterviewShift,
    InterviewShiftChange,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.finding_acknowledgements import (
    acknowledge_finding,
    acknowledgement_for_finding,
    finding_identity,
)
from schedules.services.interviews import (
    assign_interview_shift,
    change_interview_shift_note,
    correct_interview_shift,
    create_interview_shift,
    interview_assignment_candidates,
    interview_assignment_picker_candidates_on,
    interview_findings_for_shift,
    interview_schedule_summary,
    remove_interview_shift,
    unassign_interview_shift,
)
from schedules.services.physicians import (
    add_role,
    correct_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    open_monthly_draft,
)


class InterviewScheduleServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def faculty(self, name, availability="open", leadership=""):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 7, 1),
            effective_end=None,
            faculty_leadership=leadership,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def shift(self, on_date=date(2025, 2, 3), **overrides):
        values = {"academic_year": 2024, "date": on_date, "scheduler": self.scheduler}
        values.update(overrides)
        return create_interview_shift(**values)

    def test_retained_lifecycle_records_actor_values_and_releases_a_date_for_reentry(self):
        faculty = self.faculty("Faculty")
        shift = self.shift(note="  initial note  ")
        assign_interview_shift(shift=shift, physician=faculty, scheduler=self.scheduler)
        change_interview_shift_note(shift=shift, note="corrected note", scheduler=self.scheduler)
        correct_interview_shift(
            shift=shift,
            academic_year=2024,
            date=date(2025, 2, 4),
            scheduler=self.scheduler,
        )
        unassign_interview_shift(shift=shift, scheduler=self.scheduler)
        assign_interview_shift(shift=shift, physician=faculty, scheduler=self.scheduler)
        remove_interview_shift(shift=shift, scheduler=self.scheduler)
        replacement = self.shift(on_date=date(2025, 2, 4))

        shift.refresh_from_db()
        changes = list(InterviewShiftChange.objects.filter(shift=shift).order_by("pk"))
        self.assertEqual(shift.note, "corrected note")
        self.assertFalse(shift.is_active)
        self.assertIsNone(shift.physician)
        self.assertNotEqual(replacement.pk, shift.pk)
        self.assertEqual(
            [change.action for change in changes],
            [
                "created",
                "assigned",
                "note_changed",
                "corrected",
                "unassigned",
                "assigned",
                "removed",
            ],
        )
        self.assertIsNone(changes[0].previous_value)
        self.assertEqual(changes[1].recorded_value["physician_id"], faculty.pk)
        self.assertEqual(changes[-1].previous_value["physician_id"], faculty.pk)
        self.assertFalse(changes[-1].recorded_value["is_active"])
        self.assertIsNone(changes[-1].recorded_value["physician_id"])
        self.assertTrue(all(change.changed_by == self.scheduler for change in changes))
        self.assertTrue(all(change.changed_at is not None for change in changes))

    def test_academic_year_edges_and_summary_counts_are_exact(self):
        first = self.shift(on_date=date(2024, 7, 1))
        last = self.shift(on_date=date(2025, 6, 30))
        self.assertEqual(self.shift(on_date=date(2024, 7, 1)).pk, first.pk)
        before_shifts = {
            shift.pk: (shift.date, shift.physician_id, shift.note, shift.is_active)
            for shift in (first, last)
        }
        before_history = list(
            InterviewShiftChange.objects.order_by("pk").values_list(
                "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
            )
        )

        # Friday proves academic-year rejection independently of closure behavior; Sunday
        # retains the calendar-boundary edge.
        for outside_date in (date(2024, 6, 28), date(2024, 6, 30)):
            with (
                self.subTest(outside_date=outside_date),
                self.assertRaises(ValidationError) as error,
            ):
                self.shift(on_date=outside_date)
            self.assertTrue(
                any("academic year" in message.lower() for message in error.exception.messages)
            )
            self.assertFalse(InterviewShift.objects.filter(date=outside_date).exists())

        self.assertEqual(
            {
                shift.pk: (shift.date, shift.physician_id, shift.note, shift.is_active)
                for shift in InterviewShift.objects.filter(pk__in=before_shifts)
            },
            before_shifts,
        )
        self.assertEqual(
            list(
                InterviewShiftChange.objects.order_by("pk").values_list(
                    "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
                )
            ),
            before_history,
        )
        summary = interview_schedule_summary(2024)
        self.assertEqual(summary.active_count, 2)
        self.assertEqual(summary.remaining_to_20, 18)
        self.assertEqual(summary.unassigned_count, 2)
        self.assertEqual(summary.over_target_count, 0)
        self.assertFalse(summary.complete)

    def test_historical_date_corrections_remain_auditable(self):
        shift = self.shift(academic_year=2023, on_date=date(2024, 2, 1), note="Original")

        correct_interview_shift(
            shift=shift,
            academic_year=2023,
            date=date(2024, 2, 2),
            scheduler=self.scheduler,
        )

        correction = InterviewShiftChange.objects.get(action=InterviewShiftChange.Action.CORRECTED)
        self.assertEqual(correction.previous_value["date"], "2024-02-01")
        self.assertEqual(correction.recorded_value["date"], "2024-02-02")
        self.assertEqual(correction.changed_by, self.scheduler)

    def test_exactly_twenty_assigned_shifts_complete_and_an_extra_is_over_target(self):
        faculty = self.faculty("Faculty")
        on_date = date(2024, 7, 1)
        assigned = 0
        while assigned < 20:
            if on_date.weekday() < 5:
                shift = self.shift(on_date=on_date)
                assign_interview_shift(shift=shift, physician=faculty, scheduler=self.scheduler)
                assigned += 1
            on_date += timedelta(days=1)

        complete = interview_schedule_summary(2024)
        self.assertTrue(complete.complete)
        self.assertEqual(complete.remaining_to_20, 0)
        self.assertEqual(complete.hard_findings, ())

        self.shift(on_date=on_date)
        over_target = interview_schedule_summary(2024)
        self.assertEqual(over_target.active_count, 21)
        self.assertEqual(over_target.over_target_count, 1)
        self.assertFalse(over_target.complete)

    def test_candidates_order_faculty_and_exclude_sibling_assignments(self):
        pd = self.faculty("Program Director", leadership="program_director")
        other = self.faculty("Other Faculty")
        backup = self.faculty("Backup Faculty", availability="backup_only")
        associate = self.faculty("Associate", leadership="associate_program_director")
        community = create_physician(display_name="Faculty-equivalent", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2024, 7, 1),
            effective_end=None,
            faculty_equivalent=True,
            scheduler=self.scheduler,
        )
        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assign_preceptor(
            session=session,
            physician=other,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = self.shift()

        candidates = interview_assignment_candidates(shift).candidates
        self.assertEqual(
            [(candidate.physician, candidate.tier) for candidate in candidates],
            [(pd, "program_director"), (associate, "available_faculty"), (backup, "backup_only")],
        )
        self.assertNotIn(community, [candidate.physician for candidate in candidates])

    def test_override_conflicts_are_findings_and_preceptor_candidates_are_symmetric(self):
        faculty = self.faculty("Faculty")
        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assignment = assign_preceptor(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = self.shift()
        before_shift = (shift.date, shift.physician_id, shift.note, shift.is_active)
        before_history = list(
            InterviewShiftChange.objects.order_by("pk").values_list(
                "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
            )
        )
        with self.assertRaises(ValidationError) as error:
            assign_interview_shift(shift=shift, physician=faculty, scheduler=self.scheduler)
        self.assertTrue(any("override" in message.lower() for message in error.exception.messages))
        shift.refresh_from_db()
        self.assertEqual(
            (shift.date, shift.physician_id, shift.note, shift.is_active), before_shift
        )
        self.assertEqual(
            list(
                InterviewShiftChange.objects.order_by("pk").values_list(
                    "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
                )
            ),
            before_history,
        )
        assigned_shift = assign_interview_shift(
            shift=shift, physician=faculty, scheduler=self.scheduler, override=True
        )
        interview_finding = next(
            finding
            for finding in interview_findings_for_shift(shift).findings
            if finding.code == "preceptor_assignment_conflict"
        )
        preceptor_finding = next(
            finding
            for finding in draft_findings_for_session(session).findings
            if finding.code == "interview_assignment_conflict"
        )
        alternatives = assignment_alternatives_for_session(
            session, PreceptorAssignment.Responsibility.PRIMARY, 2
        )

        self.assertEqual(
            (interview_finding.code, interview_finding.severity),
            ("preceptor_assignment_conflict", "hard"),
        )
        self.assertEqual(interview_finding.physicians, (faculty,))
        self.assertEqual(interview_finding.explanation["date"], shift.date)
        self.assertEqual(interview_finding.explanation["preceptor_assignment_id"], assignment.pk)
        self.assertEqual(interview_finding.explanation["preceptor_session_id"], session.pk)
        self.assertEqual(
            (preceptor_finding.code, preceptor_finding.severity),
            ("interview_assignment_conflict", "hard"),
        )
        self.assertEqual(preceptor_finding.physicians, (faculty,))
        self.assertEqual(preceptor_finding.explanation["interview_shift_id"], shift.pk)
        self.assertEqual(preceptor_finding.explanation["interview_date"], shift.date)
        self.assertNotIn(faculty, [candidate.physician for candidate in alternatives])
        self.assertEqual(assignment.physician, faculty)
        assignment_change = InterviewShiftChange.objects.get(
            shift=assigned_shift, action=InterviewShiftChange.Action.ASSIGNED
        )
        self.assertEqual(assignment_change.recorded_value["physician_id"], faculty.pk)
        self.assertIsNone(assignment_change.previous_value["physician_id"])

    def test_unassigned_interview_commits_program_director_to_the_same_am_preceptor_slot(self):
        director = self.faculty("Program Director", leadership="program_director")
        draft = open_monthly_draft(date(2025, 2, 1))
        session = draft.sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        matching_pm_session = draft.sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.PM
        )
        other_open_am_session = draft.sessions.get(
            date=date(2025, 2, 4), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        shift = self.shift()

        alternatives = assignment_alternatives_for_session(
            session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )
        director_alternative = next(
            candidate for candidate in alternatives if candidate.physician == director
        )
        self.assertFalse(director_alternative.automatic_eligible)
        self.assertEqual(director_alternative.tier, "not_available")
        self.assertIn(
            "program_director_interview_commitment",
            [reason.code for reason in director_alternative.reasons],
        )

        assignment = assign_preceptor(
            session=session,
            physician=director,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=matching_pm_session,
            physician=director,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=other_open_am_session,
            physician=director,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        finding = next(
            finding
            for finding in draft_findings_for_session(session).findings
            if finding.code == "program_director_interview_commitment"
        )

        self.assertEqual(assignment.physician, director)
        self.assertEqual(
            (finding.code, finding.severity), ("program_director_interview_commitment", "hard")
        )
        self.assertEqual(finding.session, session)
        self.assertEqual(finding.physicians, (director,))
        self.assertEqual(finding.explanation["interview_shift_id"], shift.pk)
        self.assertEqual(finding.explanation["interview_date"], shift.date)
        self.assertFalse(
            any(
                candidate.code == "program_director_interview_commitment"
                for candidate in draft_findings_for_session(matching_pm_session).findings
            )
        )
        self.assertFalse(
            any(
                candidate.code == "program_director_interview_commitment"
                for candidate in draft_findings_for_session(other_open_am_session).findings
            )
        )

    def test_patient_care_candidate_is_backup_only_and_has_a_preference_finding(self):
        physician = self.faculty("Patient care Faculty", availability="patient_care")
        shift = self.shift()
        candidate = next(
            candidate
            for candidate in interview_assignment_picker_candidates_on(shift.date)
            if candidate.physician == physician
        )

        self.assertFalse(candidate.requires_override)
        self.assertEqual(candidate.availability, "backup_only")
        assign_interview_shift(shift=shift, physician=physician, scheduler=self.scheduler)
        finding = interview_findings_for_shift(shift).findings[0]
        self.assertEqual((finding.code, finding.severity), ("backup_only_assignment", "preference"))
        self.assertEqual(finding.explanation["base_availability"], "patient_care")

    def test_interview_acknowledgement_is_retired_when_the_shift_occurrence_changes(self):
        faculty = self.faculty("Unavailable Faculty", availability="off")
        shift = self.shift()
        assign_interview_shift(
            shift=shift, physician=faculty, scheduler=self.scheduler, override=True
        )
        finding = interview_findings_for_shift(shift).findings[0]

        acknowledgement = acknowledge_finding(
            finding=finding,
            scheduler=self.scheduler,
            reason="No alternate interviewer is available.",
        )
        change_interview_shift_note(shift=shift, note="Unrelated note", scheduler=self.scheduler)
        acknowledgement.refresh_from_db()
        self.assertTrue(acknowledgement.is_current)

        correct_interview_shift(
            shift=shift,
            academic_year=2024,
            date=date(2025, 2, 4),
            scheduler=self.scheduler,
        )
        acknowledgement.refresh_from_db()
        self.assertIsNotNone(acknowledgement.no_longer_applies_at)
        self.assertIsNone(
            acknowledgement_for_finding(interview_findings_for_shift(shift).findings[0])
        )

    def test_interview_holiday_identity_excludes_the_displayed_closure_text(self):
        faculty = self.faculty("Holiday Faculty")
        shift = InterviewShift.objects.create(date=date(2026, 12, 25), physician=faculty)
        finding = interview_findings_for_shift(shift).findings[0]

        self.assertEqual(finding.code, "holiday_closed_interview")
        self.assertEqual(finding_identity(finding).material_values, {"physician_ids": [faculty.pk]})

    def test_mutations_require_a_scheduler_and_a_current_faculty_role(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        shift = self.shift()
        nonfaculty = create_physician(display_name="No Faculty role", scheduler=self.scheduler)
        expired = self.faculty("Expired Faculty")
        expired_role = expired.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=expired_role,
            effective_start=expired_role.effective_start,
            effective_end=date(2025, 2, 2),
            scheduler=self.scheduler,
        )

        before_shift = (shift.date, shift.physician_id, shift.note, shift.is_active)
        before_history = list(
            InterviewShiftChange.objects.order_by("pk").values_list(
                "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
            )
        )
        with self.assertRaises(PermissionDenied):
            change_interview_shift_note(shift=shift, note="no access", scheduler=anonymous)
        with self.assertRaises(ValidationError) as no_faculty_error:
            assign_interview_shift(shift=shift, physician=nonfaculty, scheduler=self.scheduler)
        self.assertTrue(
            any(
                "effective faculty role" in message.lower()
                for message in no_faculty_error.exception.messages
            )
        )
        with self.assertRaises(ValidationError) as expired_faculty_error:
            assign_interview_shift(shift=shift, physician=expired, scheduler=self.scheduler)
        self.assertTrue(
            any(
                "effective faculty role" in message.lower()
                for message in expired_faculty_error.exception.messages
            )
        )
        shift.refresh_from_db()
        self.assertEqual(
            (shift.date, shift.physician_id, shift.note, shift.is_active), before_shift
        )
        self.assertEqual(
            list(
                InterviewShiftChange.objects.order_by("pk").values_list(
                    "shift_id", "action", "previous_value", "recorded_value", "changed_by_id"
                )
            ),
            before_history,
        )
        assignment = assign_interview_shift(
            shift=shift,
            physician=nonfaculty,
            scheduler=self.scheduler,
            override=True,
        )
        self.assertEqual(InterviewShift.objects.get(pk=shift.pk).note, "")
        self.assertEqual(assignment.physician, nonfaculty)
        assignment_change = InterviewShiftChange.objects.get(
            shift=assignment, action=InterviewShiftChange.Action.ASSIGNED
        )
        self.assertEqual(assignment_change.recorded_value["physician_id"], nonfaculty.pk)
        self.assertIsNone(assignment_change.previous_value["physician_id"])
