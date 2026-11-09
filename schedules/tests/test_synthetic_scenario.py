from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import CommandError
from django.db import transaction
from django.test import TestCase, tag

from schedules.management.commands.reset_production_domain import _synthetic_summary
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    CustomAvailabilityCategory,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    HolidayOccurrence,
    InterviewShift,
    LateDocAssignment,
    LateDocAssignmentChange,
    LeadTimeChange,
    LeadTimeSetting,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    StipendRate,
    Vacation,
)
from schedules.services.interviews import create_interview_shift, interview_findings_for_shift
from schedules.services.lead_times import (
    ensure_lead_time_defaults,
    lead_time_months,
    set_lead_time,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import initialize_preceptor_session_range
from schedules.services.schedule_reports import (
    academic_event_schedule_report,
    casual_rounding_schedule_report,
    interview_schedule_report,
    normal_rounding_schedule_report,
    schedule_report_parameters,
    vacation_schedule_report,
)
from schedules.services.slot_projection import RequestedSlot, slot_projection
from schedules.services.stipend_rates import stipend_rates
from schedules.services.synthetic_scenario import (
    BASELINE_START,
    SyntheticScenario,
)
from schedules.services.work_remaining import global_work_remaining, shift_date_months

EXPECTED_FACULTY_NAMES = frozenset(
    {
        "Fake Faculty Doc 01",
        "Fake Faculty Doc 02",
        "Fake Faculty Doc 03",
        "Fake Faculty Doc 04",
        "Fake Faculty Doc 05",
        "Fake Faculty Doc 06",
        "Fake Faculty Doc 07",
        "Fake Faculty Doc 08",
        "Fake Faculty Doc 09",
        "Fake Faculty Doc 10",
        "Fake Faculty Doc 11",
        "Fake Faculty Doc 12",
        "Fake Faculty Doc 13",
        "Fake Faculty Doc 14",
        "Fake Faculty Doc 15",
        "Fake Faculty Doc 16",
    }
)
# TEXT-CONTRACT: synthetic-community-preceptor-identities — independently
# expected stable generated identity keys for the accepted synthetic baseline.
# ASCII capitalization, spaces, and the digits 1 through 3 are exact;
# docs/design.md "Accepted synthetic scenario baseline" and
# schedules/services/synthetic_scenario.py define and protect the contract.
EXPECTED_COMMUNITY_PRECEPTOR_NAMES = frozenset(
    {"Fake Community Doc 1", "Fake Community Doc 2", "Fake Community Doc 3"}
)
EXPECTED_TEMPLATE_SLOTS = frozenset(
    {
        "monday_am",
        "monday_pm",
        "tuesday_am",
        "tuesday_pm",
        "wednesday_am",
        "wednesday_pm",
        "thursday_am",
        "thursday_pm",
        "friday_am",
        "friday_pm",
    }
)


class SyntheticScenarioBaselineTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            username="scenario-administrator", email="scenario@example.com"
        )
        self.actor.groups.add(Group.objects.get(name="Scheduling Administrators"))
        LeadTimeSetting.objects.all().delete()

    def scenario(self, seed="baseline-seed"):
        return SyntheticScenario(actor=self.actor, seed=seed)

    def test_establishes_imported_faculty_baseline_and_generated_community_inputs(self):
        baseline = self.scenario().establish_baseline()

        self.assertEqual(
            {physician.display_name for physician in baseline.faculty_physicians},
            EXPECTED_FACULTY_NAMES,
        )
        templates = tuple(FacultyAvailabilityTemplate.objects.select_related("physician"))
        self.assertEqual(len(templates), len({template.physician_id for template in templates}))
        templates_by_physician = {
            template.physician.display_name: template for template in templates
        }
        self.assertEqual(set(templates_by_physician), EXPECTED_FACULTY_NAMES)
        self.assertEqual(
            {template.effective_start for template in templates_by_physician.values()},
            {date(2020, 7, 1)},
        )
        self.assertTrue(
            all(
                set(template.slots) == EXPECTED_TEMPLATE_SLOTS
                for template in templates_by_physician.values()
            )
        )
        self.assertEqual(
            templates_by_physician["Fake Faculty Doc 01"].slots["monday_am"], {"base_state": "off"}
        )
        self.assertEqual(
            templates_by_physician["Fake Faculty Doc 01"].slots["monday_pm"],
            {"base_state": "patient_care"},
        )
        self.assertEqual(
            templates_by_physician["Fake Faculty Doc 07"].slots["tuesday_am"]["category_name"],
            "Rounds",
        )
        self.assertEqual(
            templates_by_physician["Fake Faculty Doc 14"].slots["tuesday_pm"]["category_name"],
            "Rounds",
        )
        self.assertEqual(CustomAvailabilityCategory.objects.get(name="Rounds").name, "Rounds")

        self.assertEqual(
            {physician.display_name for physician in baseline.community_preceptors},
            EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
        )
        community_roles = EffectiveRole.objects.filter(
            physician__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=BASELINE_START,
            effective_end=None,
        )
        self.assertEqual(community_roles.count(), 3)
        roles_by_physician = {}
        for name, role, faculty_equivalent in EffectiveRole.objects.filter(
            physician__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES
        ).values_list("physician__display_name", "role", "faculty_equivalent"):
            roles_by_physician.setdefault(name, []).append((role, faculty_equivalent))
        self.assertEqual(
            set(roles_by_physician),
            EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
        )
        self.assertEqual(
            {frozenset(roles) for roles in roles_by_physician.values()},
            {
                frozenset({(EffectiveRole.Role.COMMUNITY_PRECEPTOR, False)}),
                frozenset({(EffectiveRole.Role.COMMUNITY_PRECEPTOR, True)}),
                frozenset(
                    {
                        (EffectiveRole.Role.COMMUNITY_PRECEPTOR, True),
                        (EffectiveRole.Role.CASUAL_ROUNDER, False),
                    }
                ),
            },
        )
        self.assertEqual(
            EffectiveRole.objects.filter(
                physician__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
                role=EffectiveRole.Role.CASUAL_ROUNDER,
                effective_start=BASELINE_START,
                effective_end=None,
            ).count(),
            1,
        )
        rules = CommunityPreceptorRule.objects.filter(
            physician__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
            effective_start=BASELINE_START,
            effective_end=None,
        )
        self.assertEqual(rules.count(), 3)
        self.assertEqual(len(set(rules.values_list("weekday", flat=True))), 3)
        self.assertEqual(
            {
                (weekday, tuple(ordinals), tuple(periods))
                for weekday, ordinals, periods in rules.values_list(
                    "weekday", "ordinals", "periods"
                )
            },
            {
                (4, (1, 3, 5), ("AM",)),
                (0, (1, 3), ("AM", "PM")),
                (2, (2, 4), ("PM",)),
            },
        )
        self.assertEqual(
            set(PhysicianChange.objects.values_list("changed_by_id", flat=True)), {self.actor.pk}
        )
        self.assertEqual(
            set(CommunityPreceptorScheduleChange.objects.values_list("changed_by_id", flat=True)),
            {self.actor.pk},
        )

    def test_rejects_a_nonempty_scheduling_domain_before_creating_baseline_inputs(self):
        create_physician(
            display_name="Existing scheduling record", scheduler=self.actor, reason="Test setup"
        )
        before_physicians = tuple(
            Physician.objects.order_by("pk").values_list(
                "pk", "display_name", "user_id", "faculty_precepting_capacity_override"
            )
        )
        before_physician_changes = tuple(
            PhysicianChange.objects.order_by("pk").values_list(
                "physician_id",
                "subject",
                "subject_id",
                "previous_value",
                "recorded_value",
                "reason",
                "changed_by_id",
            )
        )

        with self.assertRaisesRegex(ValidationError, "requires an empty scheduling domain"):
            self.scenario().establish_baseline()

        self.assertEqual(
            tuple(
                Physician.objects.order_by("pk").values_list(
                    "pk", "display_name", "user_id", "faculty_precepting_capacity_override"
                )
            ),
            before_physicians,
        )
        self.assertEqual(
            tuple(
                PhysicianChange.objects.order_by("pk").values_list(
                    "physician_id",
                    "subject",
                    "subject_id",
                    "previous_value",
                    "recorded_value",
                    "reason",
                    "changed_by_id",
                )
            ),
            before_physician_changes,
        )
        self.assertFalse(
            Physician.objects.filter(display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES).exists()
        )
        self.assertFalse(FacultyAvailabilityTemplate.objects.exists())

    def test_uses_existing_lead_time_policy_when_creating_baseline_inputs(self):
        ensure_lead_time_defaults()
        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=9,
            scheduler=self.actor,
        )

        baseline = self.scenario().establish_baseline()

        self.assertEqual(LeadTimeSetting.objects.count(), 5)
        self.assertEqual(lead_time_months(LeadTimeSetting.Family.PRECEPTING), 9)
        self.assertEqual(LeadTimeChange.objects.count(), 1)
        self.assertEqual(len(baseline.faculty_physicians), 16)

    def test_preserves_authentication_owned_records_and_does_not_link_generated_physicians(self):
        preserved = get_user_model().objects.create_user(
            username="preserved-login", email="preserved@example.com"
        )
        before_users = list(get_user_model().objects.order_by("pk").values())
        before_groups = list(
            Group.objects.order_by("pk").values_list("pk", "name", "permissions__pk")
        )

        self.scenario().establish_baseline()

        self.assertEqual(list(get_user_model().objects.order_by("pk").values()), before_users)
        self.assertEqual(
            list(Group.objects.order_by("pk").values_list("pk", "name", "permissions__pk")),
            before_groups,
        )
        self.assertFalse(Physician.objects.filter(user__in=(self.actor, preserved)).exists())

    def test_stable_choices_use_the_seed_and_domain_physician_and_period_keys(self):
        scenario = self.scenario("fixed-seed")
        first_choice = scenario.choice(
            domain="vacation",
            physician="Fake Faculty Doc 01",
            period=date(2020, 7, 1),
            choices=range(10),
        )

        self.assertEqual(
            first_choice,
            self.scenario("fixed-seed").choice(
                domain="vacation",
                physician="Fake Faculty Doc 01",
                period=date(2020, 7, 1),
                choices=range(10),
            ),
        )
        scenario.choice(
            domain="interview", physician="faculty", period=date(2020, 9, 1), choices=range(3)
        )
        self.assertEqual(
            scenario.choice(
                domain="vacation",
                physician="Fake Faculty Doc 01",
                period=date(2020, 7, 1),
                choices=range(10),
            ),
            first_choice,
        )


