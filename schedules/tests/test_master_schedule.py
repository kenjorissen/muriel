from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from schedules.models import (
    AcademicEvent,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    HolidayOccurrence,
    PreceptorAssignment,
)
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_occurrence,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.late_docs import assign_late_doc
from schedules.services.master_schedule import _occupancy, master_schedule_week
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.slot_projection import PreceptingConflict
from schedules.services.vacations import create_vacation


class MasterScheduleWeekTests(TestCase):
    monday = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def physician(
        self, name, *, role=None, start=date(2024, 1, 1), end=None, faculty_equivalent=False
    ):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        if role:
            add_role(
                physician=physician,
                role=role,
                effective_start=start,
                effective_end=end,
                faculty_equivalent=faculty_equivalent,
                scheduler=self.scheduler,
            )
        return physician

    def faculty(self, name, **availability):
        physician = self.physician(name, role=EffectiveRole.Role.FACULTY)
        slots = {slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        slots.update(availability)
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        return physician

    def coverage(self, week, key, day, period):
        slot = next(slot for slot in week.slots if slot.key == key)
        return next(
            coverage
            for schedule_day in slot.days
            if schedule_day.on_date == day
            for coverage in schedule_day.coverage
            if coverage.period_of_day == period
        )

    def test_week_has_fixed_slot_geometry_and_direct_assignment_facts(self):
        first = self.faculty("First", monday_pm="backup_only")
        second = self.faculty("Second")
        sessions = open_weekly_draft(self.monday)
        am = next(session for session in sessions if session.period_of_day == "AM")
        pm = next(session for session in sessions if session.period_of_day == "PM")
        assign_preceptor(
            session=am,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=pm,
            physician=second,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )
        late_doc = assign_late_doc(on_date=self.monday, physician=first, scheduler=self.scheduler)
        shift = create_interview_shift(
            academic_year=2024, date=self.monday, scheduler=self.scheduler
        )
        assign_interview_shift(shift=shift, physician=second, scheduler=self.scheduler)

        week = master_schedule_week(self.monday)

        self.assertEqual((week.week_start, week.week_end), (self.monday, date(2025, 2, 9)))
        self.assertEqual(
            [slot.key for slot in week.slots],
            [
                "precepting-primary-1",
                "precepting-primary-2",
                "precepting-secondary",
                "late-doc",
                "interviews",
            ],
        )
        self.assertTrue(all(len(slot.days) == 7 for slot in week.slots))
        self.assertEqual(
            [
                fact.source_id
                for fact in self.coverage(week, "precepting-primary-1", self.monday, "AM").facts
            ],
            [first.preceptor_assignments.get().pk],
        )
        self.assertEqual(
            [
                fact.source_id
                for fact in self.coverage(week, "precepting-secondary", self.monday, "PM").facts
            ],
            [second.preceptor_assignments.get().pk],
        )
        self.assertEqual(
            self.coverage(week, "late-doc", self.monday, "PM").facts[0].record,
            late_doc,
        )
        self.assertEqual(
            self.coverage(week, "interviews", self.monday, "AM").facts[0].record,
            shift,
        )
        self.assertFalse(
            self.coverage(week, "late-doc", self.monday + date.resolution, "PM").is_applicable
        )
        self.assertFalse(self.coverage(week, "interviews", date(2025, 2, 8), "AM").is_applicable)

        self.assertEqual(
            [
                (group.key, [position.key for position in group.positions])
                for group in week.clinic_groups
            ],
            [
                (
                    "clinic-morning",
                    [
                        "precepting-primary-1",
                        "precepting-primary-2",
                        "precepting-secondary",
                    ],
                ),
                (
                    "clinic-afternoon",
                    [
                        "precepting-primary-1",
                        "precepting-primary-2",
                        "precepting-secondary",
                        "late-doc",
                    ],
                ),
            ],
        )
        self.assertTrue(
            all(
                len(position.days) == 5
                for group in week.clinic_groups
                for position in group.positions
            )
        )
        self.assertEqual(
            self.coverage(week, "interviews", self.monday, "AM").facts[0].record, shift
        )
        self.assertEqual(
            [
                coverage.period_of_day
                for coverage in week.clinic_groups[1].positions[-1].days[0].coverage
            ],
            ["PM"],
        )

    def morning_position_keys(self, on_date):
        return [
            position.key for position in master_schedule_week(on_date).clinic_groups[0].positions
        ]

    def test_interview_row_is_limited_to_its_season_and_open_january_work(self):
        october_shift = create_interview_shift(
            academic_year=2024, date=date(2024, 10, 7), scheduler=self.scheduler
        )
        create_interview_shift(academic_year=2024, date=date(2025, 1, 13), scheduler=self.scheduler)

        self.assertNotIn("interviews", self.morning_position_keys(date(2024, 9, 23)))
        self.assertIn("interviews", self.morning_position_keys(date(2024, 10, 7)))
        self.assertEqual(
            self.coverage(
                master_schedule_week(date(2024, 10, 7)), "interviews", october_shift.date, "AM"
            )
            .facts[0]
            .record,
            october_shift,
        )
        self.assertIn("interviews", self.morning_position_keys(date(2024, 12, 23)))
        self.assertIn("interviews", self.morning_position_keys(date(2025, 1, 6)))
        self.assertNotIn("interviews", self.morning_position_keys(date(2025, 1, 20)))
        self.assertNotIn("interviews", self.morning_position_keys(date(2025, 2, 3)))

    def test_interview_row_remains_for_an_empty_year_and_a_season_boundary_week(self):
        self.assertIn("interviews", self.morning_position_keys(date(2024, 9, 30)))
        self.assertIn("interviews", self.morning_position_keys(date(2026, 1, 5)))

    def test_availability_is_compact_and_sibling_occupancy_is_not_availability(self):
        preceptor = self.faculty("Preceptor")
        interviewer = self.faculty("Interviewer", monday_am="backup_only")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assignment = assign_preceptor(
            session=session,
            physician=preceptor,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(
            academic_year=2024, date=self.monday, scheduler=self.scheduler
        )
        assign_interview_shift(shift=shift, physician=interviewer, scheduler=self.scheduler)

        coverage = self.coverage(master_schedule_week(self.monday), "interviews", self.monday, "AM")
        candidates = {candidate.physician: candidate for candidate in coverage.candidates}

        self.assertEqual(candidates[preceptor].availability, "available")
        self.assertEqual(candidates[preceptor].occupancy[0].record, assignment)
        self.assertEqual(candidates[interviewer].availability, "backup_only")
        self.assertEqual(candidates[interviewer].occupancy[0].record, shift)
        self.assertEqual(
            candidates[interviewer].availability_sources[0].source_type,
            "faculty_template",
        )

    def test_sibling_conflicts_use_the_shared_display_contract(self):
        source = object()
        physician = object()
        facts = _occupancy(
            SimpleNamespace(
                physician=physician,
                preceptor_assignment=None,
                interview_shift=None,
                precepting_conflicts=(
                    PreceptingConflict(
                        "another_sibling_commitment",
                        "another_sibling_source",
                        "source-1",
                        source,
                        {"source": "source-1"},
                        "Another sibling commitment",
                        "Blocked by another sibling schedule.",
                    ),
                ),
            )
        )

        self.assertEqual(len(facts), 1)
        self.assertEqual(
            (
                facts[0].kind,
                facts[0].source_id,
                facts[0].record,
                facts[0].label,
                facts[0].explanation,
                facts[0].physician,
            ),
            (
                "another_sibling_source",
                "source-1",
                source,
                "Another sibling commitment",
                "Blocked by another sibling schedule.",
                physician,
            ),
        )

    def test_role_boundaries_focus_and_retained_references_do_not_change_truth(self):
        expired = self.physician("Expired", role=EffectiveRole.Role.FACULTY, end=date(2025, 2, 2))
        focused = self.faculty("Focused")
        retained = self.physician("Retained")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        PreceptorAssignment.objects.create(
            session=session,
            physician=retained,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        all_physicians = master_schedule_week(self.monday)
        focused_projection = master_schedule_week(self.monday, physician_focus_ids=(focused.pk,))
        coverage = self.coverage(focused_projection, "precepting-primary-1", self.monday, "AM")

        self.assertNotIn(expired, [value.physician for value in all_physicians.physicians])
        self.assertIn(retained, [value.physician for value in all_physicians.physicians])
        self.assertEqual(len(coverage.facts), 1)
        self.assertEqual(coverage.displayed_facts, ())
        self.assertEqual(
            [candidate.physician for candidate in coverage.displayed_candidates], [focused]
        )
        self.assertEqual(
            [slot.key for slot in focused_projection.slots],
            [slot.key for slot in all_physicians.slots],
        )
        self.assertEqual(
            [row.physician.physician for row in focused_projection.faculty_availability], [focused]
        )
        self.assertTrue(focused_projection.faculty_availability[0].physician.is_within_focus)

    def test_faculty_availability_retains_period_sources_and_focus_does_not_change_rows(self):
        focused = self.faculty("Focused", monday_am="backup_only")
        outside = self.faculty("Outside")

        projection = master_schedule_week(self.monday, physician_focus_ids=(focused.pk,))

        self.assertEqual(
            [row.physician.physician for row in projection.faculty_availability], [focused, outside]
        )
        self.assertEqual(
            [row.physician.is_within_focus for row in projection.faculty_availability],
            [True, False],
        )
        focused_monday = projection.faculty_availability[0].days[0]
        self.assertEqual(
            [(period.period_of_day, period.availability) for period in focused_monday.periods],
            [("AM", "backup_only"), ("PM", "available")],
        )
        self.assertEqual(focused_monday.periods[0].sources[0].source_type, "faculty_template")

    def test_secondary_candidates_require_effective_faculty_role(self):
        community = self.physician(
            "Community",
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
        )
        faculty = self.faculty("Faculty")
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )

        week = master_schedule_week(self.monday)
        primary = self.coverage(week, "precepting-primary-1", self.monday, "AM")
        secondary = self.coverage(week, "precepting-secondary", self.monday, "AM")

        self.assertEqual(
            [candidate.physician for candidate in primary.candidates], [community, faculty]
        )
        self.assertEqual([candidate.physician for candidate in secondary.candidates], [faculty])

    def test_supporting_sources_keep_coverage_provenance_and_operational_attention(self):
        participant = self.faculty("Participant")
        vacationing = self.physician("Vacationing")
        community = self.physician("Community")
        normal_rounder = self.faculty("Normal rounder")
        casual_rounder = self.physician("Casual rounder")
        definition = create_academic_event_definition(
            base_name="Academic Event",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=2,
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday + date.resolution,
            participants=(participant,),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        vacation = create_vacation(
            physician=vacationing,
            start_date=self.monday,
            end_date=self.monday + date.resolution,
            period_of_day="PM",
            scheduler=self.scheduler,
            note="Conference recovery",
        )
        community_rule = create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["PM"],
            scheduler=self.scheduler,
        )
        normal = record_normal_rounder(
            week=self.monday, physician=normal_rounder, scheduler=self.scheduler
        )
        casual_rule = create_casual_rounder_rule(
            physician=casual_rounder, weekday=0, ordinals=[1], scheduler=self.scheduler
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            facts = master_schedule_week(self.monday).supporting_facts

        academic = next(fact for fact in facts if fact.kind == "academic_event")
        self.assertEqual(
            (
                academic.source_id,
                academic.record,
                academic.physician,
                academic.periods,
                academic.attention,
            ),
            (event.pk, event, participant, ("AM",), "incomplete"),
        )
        self.assertEqual(
            (academic.start_date, academic.end_date), (self.monday, self.monday + date.resolution)
        )
        vacation_fact = next(fact for fact in facts if fact.kind == "vacation")
        self.assertEqual(
            (vacation_fact.record, vacation_fact.physician, vacation_fact.periods),
            (vacation, vacationing, ("PM",)),
        )
        self.assertEqual(vacation_fact.explanation, "Conference recovery")
        community_fact = next(fact for fact in facts if fact.kind == "community_preceptor_rule")
        self.assertEqual(
            (community_fact.record, community_fact.physician, community_fact.periods),
            (community_rule, community, ("PM",)),
        )
        normal_fact = next(fact for fact in facts if fact.kind == "normal_rounder")
        self.assertEqual(
            (
                normal_fact.record,
                normal_fact.physician,
                normal_fact.start_date,
                normal_fact.end_date,
            ),
            (normal, normal_rounder, self.monday, date(2025, 2, 9)),
        )
        casual_fact = next(fact for fact in facts if fact.kind == "casual_rounder_rule")
        self.assertEqual(
            (casual_fact.record, casual_fact.physician, casual_fact.periods),
            (casual_rule, casual_rounder, ("AM", "PM")),
        )
        self.assertEqual((casual_fact.start_date, casual_fact.end_date), (self.monday, self.monday))
        self.assertEqual(
            len(
                {
                    (fact.kind, fact.source_id, fact.physician, fact.start_date, fact.periods)
                    for fact in facts
                }
            ),
            len(facts),
        )
        self.assertEqual(
            [
                (group.key, {fact.kind for fact in group.facts})
                for group in master_schedule_week(self.monday).supporting_groups
            ],
            [
                ("rounding", {"normal_rounder", "casual_rounder_rule"}),
                ("community-precepting", {"community_preceptor_rule"}),
                ("vacation", {"vacation"}),
                ("academic-events", {"academic_event"}),
            ],
        )

    def test_operational_findings_target_the_affected_clinic_positions(self):
        first = self.faculty("First", monday_am="backup_only")
        second = self.faculty("Second")
        sessions = open_weekly_draft(self.monday)
        am = next(session for session in sessions if session.period_of_day == "AM")
        assignment = assign_preceptor(
            session=am,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(
            academic_year=2024, date=self.monday, scheduler=self.scheduler
        )
        assign_interview_shift(shift=shift, physician=second, scheduler=self.scheduler)
        assign_preceptor(
            session=am,
            physician=second,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(self.monday).findings

        backup = next(
            finding
            for finding in findings
            if finding.code == "backup_only_assignment" and finding.physicians == (first,)
        )
        self.assertEqual(backup.sources[0].source_type, "faculty_template")
        self.assertEqual(len(backup.targets), 1)
        self.assertEqual(
            (
                backup.targets[0].group_key,
                backup.targets[0].row_key,
                backup.targets[0].on_date,
                backup.targets[0].period_of_day,
            ),
            ("clinic-morning", "precepting-primary-1", self.monday, "AM"),
        )
        precepting_conflict = next(
            finding
            for finding in findings
            if finding.code == "interview_assignment_conflict" and finding.physicians == (second,)
        )
        self.assertEqual(
            [
                (
                    target.group_key,
                    target.row_key,
                    target.on_date,
                    target.period_of_day,
                )
                for target in precepting_conflict.targets
            ],
            [("clinic-morning", "precepting-primary-2", self.monday, "AM")],
        )
        interview_conflict = next(
            finding for finding in findings if finding.code == "preceptor_assignment_conflict"
        )
        self.assertEqual(interview_conflict.targets[0].row_key, "interviews")
        self.assertEqual(interview_conflict.targets[0].on_date, self.monday)
        self.assertEqual(assignment.physician, first)

    def test_every_live_finding_family_is_retained_at_its_exact_master_target(self):
        first = self.faculty("First", monday_am="backup_only")
        second = self.faculty("Second", monday_pm="off")
        community = self.physician("Community", role=EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        invalid = self.physician("Invalid")
        sessions = {
            (session.date, session.period_of_day): session
            for session in open_weekly_draft(self.monday)
        }
        for offset in range(5):
            on_date = self.monday + timedelta(days=offset)
            for period in ("AM", "PM"):
                session = sessions[on_date, period]
                PreceptorAssignment.objects.create(
                    session=session,
                    physician=first,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=1,
                )
                if (on_date, period) == (self.monday + timedelta(days=1), "AM"):
                    continue
                physician = second
                if (on_date, period) == (self.monday + timedelta(days=2), "AM"):
                    physician = community
                elif (on_date, period) == (self.monday + timedelta(days=3), "AM"):
                    physician = invalid
                PreceptorAssignment.objects.create(
                    session=session,
                    physician=physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=2,
                )
        PreceptorAssignment.objects.create(
            session=sessions[self.monday + timedelta(days=4), "AM"],
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(
            academic_year=2024, date=self.monday, scheduler=self.scheduler
        )
        assign_interview_shift(
            shift=shift, physician=first, scheduler=self.scheduler, override=True
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(self.monday).findings

        def targets(code, *, physician=None, on_date):
            matching = [
                finding
                for finding in findings
                if finding.code == code
                and (physician is None or finding.physicians == (physician,))
                and any(target.on_date == on_date for target in finding.targets)
            ]
            return [
                (target.group_key, target.row_key, target.on_date, target.period_of_day)
                for finding in matching
                for target in finding.targets
            ]

        self.assertEqual(
            targets("missing_primary_position", on_date=self.monday + timedelta(days=1)),
            [
                (
                    "clinic-morning",
                    "precepting-primary-2",
                    self.monday + timedelta(days=1),
                    "AM",
                )
            ],
        )
        self.assertEqual(
            targets(
                "insufficient_faculty_equivalent_coverage", on_date=self.monday + timedelta(days=1)
            ),
            [],
        )
        self.assertEqual(
            targets(
                "role_ineligible_assignment",
                physician=invalid,
                on_date=self.monday + timedelta(days=3),
            ),
            [
                (
                    "clinic-morning",
                    "precepting-primary-2",
                    self.monday + timedelta(days=3),
                    "AM",
                )
            ],
        )
        self.assertEqual(
            targets(
                "role_responsibility_mismatch",
                physician=community,
                on_date=self.monday + timedelta(days=4),
            ),
            [
                (
                    "clinic-morning",
                    "precepting-secondary",
                    self.monday + timedelta(days=4),
                    "AM",
                )
            ],
        )
        self.assertEqual(
            targets("not_available_assignment", physician=second, on_date=self.monday),
            [("clinic-afternoon", "precepting-primary-2", self.monday, "PM")],
        )
        self.assertEqual(
            targets("backup_only_assignment", physician=first, on_date=self.monday),
            [
                ("clinic-morning", "precepting-primary-1", self.monday, "AM"),
                ("clinic-morning", "interviews", self.monday, "AM"),
            ],
        )
        self.assertEqual(
            targets("unassigned_community_commitment", physician=community, on_date=self.monday),
            [
                ("clinic-morning", row, self.monday, "AM")
                for row in (
                    "precepting-primary-1",
                    "precepting-primary-2",
                    "precepting-secondary",
                )
            ],
        )
        self.assertEqual(
            targets("missing_normal_rounder", on_date=self.monday),
            [("rounding", "normal-rounding", self.monday, None)],
        )
        self.assertEqual(
            targets("missing_late_doc", on_date=self.monday),
            [("clinic-afternoon", "late-doc", self.monday, "PM")],
        )
        self.assertEqual(
            targets("interview_assignment_conflict", physician=first, on_date=self.monday),
            [("clinic-morning", "precepting-primary-1", self.monday, "AM")],
        )
        self.assertEqual(
            targets("preceptor_assignment_conflict", physician=first, on_date=self.monday),
            [("clinic-morning", "interviews", self.monday, "AM")],
        )
        backup_sources = [
            source.source_type
            for finding in findings
            if finding.code == "backup_only_assignment" and finding.physicians == (first,)
            for source in finding.sources
        ]
        self.assertEqual(backup_sources, ["faculty_template", "faculty_template"])
        community_finding = next(
            finding
            for finding in findings
            if finding.code == "unassigned_community_commitment"
            and finding.physicians == (community,)
        )
        self.assertEqual(community_finding.sources[0].source_type, "community_preceptor_rule")

    def test_holiday_assignment_finding_targets_the_closed_clinic_period(self):
        holiday_week = date(2025, 12, 22)
        physician = self.physician("Holiday assignment")
        session = next(
            session
            for session in open_weekly_draft(holiday_week)
            if (session.date, session.period_of_day) == (date(2025, 12, 24), "PM")
        )
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(holiday_week).findings

        holiday_finding = next(
            finding
            for finding in findings
            if finding.code == "holiday_closed_assignment" and finding.physicians == (physician,)
        )
        self.assertEqual(
            [
                (target.group_key, target.row_key, target.on_date, target.period_of_day)
                for target in holiday_finding.targets
            ],
            [("clinic-afternoon", "precepting-primary-1", date(2025, 12, 24), "PM")],
        )

    def test_holidays_suppress_community_occurrences_without_materializing_closures(self):
        christmas_week = date(2025, 12, 22)
        community = self.physician("Community")
        create_community_preceptor_rule(
            physician=community,
            weekday=2,
            ordinals=[4],
            periods=["PM"],
            scheduler=self.scheduler,
        )
        before = HolidayOccurrence.objects.count()

        facts = master_schedule_week(christmas_week).supporting_facts

        holiday = next(fact for fact in facts if fact.kind == "holiday_closure")
        self.assertEqual(
            (holiday.label, holiday.start_date, holiday.end_date, holiday.periods),
            ("Christmas Eve", date(2025, 12, 24), date(2025, 12, 24), ("PM",)),
        )
        self.assertFalse(any(fact.kind == "community_preceptor_rule" for fact in facts))
        self.assertEqual(HolidayOccurrence.objects.count(), before)

    def test_historical_supporting_facts_remain_visible_without_new_attention(self):
        participant = self.physician("Participant")
        definition = create_academic_event_definition(
            base_name="Academic Event",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=2,
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(participant,),
            scheduler=self.scheduler,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            current_facts = master_schedule_week(self.monday).supporting_facts
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 10, 18, tzinfo=UTC),
        ):
            facts = master_schedule_week(self.monday).supporting_facts
            findings = master_schedule_week(self.monday).findings

        normal = next(fact for fact in current_facts if fact.kind == "normal_rounder")
        self.assertEqual(normal.attention, "missing")
        academic = next(fact for fact in facts if fact.kind == "academic_event")
        self.assertEqual((academic.record, academic.attention), (event, ""))
        self.assertFalse(any(fact.kind == "normal_rounder" for fact in facts))
        self.assertEqual(findings, ())

    def test_midweek_projection_keeps_earlier_precepting_late_doc_and_rounding_findings(self):
        open_weekly_draft(self.monday)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(self.monday).findings

        missing_coverage_dates = {
            target.on_date
            for finding in findings
            if finding.code == "missing_primary_position"
            for target in finding.targets
        }
        finding_codes = {finding.code for finding in findings}
        self.assertIn(self.monday, missing_coverage_dates)
        self.assertIn(self.monday + timedelta(days=1), missing_coverage_dates)
        self.assertIn(self.monday + timedelta(days=2), missing_coverage_dates)
        self.assertIn(self.monday + timedelta(days=4), missing_coverage_dates)
        self.assertIn("missing_late_doc", finding_codes)
        self.assertIn("missing_normal_rounder", finding_codes)

    def test_midweek_projection_keeps_earlier_interview_findings(self):
        physician = self.faculty("Interview", monday_am="backup_only")
        shift = create_interview_shift(
            academic_year=2024, date=self.monday, scheduler=self.scheduler
        )
        assign_interview_shift(shift=shift, physician=physician, scheduler=self.scheduler)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(self.monday).findings

        interview_finding = next(
            finding
            for finding in findings
            if finding.code == "backup_only_assignment" and finding.physicians == (physician,)
        )
        self.assertEqual(interview_finding.targets[0].on_date, self.monday)
        self.assertEqual(interview_finding.targets[0].row_key, "interviews")

    def test_future_week_projects_its_complete_clinic_findings(self):
        future_monday = self.monday + timedelta(days=14)
        open_weekly_draft(future_monday)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
        ):
            findings = master_schedule_week(future_monday).findings

        missing_coverage_dates = {
            target.on_date
            for finding in findings
            if finding.code == "missing_primary_position"
            for target in finding.targets
        }
        self.assertIn(future_monday, missing_coverage_dates)
        self.assertIn(future_monday + timedelta(days=4), missing_coverage_dates)
        self.assertIn("missing_late_doc", {finding.code for finding in findings})

    def test_projection_is_read_only_and_bounded_for_a_week(self):
        self.faculty("First")
        self.faculty("Second")
        community = self.physician("Community")
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        before = {model: model.objects.count() for model in (PreceptorAssignment,)}

        with CaptureQueriesContext(connection) as queries:
            master_schedule_week(self.monday)

        self.assertLessEqual(len(queries), 29)
        self.assertEqual(before, {model: model.objects.count() for model in before})

    def test_projection_query_bound_includes_populated_preceptor_week(self):
        first = self.faculty("First")
        self.faculty("Second")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        with CaptureQueriesContext(connection) as queries:
            master_schedule_week(self.monday)

        self.assertLessEqual(len(queries), 29)
