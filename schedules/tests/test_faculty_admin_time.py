from datetime import date
from decimal import Decimal

from django.test import TestCase

from schedules.academic_years import academic_year_for_date
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    CustomAvailabilityCategory,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    HolidayOccurrence,
    InterviewShift,
    MonthlyPhysicianAvailabilityRule,
    NormalRounder,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    SchedulePeriod,
    Vacation,
)
from schedules.services.faculty_admin_time import monthly_faculty_admin_time_report


class MonthlyFacultyAdminTimeReportTests(TestCase):
    month = date(2021, 2, 1)

    def faculty(self, name, *, effective_start=date(2020, 1, 1), effective_end=None, slots=None):
        physician = Physician.objects.create(display_name=name)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=effective_start,
            effective_end=effective_end,
        )
        if slots is not None:
            FacultyAvailabilityTemplate.objects.create(
                physician=physician,
                effective_start=effective_start,
                effective_end=effective_end,
                slots=self.slots(slots),
            )
        return physician

    def slots(self, values):
        slots = {
            slot: FacultyAvailabilityTemplate.base_selection("off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        slots.update(
            {
                slot: value
                if isinstance(value, dict)
                else FacultyAvailabilityTemplate.base_selection(value)
                for slot, value in values.items()
            }
        )
        return slots

    def report(self, coverage="history-and-scheduled", as_of_date=date(2021, 2, 15)):
        return monthly_faculty_admin_time_report(self.month, coverage, as_of_date)

    def assignment(self, physician, on_date, period_of_day):
        period = SchedulePeriod.objects.get_or_create(month=self.month)[0]
        session = PreceptorSession.objects.create(
            period=period, date=on_date, period_of_day=period_of_day
        )
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

    def event(
        self,
        title,
        on_date,
        *,
        participants=(),
        all_available_faculty=False,
        precepting_impact=AcademicEvent.PreceptingImpact.HARD,
    ):
        definition = AcademicEventDefinition.objects.create(
            base_name=title, start_academic_year=academic_year_for_date(on_date)
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=academic_year_for_date(on_date),
            base_name=title,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=on_date,
            end_date=on_date,
            period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
            precepting_impact=precepting_impact,
            all_available_faculty=all_available_faculty,
        )
        event.participants.set(participants)
        return event

    def test_explicit_option_ledger_uses_total_worked_and_admin_precepting_consumption(self):
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        clinic = CustomAvailabilityCategory.objects.create(
            name="Clinic teaching", impact="available"
        )
        research = CustomAvailabilityCategory.objects.create(
            name="Research", impact="not_available"
        )
        physician = self.faculty(
            "Ledger",
            slots={
                "monday_am": "open",
                "monday_pm": FacultyAvailabilityTemplate.category_selection(admin),
                "tuesday_am": "patient_care",
                "tuesday_pm": FacultyAvailabilityTemplate.category_selection(clinic),
                "wednesday_am": FacultyAvailabilityTemplate.category_selection(research),
            },
        )
        self.assignment(physician, date(2021, 2, 1), "AM")
        self.assignment(physician, date(2021, 2, 1), "PM")
        self.assignment(physician, date(2021, 2, 2), "AM")

        report = self.report()
        row = report.rows[0]

        option_labels = [(option.key, option.label) for option in report.options]
        self.assertEqual(
            option_labels[:5],
            [
                ("base:open", "Open"),
                (f"category:{admin.pk}", "Admin"),
                ("base:backup_only", "Backup only"),
                ("base:patient_care", "Patient care"),
                ("base:off", "Off"),
            ],
        )
        self.assertIn((f"category:{clinic.pk}", "Clinic teaching"), option_labels)
        self.assertIn((f"category:{research.pk}", "Research"), option_labels)
        self.assertEqual(row.hours_for("base:open"), Decimal("16"))
        self.assertEqual(row.hours_for(f"category:{admin.pk}"), Decimal("16"))
        self.assertEqual(row.hours_for("base:patient_care"), Decimal("16"))
        self.assertEqual(row.hours_for(f"category:{clinic.pk}"), Decimal("16"))
        self.assertEqual(row.hours_for(f"category:{research.pk}"), Decimal("16"))
        self.assertEqual(row.not_available_hours, Decimal("80"))
        self.assertEqual(row.total_worked_hours, Decimal("80"))
        self.assertEqual(row.potential_admin_hours, Decimal("32"))
        self.assertEqual(row.precepting_from_admin_hours, Decimal("8"))
        self.assertEqual(row.computed_admin_hours, Decimal("24"))
        self.assertEqual(row.acgme_ten_percent, Decimal("8.00"))
        self.assertEqual(row.acgme_goal, Decimal("8.00"))
        self.assertEqual(row.umn_goal, Decimal("16.00"))
        self.assertTrue(row.acgme_met)
        self.assertTrue(row.umn_met)

    def test_monthly_rule_precedence_template_transition_and_annual_window_are_resolved(self):
        physician = self.faculty("Transition")
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            effective_start=date(2020, 1, 1),
            effective_end=date(2021, 2, 14),
            slots=self.slots({"monday_am": "patient_care"}),
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            effective_start=date(2021, 2, 15),
            slots=self.slots({"monday_pm": "patient_care"}),
        )
        MonthlyPhysicianAvailabilityRule.objects.create(
            physician=physician,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            annual_start_month=2,
            annual_end_month=2,
        )

        row = next(row for row in self.report().rows if row.physician == physician)

        self.assertEqual(row.hours_for("base:open"), Decimal("4"))
        self.assertEqual(row.patient_care_hours, Decimal("12"))
        self.assertEqual(row.total_worked_hours, Decimal("16"))

    def test_inactive_or_renamed_custom_option_keeps_its_selected_identity_and_name(self):
        category = CustomAvailabilityCategory.objects.create(
            name="Original label", impact="available"
        )
        self.faculty(
            "Retained custom option",
            slots={"monday_am": FacultyAvailabilityTemplate.category_selection(category)},
        )
        CustomAvailabilityCategory.objects.filter(pk=category.pk).update(
            name="Renamed later", is_active=False
        )

        report = self.report()
        row = report.rows[0]

        self.assertIn(
            (f"category:{category.pk}", "Original label"),
            [(option.key, option.label) for option in report.options],
        )
        self.assertEqual(row.hours_for(f"category:{category.pk}"), Decimal("16"))

    def test_vacation_events_holiday_and_overlaps_exclude_one_period_each(self):
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        physician = self.faculty(
            "Excluded",
            slots={
                "monday_am": "open",
                "monday_pm": FacultyAvailabilityTemplate.category_selection(admin),
            },
        )
        Vacation.objects.create(
            physician=physician,
            start_date=date(2021, 2, 1),
            end_date=date(2021, 2, 1),
            period_of_day=Vacation.PeriodOfDay.AM,
        )
        self.event("Faculty retreat", date(2021, 2, 1), participants=[physician])
        self.event("All Faculty retreat", date(2021, 2, 8), all_available_faculty=True)
        HolidayOccurrence.objects.create(
            holiday="new_years_day",
            calendar_year=2021,
            name="Moved closure",
            nominal_date=date(2021, 1, 1),
            observed_date=date(2021, 2, 1),
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.SCHEDULED,
        )

        row = self.report().rows[0]

        self.assertEqual(len(row.exclusions), 4)
        self.assertEqual(row.excluded_hours, Decimal("16"))
        first_am = next(
            item
            for item in row.exclusions
            if item.on_date == date(2021, 2, 1) and item.period_of_day == "AM"
        )
        self.assertEqual(first_am.option_key, "base:open")
        self.assertIn("Holiday closure: Moved closure", first_am.exclusions)
        self.assertIn(f"Vacation {physician.vacations.get().pk}", first_am.exclusions)
        self.assertIn("Academic Event: Faculty retreat", first_am.exclusions)
        all_faculty_retreat = next(
            item
            for item in row.exclusions
            if item.on_date == date(2021, 2, 8) and item.period_of_day == "AM"
        )
        self.assertIn("Academic Event: All Faculty retreat", all_faculty_retreat.exclusions)
        self.assertEqual(row.total_worked_hours, Decimal("16"))
        self.assertEqual(row.potential_admin_hours, Decimal("16"))

    def test_shared_blockers_include_rounding_and_interviews_but_not_soft_events(self):
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        custom = CustomAvailabilityCategory.objects.create(
            name="Independent study", impact="not_available"
        )
        physician = self.faculty(
            "Shared blockers",
            slots={
                "monday_am": "open",
                "monday_pm": FacultyAvailabilityTemplate.category_selection(admin),
                "tuesday_am": FacultyAvailabilityTemplate.category_selection(custom),
                "tuesday_pm": "open",
            },
        )
        NormalRounder.objects.create(week=date(2021, 2, 1), physician=physician)
        InterviewShift.objects.create(date=date(2021, 2, 2), physician=physician)
        self.event(
            "Advisory discussion",
            date(2021, 2, 2),
            participants=[physician],
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
        )
        self.assignment(physician, date(2021, 2, 8), "PM")

        row = next(row for row in self.report().rows if row.physician == physician)

        self.assertEqual(row.hours_for("base:open"), Decimal("24"))
        self.assertEqual(row.hours_for(f"category:{admin.pk}"), Decimal("12"))
        self.assertEqual(row.hours_for(f"category:{custom.pk}"), Decimal("12"))
        self.assertEqual(row.total_worked_hours, Decimal("48"))
        self.assertEqual(row.potential_admin_hours, Decimal("36"))
        self.assertEqual(row.precepting_from_admin_hours, Decimal("4"))
        self.assertEqual(row.computed_admin_hours, Decimal("32"))
        self.assertEqual(row.excluded_hours, Decimal("16"))
        exclusions_by_slot = {(item.on_date, item.period_of_day): item for item in row.exclusions}
        expected_rounder_slots = {
            (date(2021, 2, 1), "AM"): "base:open",
            (date(2021, 2, 1), "PM"): f"category:{admin.pk}",
            (date(2021, 2, 2), "AM"): f"category:{custom.pk}",
            (date(2021, 2, 2), "PM"): "base:open",
        }
        self.assertEqual(set(exclusions_by_slot), set(expected_rounder_slots))
        for slot, option_key in expected_rounder_slots.items():
            with self.subTest(slot=slot):
                exclusion = exclusions_by_slot[slot]
                self.assertEqual(exclusion.option_key, option_key)
                self.assertIn("Normal rounder week 2021-02-01", exclusion.exclusions)
                self.assertNotIn("Academic Event: Advisory discussion", exclusion.exclusions)

        interview_period = exclusions_by_slot[(date(2021, 2, 2), "AM")]
        self.assertIn("Interview: 2021-02-02", interview_period.exclusions)
        for slot, exclusion in exclusions_by_slot.items():
            if slot != (date(2021, 2, 2), "AM"):
                with self.subTest(slot=slot, exclusion="interview"):
                    self.assertNotIn("Interview: 2021-02-02", exclusion.exclusions)

    def test_role_and_coverage_boundaries_limit_ledger_periods(self):
        former = self.faculty(
            "Former", effective_end=date(2021, 2, 12), slots={"monday_am": "patient_care"}
        )
        future = self.faculty(
            "Future", effective_start=date(2021, 2, 15), slots={"monday_am": "patient_care"}
        )

        history = self.report("history")
        scheduled = self.report("scheduled")

        self.assertEqual([row.physician for row in history.rows], [former])
        self.assertEqual(history.rows[0].patient_care_hours, Decimal("8"))
        self.assertEqual([row.physician for row in scheduled.rows], [future])
        self.assertEqual(scheduled.rows[0].patient_care_hours, Decimal("8"))