class SyntheticScenarioSourceHistoryTests(TestCase):
    today = date(2020, 7, 15)

    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            username="scenario-administrator", email="scenario@example.com"
        )
        self.actor.groups.add(Group.objects.get(name="Scheduling Administrators"))
        LeadTimeSetting.objects.all().delete()
        self.baseline = self.scenario("source-history-seed").establish_baseline()
        self.scenario("source-history-seed").generate_source_schedule_history(
            self.baseline, today=self.today
        )

    def scenario(self, seed="baseline-seed"):
        return SyntheticScenario(actor=self.actor, seed=seed)

    def test_generates_complete_source_history_and_attention_horizon_facts(self):
        self.assertFalse(
            AcademicEvent.objects.filter(
                participants__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES
            ).exists()
        )
        annual_definitions = AcademicEventDefinition.objects.filter(
            start_academic_year=2020,
            scheduling_mode=AcademicEventDefinition.SchedulingMode.YEARLY,
        )
        self.assertTrue(
            {"Q1", "Q2", "Q3", "Q4"}
            <= set(annual_definitions.values_list("expected_quarter", flat=True))
        )
        self.assertTrue(
            {"AM", "PM", "whole_day"}
            <= set(annual_definitions.values_list("default_period_of_day", flat=True))
        )
        self.assertTrue(
            all(
                definition.default_required_participant_count > 0
                for definition in annual_definitions
            )
        )
        annual_occurrences = AcademicEvent.objects.filter(
            definition__in=annual_definitions, academic_year=2020
        )
        self.assertTrue(annual_occurrences.exists())
        self.assertTrue(annual_occurrences.filter(participants__isnull=False).exists())
        self.assertTrue(AcademicEvent.objects.exists())
        individual_monthly = AcademicEvent.objects.filter(
            definition__base_name="Monthly Faculty Workshop"
        )
        collective_monthly = AcademicEvent.objects.filter(
            definition__base_name="Monthly Faculty Forum"
        )
        self.assertTrue(individual_monthly.exists())
        self.assertTrue(collective_monthly.exists())
        self.assertTrue(
            all(
                event.assignment_is_default
                and not event.all_available_faculty
                and event.participants.count() == 2
                for event in individual_monthly
            )
        )
        self.assertTrue(
            all(
                event.all_available_faculty and not event.participants.exists()
                for event in collective_monthly
            )
        )
        self.assertTrue(Vacation.objects.filter(period_of_day="whole_day").exists())
        self.assertEqual(CasualRounderRule.objects.count(), 1)
        self.assertTrue(NormalRounder.objects.filter(week__gte=BASELINE_START).exists())
        self.assertEqual(InterviewShift.objects.filter(physician__isnull=False).count(), 20)
        for kind in StipendRate.Kind.values:
            rates = list(stipend_rates(kind))
            self.assertEqual(len(rates), 3)
            self.assertIsNone(rates[0].effective_start)
            self.assertIsNone(rates[-1].effective_end)
            self.assertEqual(rates[0].effective_end + date.resolution, rates[1].effective_start)
            self.assertEqual(rates[1].effective_end + date.resolution, rates[2].effective_start)

    def test_interviews_use_available_faculty_without_findings(self):
        shifts = InterviewShift.objects.filter(physician__isnull=False).order_by("date", "pk")

        self.assertEqual(shifts.count(), 20)
        self.assertEqual(
            [
                (shift.pk, finding.code, finding.severity)
                for shift in shifts
                for finding in interview_findings_for_shift(shift).findings
            ],
            [],
        )

    def test_schedules_manually_movable_holidays_within_the_source_and_attention_bounds(self):
        horizon_end = shift_date_months(
            self.today, lead_time_months(LeadTimeSetting.Family.HOLIDAYS)
        )
        scheduled = HolidayOccurrence.objects.filter(state=HolidayOccurrence.State.SCHEDULED)

        self.assertTrue(scheduled.exists())
        self.assertFalse(scheduled.filter(nominal_date__lt=BASELINE_START).exists())
        self.assertFalse(scheduled.filter(nominal_date__gt=horizon_end).exists())

    def test_source_reports_have_meaningful_calendar_and_academic_year_coverage(self):
        reports = (
            (
                "academic-events",
                academic_event_schedule_report,
                EXPECTED_FACULTY_NAMES,
                lambda row: row.events > 0 and row.event_days > 0 and row.affected_shifts > 0,
            ),
            (
                "interviews",
                interview_schedule_report,
                EXPECTED_FACULTY_NAMES,
                lambda row: row.shifts > 0,
            ),
            (
                "normal-rounding",
                normal_rounding_schedule_report,
                EXPECTED_FACULTY_NAMES,
                lambda row: row.weeks > 0,
            ),
            (
                "casual-rounding",
                casual_rounding_schedule_report,
                EXPECTED_COMMUNITY_PRECEPTOR_NAMES,
                lambda row: row.days > 0,
            ),
            (
                "vacation",
                vacation_schedule_report,
                EXPECTED_FACULTY_NAMES,
                lambda row: row.ranges > 0 and row.weekdays > 0 and row.total_days > 0,
            ),
        )
        for schedule, report_function, expected_physicians, has_meaningful_coverage in reports:
            parameters = schedule_report_parameters(
                {
                    "schedule": schedule,
                    "period": "academic-year",
                    "academic_year": "2020",
                    "coverage": "history-and-scheduled",
                },
                today=self.today,
            )
            report = report_function(parameters)
            self.assertTrue(report.rows, schedule)
            self.assertTrue(
                any(
                    row.physician.display_name in expected_physicians
                    and has_meaningful_coverage(row)
                    for row in report.rows
                ),
                schedule,
            )

    def test_source_absences_preserve_faculty_equivalent_coverage(self):
        affected_days = set()
        for vacation in Vacation.objects.all():
            affected_days.update(
                vacation.start_date + date.resolution * offset
                for offset in range((vacation.end_date - vacation.start_date).days + 1)
                if (vacation.start_date + date.resolution * offset).weekday() < 5
            )
        for event in AcademicEvent.objects.filter(resolution="dated"):
            affected_days.update(
                event.start_date + date.resolution * offset
                for offset in range((event.end_date - event.start_date).days + 1)
                if (event.start_date + date.resolution * offset).weekday() < 5
            )
        requested_slots = tuple(
            RequestedSlot(on_date, period) for on_date in affected_days for period in ("AM", "PM")
        )
        rows = slot_projection(requested_slots)
        counts = {slot: 0 for slot in requested_slots}
        for row in rows:
            if row.faculty_equivalent and row.availability != "not_available":
                counts[row.slot] = counts.get(row.slot, 0) + 1
        self.assertTrue(requested_slots)
        self.assertTrue(
            all(count >= 2 for count in counts.values()),
            {slot: count for slot, count in counts.items() if count < 2},
        )

    @tag("synthetic-scenario-integration")
    def test_later_generation_extends_stable_source_history_without_reshuffling(self):
        before = tuple(
            Vacation.objects.order_by("physician_id", "start_date", "pk").values_list(
                "physician_id", "start_date", "end_date", "period_of_day"
            )
        )

        self.scenario("source-history-seed").generate_source_schedule_history(
            self.baseline, today=date(2027, 4, 2)
        )

        self.assertEqual(
            tuple(
                Vacation.objects.filter(end_date__lte=date(2020, 7, 31))
                .order_by("physician_id", "start_date", "pk")
                .values_list("physician_id", "start_date", "end_date", "period_of_day")
            ),
            before,
        )
        self.assertTrue(NormalRounder.objects.filter(week__gte=date(2027, 8, 1)).exists())


