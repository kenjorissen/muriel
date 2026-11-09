"""Retained month provenance at real scheduling write boundaries."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from schedules.models import (
    AcademicEventDefinition,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    LateDocAssignment,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    SchedulePeriod,
    Vacation,
)
from schedules.services._mutation import capture_calendar_range, scheduling_operation
from schedules.services.academic_events import (
    correct_academic_event_occurrence,
    create_academic_event_occurrence,
)
from schedules.services.community import (
    create_community_preceptor_rule,
    remove_community_preceptor_rule,
)
from schedules.services.finding_acknowledgements import (
    acknowledge_finding,
    remove_finding_acknowledgement,
)
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence
from schedules.services.late_docs import assign_late_doc, remove_late_doc_assignment
from schedules.services.month_edits import MonthEditRecorder, month_calendar_content
from schedules.services.physicians import (
    correct_faculty_availability_template,
    correct_physician_identity,
    link_physician_user,
    unlink_physician_user,
)
from schedules.services.preceptors import (
    assign_preceptor,
    initialize_preceptor_periods,
    initialize_preceptor_session_range,
    operational_draft_findings_for_session,
    remove_preceptor_assignment,
    set_preceptor_assignment_lock,
)
from schedules.services.vacation_calendar_import import (
    CalendarData,
    ImportPlan,
    PlannedVacation,
    apply_vacation_calendar_import,
)
from schedules.services.vacations import correct_vacation, create_vacation, remove_vacation


class MonthEditTests(TestCase):
    month = date(2026, 9, 1)
    day = date(2026, 9, 30)
    first = datetime(2026, 9, 1, 18, tzinfo=UTC)
    second = datetime(2026, 9, 2, 18, tzinfo=UTC)

    @classmethod
    def setUpTestData(cls):
        cls.scheduler = get_user_model().objects.create_user(username="month-editor")
        cls.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        cls.physicians = []
        for name in ("Scheduled Faculty", "Replacement Faculty"):
            physician = Physician.objects.create(display_name=name)
            EffectiveRole.objects.create(
                physician=physician, role="faculty", effective_start=date(2020, 1, 1)
            )
            FacultyAvailabilityTemplate.objects.create(
                physician=physician,
                slots={
                    slot: {"base_state": "open"}
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                },
            )
            cls.physicians.append(physician)

    def setUp(self):
        self.clock = patch(
            "schedules.services.operational_attention.timezone.now", return_value=self.first
        )
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def session(self, day=None):
        day = day or self.day
        period, _ = SchedulePeriod.objects.get_or_create(month=day.replace(day=1))
        return PreceptorSession.objects.get_or_create(period=period, date=day, period_of_day="AM")[
            0
        ]

    def retained_assignment(self, day=None, physician=None):
        return PreceptorAssignment.objects.create(
            session=self.session(day),
            physician=physician or self.physicians[0],
            responsibility="primary",
            primary_position=1,
        )

    def assign(self, session=None, physician=None):
        return assign_preceptor(
            session=session or self.session(),
            physician=physician or self.physicians[0],
            responsibility="primary",
            primary_position=1,
            scheduler=self.scheduler,
        )

    def edited(self, month=None):
        return (
            SchedulePeriod.objects.filter(month=month or self.month)
            .values_list("last_edited_at", flat=True)
            .first()
        )

    def vacation(self, physician=None, day=None):
        return create_vacation(
            physician=physician or self.physicians[0],
            start_date=day or self.day,
            end_date=day or self.day,
            scheduler=self.scheduler,
        )

    def test_initialization_view_pdf_and_clock_do_not_invent_an_edit(self):
        initialize_preceptor_periods(self.month)
        self.client.force_login(self.scheduler)
        for now in (self.first, self.second):
            self.now.return_value = now
            for name in ("monthly-workspace", "preceptor-list", "precepting-calendar-pdf"):
                self.assertEqual(
                    self.client.get(reverse(name), {"month": "2026-09"}).status_code, 200
                )
            self.assertIsNone(self.edited())
            self.assertFalse(SchedulePeriod.objects.filter(last_edited_at__isnull=False).exists())

    def test_projection_and_nested_initialization_do_not_edit_empty_edge_months(self):
        december = date(2026, 12, 1)
        january = date(2027, 1, 1)
        before = {month: month_calendar_content(month) for month in (december, january)}
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())

        with scheduling_operation():
            capture_calendar_range(date(2026, 12, 31), date(2027, 1, 1))
            initialize_preceptor_session_range(date(2026, 12, 31), date(2027, 1, 1))
        self.assertFalse(SchedulePeriod.objects.filter(last_edited_at__isnull=False).exists())
        for month in (december, january):
            self.assertEqual(month_calendar_content(month), before[month])

        with scheduling_operation():
            capture_calendar_range(december, january)
            initialize_preceptor_periods(december, january)
        for month in (december, january):
            self.assertEqual(month_calendar_content(month), before[month])
        self.assertFalse(SchedulePeriod.objects.filter(last_edited_at__isnull=False).exists())

    def test_first_write_noop_lock_and_last_removal_retain_real_edit_times(self):
        assignment = self.assign()
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        self.assign()
        self.assertEqual(self.edited(), self.first)
        set_preceptor_assignment_lock(
            assignment=assignment, is_locked=True, scheduler=self.scheduler
        )
        self.assertEqual(self.edited(), self.second)
        self.now.return_value += timedelta(hours=1)
        remove_preceptor_assignment(assignment=assignment, scheduler=self.scheduler)
        retained = self.now.return_value
        self.assertEqual(self.edited(), retained)
        self.now.return_value += timedelta(days=50)
        initialize_preceptor_periods(self.month)
        self.assertEqual(self.edited(), retained)

    def test_nested_intermediate_changes_and_rollback_are_not_edits(self):
        assignment = self.retained_assignment()
        with scheduling_operation():
            self.assign(physician=self.physicians[1])
            self.assign(physician=self.physicians[0])
        self.assertIsNone(self.edited())
        with self.assertRaisesMessage(ValueError, "abort"):
            with transaction.atomic():
                set_preceptor_assignment_lock(
                    assignment=assignment, is_locked=True, scheduler=self.scheduler
                )
                raise ValueError("abort")
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_locked)
        self.assertIsNone(self.edited())

    def test_distinct_reverse_edits_each_count(self):
        self.retained_assignment()
        self.assign(physician=self.physicians[1])
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        self.assign(physician=self.physicians[0])
        self.assertEqual(self.edited(), self.second)

    def test_vacation_findings_count_both_directions_but_candidate_and_note_changes_do_not(self):
        self.retained_assignment()
        self.vacation(physician=self.physicians[1])
        self.assertIsNone(self.edited())
        vacation = self.vacation()
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        correct_vacation(
            vacation=vacation,
            start_date=self.day,
            end_date=self.day,
            period_of_day="whole_day",
            note="Source history only",
            scheduler=self.scheduler,
        )
        self.assertEqual(self.edited(), self.first)
        remove_vacation(vacation=vacation, scheduler=self.scheduler)
        self.assertEqual(self.edited(), self.second)

    def test_same_marker_changed_event_detail_counts_and_reaches_editor(self):
        assignment = self.retained_assignment()
        definition = AcademicEventDefinition.objects.create(
            base_name="Teaching", start_academic_year=2026
        )
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=self.day,
            end_date=self.day,
            participants=(self.physicians[0],),
            suffix="First group",
            scheduler=self.scheduler,
        )
        before = operational_draft_findings_for_session(assignment.session).findings
        self.now.return_value = self.second
        correct_academic_event_occurrence(
            event=event,
            start_date=self.day,
            end_date=self.day,
            participants=(self.physicians[0],),
            suffix="Second group",
            scheduler=self.scheduler,
        )
        after = operational_draft_findings_for_session(assignment.session).findings
        self.assertEqual(
            [(f.code, f.severity) for f in before], [(f.code, f.severity) for f in after]
        )
        self.assertEqual(self.edited(), self.second)
        self.client.force_login(self.scheduler)
        response = self.client.get(
            reverse("session-assignment-edit", args=(assignment.session_id, "primary-1"))
        )
        entry = next(
            e["finding"]
            for e in response.context["finding_entries"]
            if e["code"] == "not_available_assignment"
        )
        self.assertTrue(any("Second group" in source.label for source in entry.source_links))

    def test_import_replacing_only_source_identity_is_one_noop_operation(self):
        self.retained_assignment()
        vacation = Vacation.objects.create(
            physician=self.physicians[0], start_date=self.day, end_date=self.day
        )
        plan = ImportPlan(
            data=CalendarData("replacement.xlsx", self.day, self.day, (), (), ()),
            matched_physicians=(self.physicians[0],),
            create=(PlannedVacation(self.physicians[0], self.day, self.day),),
            reuse=(),
            remove=(vacation,),
            blockers=(),
            warnings=(),
        )
        apply_vacation_calendar_import(plan, scheduler=self.scheduler)
        self.assertFalse(Vacation.objects.filter(pk=vacation.pk).exists())
        self.assertIsNone(self.edited())

    def test_cp_expectation_counts_unless_vacation_masks_it(self):
        self.retained_assignment()
        cp = Physician.objects.create(display_name="Community physician")
        EffectiveRole.objects.create(
            physician=cp, role="community_preceptor", effective_start=date(2020, 1, 1)
        )
        vacation = self.vacation(physician=cp)
        rule = create_community_preceptor_rule(
            physician=cp,
            weekday=2,
            ordinals=[5],
            periods=["AM"],
            effective_start=self.day,
            effective_end=self.day,
            scheduler=self.scheduler,
        )
        self.assertIsNone(self.edited())
        remove_vacation(vacation=vacation, scheduler=self.scheduler)
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        remove_community_preceptor_rule(rule=rule, scheduler=self.scheduler)
        self.assertEqual(self.edited(), self.second)

    def test_spillover_is_excluded_and_direct_moves_capture_both_months(self):
        september = self.retained_assignment()
        self.retained_assignment(date(2026, 10, 1))
        self.vacation()
        self.assertEqual(self.edited(), self.first)
        self.assertIsNone(self.edited(date(2026, 10, 1)))
        self.now.return_value = self.second
        with scheduling_operation():
            remove_preceptor_assignment(assignment=september, scheduler=self.scheduler)
            self.assign(self.session(date(2026, 11, 2)))
        self.assertEqual(self.edited(), self.second)
        self.assertEqual(self.edited(date(2026, 11, 1)), self.second)
        self.assertIsNone(self.edited(date(2026, 10, 1)))

    def test_propagation_stops_at_first_assignment_free_month_and_counts_late_doc(self):
        from schedules.models import LateDocAssignment

        self.retained_assignment()
        LateDocAssignment.objects.create(date=date(2026, 10, 5), physician=self.physicians[0])
        initialize_preceptor_periods(date(2026, 11, 1))
        examined = []
        has_assignments = MonthEditRecorder._has_assignments

        def check_month(month):
            examined.append(month)
            return has_assignments(month)

        with patch.object(MonthEditRecorder, "_has_assignments", side_effect=check_month):
            correct_physician_identity(
                physician=self.physicians[0],
                display_name="Corrected Faculty",
                scheduler=self.scheduler,
            )
        self.assertEqual(examined, [self.month, date(2026, 10, 1), date(2026, 11, 1)])
        self.assertEqual(self.edited(), self.first)
        self.assertEqual(self.edited(date(2026, 10, 1)), self.first)
        self.assertIsNone(self.edited(date(2026, 11, 1)))

    def change_template(self):
        template = self.physicians[0].faculty_availability_templates.get()
        slots = {slot: "off" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        correct_faculty_availability_template(
            template=template,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )

    def test_identity_correction_records_each_retained_assignment_month_without_current_assignments(
        self,
    ):
        precepting_month = date(2026, 7, 1)
        late_doc_month = date(2026, 8, 1)
        unrelated_month = date(2026, 6, 1)
        self.retained_assignment(date(2026, 7, 2))
        LateDocAssignment.objects.create(date=date(2026, 8, 3), physician=self.physicians[0])
        self.retained_assignment(date(2026, 6, 2), physician=self.physicians[1])
        initialize_preceptor_periods(self.month)

        correct_physician_identity(
            physician=self.physicians[0],
            display_name="Corrected historical name",
            scheduler=self.scheduler,
        )

        for month in (precepting_month, late_doc_month):
            with self.subTest(month=month):
                self.assertEqual(self.edited(month), self.first)
        self.assertIsNone(self.edited(unrelated_month))
        self.assertIsNone(self.edited())

    def test_historical_identity_noop_and_account_changes_preserve_month_edit_times(self):
        precepting_month = date(2026, 7, 1)
        late_doc_month = date(2026, 8, 1)
        self.retained_assignment(date(2026, 7, 2))
        LateDocAssignment.objects.create(date=date(2026, 8, 3), physician=self.physicians[0])
        for month in (precepting_month, late_doc_month):
            SchedulePeriod.objects.update_or_create(
                month=month, defaults={"last_edited_at": self.first}
            )
        self.now.return_value = self.second

        correct_physician_identity(
            physician=self.physicians[0],
            display_name=self.physicians[0].display_name,
            reason="History note only",
            scheduler=self.scheduler,
        )
        correct_physician_identity(
            physician=self.physicians[1],
            display_name="Unscheduled corrected name",
            scheduler=self.scheduler,
        )
        user = get_user_model().objects.create_user(
            username="historical-physician", email="historical-physician@example.com"
        )
        link_physician_user(physician=self.physicians[0], user=user, scheduler=self.scheduler)
        unlink_physician_user(physician=self.physicians[0], scheduler=self.scheduler)

        for month in (precepting_month, late_doc_month):
            with self.subTest(month=month):
                self.assertEqual(self.edited(month), self.first)
        self.assertIsNone(self.edited())

    def test_today_only_edit_is_retained_after_aging_and_unrelated_save(self):
        self.retained_assignment(date(2026, 9, 1))
        self.change_template()
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        self.client.force_login(self.scheduler)
        self.client.get(reverse("monthly-workspace"), {"month": "2026-09"})
        self.vacation(physician=self.physicians[1])
        self.assertEqual(self.edited(), self.first)

    def test_equivalent_template_edit_first_made_tomorrow_is_suppressed(self):
        self.retained_assignment(date(2026, 9, 1))
        self.now.return_value = self.second
        self.change_template()
        self.assertIsNone(self.edited())

    def test_historical_assignment_and_dated_closure_corrections_count(self):
        old_month = date(2026, 7, 1)
        self.assign(self.session(date(2026, 7, 2)))
        self.assertEqual(self.edited(old_month), self.first)
        occurrence = holiday_occurrences_for(2026).get(holiday="independence_day")
        self.now.return_value = self.second
        schedule_holiday_occurrence(
            occurrence=occurrence, observed_date=date(2026, 7, 3), scheduler=self.scheduler
        )
        self.assertEqual(self.edited(old_month), self.second)

    def test_late_doc_first_and_last_write_outside_propagation_bound(self):
        monday = date(2026, 11, 2)
        assignment = assign_late_doc(
            on_date=monday, physician=self.physicians[0], scheduler=self.scheduler
        )
        self.assertEqual(self.edited(monday.replace(day=1)), self.first)
        self.now.return_value = self.second
        remove_late_doc_assignment(assignment=assignment, scheduler=self.scheduler)
        self.assertEqual(self.edited(monday.replace(day=1)), self.second)

    def test_acknowledgement_changes_effective_calendar_status(self):
        assignment = self.retained_assignment()
        self.vacation()
        finding = next(
            f
            for f in operational_draft_findings_for_session(assignment.session).findings
            if f.code == "not_available_assignment"
        )
        self.now.return_value = self.second
        acknowledgement = acknowledge_finding(
            finding=finding, scheduler=self.scheduler, reason="Accepted exception"
        )
        self.assertEqual(self.edited(), self.second)
        self.now.return_value += timedelta(hours=1)
        remove_finding_acknowledgement(
            finding=finding, acknowledgement_id=acknowledgement.pk, scheduler=self.scheduler
        )
        self.assertEqual(self.edited(), self.now.return_value)

    def test_operation_keeps_one_effective_date_across_local_midnight(self):
        self.retained_assignment(date(2026, 9, 1))
        self.now.return_value = datetime(2026, 9, 2, 4, 59, tzinfo=UTC)
        with scheduling_operation():
            self.now.return_value = datetime(2026, 9, 2, 5, 1, tzinfo=UTC)
            self.change_template()
        self.assertEqual(self.edited(), datetime(2026, 9, 2, 4, 59, tzinfo=UTC))

    def test_dated_source_move_compares_old_and_new_ownership(self):
        self.retained_assignment()
        october = date(2026, 10, 1)
        self.retained_assignment(october)
        vacation = self.vacation()
        self.now.return_value = self.second
        correct_vacation(
            vacation=vacation,
            start_date=october,
            end_date=october,
            period_of_day="whole_day",
            scheduler=self.scheduler,
        )
        self.assertEqual(self.edited(), self.second)
        self.assertEqual(self.edited(october), self.second)

    def test_week_finding_affects_each_month_with_its_own_operational_weekdays(self):
        from schedules.services.rounding import record_normal_rounder

        monday = date(2026, 8, 31)
        self.now.return_value = datetime(2026, 8, 31, 18, tzinfo=UTC)
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())
        record_normal_rounder(week=monday, physician=self.physicians[0], scheduler=self.scheduler)
        self.assertEqual(self.edited(date(2026, 8, 1)), self.now.return_value)
        self.assertEqual(self.edited(), self.now.return_value)
        self.assertFalse(PreceptorSession.objects.exists())
        self.assertFalse(PreceptorAssignment.objects.exists())

        self.now.return_value = self.second
        initialize_preceptor_periods(self.month)
        self.assertEqual(self.edited(date(2026, 8, 1)), datetime(2026, 8, 31, 18, tzinfo=UTC))
        self.assertEqual(self.edited(), datetime(2026, 8, 31, 18, tzinfo=UTC))

    def test_interview_assignment_composes_with_precepting_without_tracking_its_note(self):
        from schedules.services.interviews import (
            assign_interview_shift,
            change_interview_shift_note,
            create_interview_shift,
            unassign_interview_shift,
        )

        self.retained_assignment()
        shift = create_interview_shift(academic_year=2026, date=self.day, scheduler=self.scheduler)
        self.assertIsNone(self.edited())
        assign_interview_shift(
            shift=shift, physician=self.physicians[0], scheduler=self.scheduler, override=True
        )
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        change_interview_shift_note(
            shift=shift, note="Private scheduling note", scheduler=self.scheduler
        )
        self.assertEqual(self.edited(), self.first)
        unassign_interview_shift(shift=shift, scheduler=self.scheduler)
        self.assertEqual(self.edited(), self.second)

    def test_cp_expectation_change_is_masked_by_an_existing_manual_float_assignment(self):
        from schedules.services.availability_categories import ensure_system_schedule_options

        admin = ensure_system_schedule_options()
        cp = Physician.objects.create(display_name="Manually assigned Float")
        for role in ("community_preceptor", "float_preceptor"):
            EffectiveRole.objects.create(physician=cp, role=role, effective_start=date(2020, 1, 1))
        slots = {
            slot: {"base_state": "off"} for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        slots["wednesday_am"] = {"category_id": admin.pk}
        FacultyAvailabilityTemplate.objects.create(physician=cp, slots=slots)
        self.assign(physician=cp)
        self.assertEqual(self.edited(), self.first)
        self.now.return_value = self.second
        create_community_preceptor_rule(
            physician=cp,
            weekday=2,
            ordinals=[5],
            periods=["AM"],
            effective_start=self.day,
            effective_end=self.day,
            scheduler=self.scheduler,
        )
        self.assertEqual(self.edited(), self.first)

    def test_intermediate_assignment_change_does_not_retire_a_restored_exception(self):
        assignment = self.retained_assignment()
        self.vacation()
        finding = next(
            f
            for f in operational_draft_findings_for_session(assignment.session).findings
            if f.code == "not_available_assignment"
        )
        acknowledgement = acknowledge_finding(
            finding=finding, scheduler=self.scheduler, reason="Keep this exception"
        )
        self.now.return_value = self.second
        with scheduling_operation():
            self.assign(physician=self.physicians[1])
            self.assign(physician=self.physicians[0])
        acknowledgement.refresh_from_db()
        self.assertIsNone(acknowledgement.no_longer_applies_at)
        self.assertEqual(self.edited(), self.first)

    def test_finding_count_changes_even_when_affected_physicians_share_a_name(self):
        self.retained_assignment()
        for recorded_at in (self.first, self.second):
            self.now.return_value = recorded_at
            cp = Physician.objects.create(display_name="Same display name")
            EffectiveRole.objects.create(
                physician=cp, role="community_preceptor", effective_start=date(2020, 1, 1)
            )
            create_community_preceptor_rule(
                physician=cp,
                weekday=2,
                ordinals=[5],
                periods=["AM"],
                effective_start=self.day,
                effective_end=self.day,
                scheduler=self.scheduler,
            )
            self.assertEqual(self.edited(), recorded_at)