class SyntheticScenarioConfiguredHolidayHorizonTests(TestCase):
    today = date(2020, 7, 1)

    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            username="scenario-administrator", email="scenario@example.com"
        )
        self.actor.groups.add(Group.objects.get(name="Scheduling Administrators"))
        LeadTimeSetting.objects.all().delete()
        baseline = self.scenario().establish_baseline()
        ensure_lead_time_defaults()
        set_lead_time(
            family=LeadTimeSetting.Family.HOLIDAYS,
            months=6,
            scheduler=self.actor,
        )
        self.scenario().generate_source_schedule_history(baseline, today=self.today)

    def scenario(self):
        return SyntheticScenario(actor=self.actor, seed="configured-holiday-horizon")

    def test_schedules_holidays_through_the_configured_non_default_attention_horizon(self):
        horizon_end = shift_date_months(
            self.today, lead_time_months(LeadTimeSetting.Family.HOLIDAYS)
        )
        scheduled = HolidayOccurrence.objects.filter(state=HolidayOccurrence.State.SCHEDULED)

        self.assertEqual(horizon_end, date(2021, 1, 1))
        self.assertEqual(
            HolidayOccurrence.objects.get(holiday="new_years_day", calendar_year=2021).state,
            HolidayOccurrence.State.SCHEDULED,
        )
        self.assertFalse(scheduled.filter(nominal_date__gt=horizon_end).exists())


class SyntheticScenarioAvailabilityTests(TestCase):
    on_date = date(2025, 2, 3)

    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            username="scenario-administrator", email="scenario@example.com"
        )
        self.actor.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def faculty(self, name, *, leadership=""):
        physician = create_physician(display_name=name, scheduler=self.actor)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 7, 1),
            effective_end=None,
            faculty_leadership=leadership,
            scheduler=self.actor,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.actor,
        )
        return physician

    def test_unassigned_interview_conflict_excludes_program_director_from_precepting_rows(self):
        director = self.faculty("Program Director", leadership="program_director")
        other_faculty = self.faculty("Other Faculty")
        create_interview_shift(academic_year=2024, date=self.on_date, scheduler=self.actor)

        rows = slot_projection((RequestedSlot(self.on_date, "AM"),))
        director_row = next(row for row in rows if row.physician == director)
        available_rows = SyntheticScenario(
            actor=self.actor, seed="availability-test"
        )._ordered_available_rows(rows, self.on_date, "AM", "primary")

        self.assertIsNone(director_row.interview_shift)
        self.assertTrue(director_row.precepting_conflicts)
        self.assertEqual([row.physician for row in available_rows], [other_faculty])


class SyntheticScenarioPreceptingHistoryTests(TestCase):
    first_today = date(2020, 7, 15)
    later_today = date(2020, 8, 15)

    @patch("schedules.services.operational_attention.organization_today", return_value=first_today)
    @patch("schedules.services.work_remaining.organization_today", return_value=first_today)
    @patch("schedules.services.synthetic_scenario.organization_today", return_value=first_today)
    def setUp(self, _scenario_today, _work_remaining_today, _operational_today):
        self.actor = get_user_model().objects.create_user(
            username="scenario-administrator", email="scenario@example.com"
        )
        self.actor.groups.add(Group.objects.get(name="Scheduling Administrators"))
        LeadTimeSetting.objects.all().delete()
        self.scenario = SyntheticScenario(actor=self.actor, seed="precepting-history-seed")
        self.baseline = self.scenario.establish_baseline()
        ensure_lead_time_defaults()
        self.scenario.generate_source_schedule_history(self.baseline, today=self.first_today)
        self.scenario.generate_precepting_history(self.baseline, today=self.first_today)

    def test_completes_the_current_month_and_leaves_every_later_initialized_week_open(self):
        future_start = date(2020, 8, 1)
        horizon_end = date(2020, 12, 4)

        self.assertFalse(
            PreceptorAssignment.objects.filter(
                session__date__range=(future_start, horizon_end)
            ).exists()
        )
        self.assertFalse(
            LateDocAssignment.objects.filter(date__range=(future_start, horizon_end)).exists()
        )
        self.assertTrue(
            PreceptorAssignment.objects.filter(
                session__date__range=(date(2020, 7, 27), date(2020, 7, 31))
            ).exists()
        )
        self.assertTrue(LateDocAssignment.objects.filter(date=date(2020, 7, 27)).exists())
        self.assertTrue(PreceptorAssignmentChange.objects.exists())

        community_assignments = PreceptorAssignment.objects.filter(
            physician__display_name__in=EXPECTED_COMMUNITY_PRECEPTOR_NAMES
        )
        self.assertTrue(community_assignments.exists())
        self.assertTrue(
            all(
                assignment.responsibility == PreceptorAssignment.Responsibility.PRIMARY
                for assignment in community_assignments
            )
        )
        late_docs = LateDocAssignment.objects.select_related("physician").all()
        self.assertTrue(late_docs.exists())
        self.assertTrue(
            all(
                assignment.physician.roles.filter(role=EffectiveRole.Role.FACULTY).exists()
                for assignment in late_docs
            )
        )

    @patch("schedules.services.operational_attention.organization_today", return_value=later_today)
    @patch("schedules.services.work_remaining.organization_today", return_value=later_today)
    @patch("schedules.services.synthetic_scenario.organization_today", return_value=later_today)
    def test_rerun_clears_future_assignments_with_history(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        self.scenario.generate_source_schedule_history(self.baseline, today=self.later_today)
        future_week = date(2020, 9, 14)
        initialize_preceptor_session_range(future_week, future_week + timedelta(days=4))
        for offset in range(5):
            self.scenario._fill_precepting_session(
                future_week + timedelta(days=offset), "Pre-rerun future assignments."
            )
        self.scenario._fill_late_doc(future_week, "Pre-rerun future assignments.")
        future_assignments = set(
            PreceptorAssignment.objects.filter(session__date__gte=future_week).values_list(
                "pk", flat=True
            )
        )
        self.assertTrue(future_assignments)
        late_doc_id = LateDocAssignment.objects.get(date=future_week).pk

        self.scenario.generate_precepting_history(self.baseline, today=self.later_today)

        self.assertFalse(
            PreceptorAssignment.objects.filter(session__date__gt=date(2020, 8, 31)).exists()
        )
        self.assertFalse(LateDocAssignment.objects.filter(date__gt=date(2020, 8, 31)).exists())
        self.assertEqual(
            set(
                PreceptorAssignmentChange.objects.filter(
                    assignment_id__in=future_assignments,
                    action=PreceptorAssignmentChange.Action.REMOVED,
                    changed_by=self.actor,
                ).values_list("assignment_id", flat=True)
            ),
            future_assignments,
        )
        self.assertTrue(
            LateDocAssignmentChange.objects.filter(
                assignment_id_at_recording=late_doc_id,
                action=LateDocAssignmentChange.Action.REMOVED,
                changed_by=self.actor,
            ).exists()
        )

    def test_reset_summary_uses_assignment_history(self):

        counts, historical_assignments, completion_end = _synthetic_summary(today=self.first_today)

        self.assertEqual(completion_end, date(2020, 7, 31))
        self.assertEqual(historical_assignments, PreceptorAssignment.objects.count())
        self.assertGreater(counts["assignment changes"], 0)
        self.assertGreater(counts["Late Doc assignments"], 0)
        self.assertGreater(counts["Late Doc changes"], 0)

    def test_reset_summary_rejects_missing_assignments_or_ordinary_history(self):
        for model in (
            PreceptorAssignment,
            PreceptorAssignmentChange,
            LateDocAssignment,
            LateDocAssignmentChange,
        ):
            with self.subTest(model=model._meta.label), transaction.atomic():
                model.objects.all().delete()
                with self.assertRaisesRegex(CommandError, "historical validation failed"):
                    _synthetic_summary(today=self.first_today)
                transaction.set_rollback(True)

    def test_reset_summary_rejects_real_future_precepting_and_late_doc_work(self):
        future_monday = date(2020, 8, 3)
        for fill in (self.scenario._fill_precepting_session, self.scenario._fill_late_doc):
            with self.subTest(fill=fill.__name__), transaction.atomic():
                fill(future_monday, "Future assignment for reset validation.")
                with self.assertRaisesRegex(CommandError, "later work is not empty"):
                    _synthetic_summary(today=self.first_today)
                transaction.set_rollback(True)

    @patch("schedules.services.operational_attention.organization_today", return_value=first_today)
    @patch("schedules.services.work_remaining.organization_today", return_value=first_today)
    @patch("schedules.services.synthetic_scenario.organization_today", return_value=first_today)
    def test_global_work_remaining_contains_every_future_precepting_week_and_nothing_else(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        self.scenario.generate_precepting_history(self.baseline)

        self.assertEqual(
            {item.key for item in global_work_remaining().items},
            {
                f"precepting-week-{week:%Y-%m-%d}"
                for week in (date(2020, 8, 3) + timedelta(days=7 * offset) for offset in range(20))
            },
        )

    @patch("schedules.services.operational_attention.organization_today", return_value=first_today)
    @patch("schedules.services.work_remaining.organization_today", return_value=first_today)
    @patch("schedules.services.synthetic_scenario.organization_today", return_value=first_today)
    def test_rejects_extra_non_precepting_global_work_remaining(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        def work_remaining_with_extra_item():
            summary = global_work_remaining()
            return SimpleNamespace(
                items=(*summary.items, SimpleNamespace(key="interview-week-2020-12-28"))
            )

        with patch(
            "schedules.services.work_remaining.global_work_remaining",
            side_effect=work_remaining_with_extra_item,
        ):
            with self.assertRaisesRegex(ValidationError, "does not match global Work Remaining"):
                self.scenario.generate_precepting_history(self.baseline, today=self.first_today)

    @patch("schedules.services.operational_attention.organization_today", return_value=later_today)
    @patch("schedules.services.work_remaining.organization_today", return_value=later_today)
    @patch("schedules.services.synthetic_scenario.organization_today", return_value=later_today)
    def test_later_generation_advances_the_boundary_without_reshuffling_completed_history(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        before = tuple(
            PreceptorAssignment.objects.filter(session__date__lte=date(2020, 7, 31))
            .order_by(
                "session__date", "session__period_of_day", "responsibility", "primary_position"
            )
            .values_list(
                "session__date",
                "session__period_of_day",
                "responsibility",
                "primary_position",
                "physician_id",
            )
        )

        self.scenario.generate_source_schedule_history(self.baseline, today=self.later_today)
        self.scenario.generate_precepting_history(self.baseline, today=self.later_today)

        self.assertEqual(
            tuple(
                PreceptorAssignment.objects.filter(session__date__lte=date(2020, 7, 31))
                .order_by(
                    "session__date", "session__period_of_day", "responsibility", "primary_position"
                )
                .values_list(
                    "session__date",
                    "session__period_of_day",
                    "responsibility",
                    "primary_position",
                    "physician_id",
                )
            ),
            before,
        )
        self.assertTrue(
            PreceptorAssignment.objects.filter(
                session__date__range=(date(2020, 8, 1), date(2020, 8, 31))
            ).exists()
        )
        self.assertTrue(LateDocAssignment.objects.filter(date=date(2020, 8, 3)).exists())
        self.assertFalse(
            PreceptorAssignment.objects.filter(
                session__date__range=(date(2020, 9, 1), date(2021, 1, 1))
            ).exists()
        )
        self.assertFalse(
            LateDocAssignment.objects.filter(
                date__range=(date(2020, 9, 1), date(2021, 1, 1))
            ).exists()
        )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2026, 8, 18),
    )
    @patch("schedules.services.work_remaining.organization_today", return_value=date(2026, 8, 18))
    @patch(
        "schedules.services.synthetic_scenario.organization_today", return_value=date(2026, 8, 18)
    )
    @tag("synthetic-scenario-integration")
    def test_representative_multi_year_scenario_generates_history_and_future_work(
        self, _scenario_today, _work_remaining_today, _operational_today
    ):
        today = date(2026, 8, 18)
        self.scenario.generate_source_schedule_history(self.baseline, today=today)
        self.scenario.generate_precepting_history(self.baseline, today=today)

        self.assertTrue(PreceptorAssignment.objects.filter(session__date__lte=today).exists())
        self.assertTrue(PreceptorAssignmentChange.objects.exists())
        self.assertTrue(LateDocAssignmentChange.objects.exists())
        self.assertTrue(global_work_remaining().items)
