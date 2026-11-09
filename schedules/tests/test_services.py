from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulePeriod,
    Vacation,
    VacationChange,
)
from schedules.services.academic_events import (
    academic_event_covers,
    academic_event_sources_for,
    academic_events_covering,
    academic_events_for_physician,
    academic_events_overlapping,
    correct_academic_event_occurrence,
    create_academic_event_occurrence,
    remove_academic_event_occurrence,
)
from schedules.services.availability import (
    effective_preceptor_availability_for_physicians,
    effective_preceptor_availability_on,
)
from schedules.services.availability_categories import (
    create_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.community import (
    community_preceptor_availability_on,
    community_preceptor_month_projection,
    community_preceptor_occurrences,
    community_preceptor_rules_on,
    correct_community_preceptor_rule,
    create_community_preceptor_rule,
    remove_community_preceptor_rule,
)
from schedules.services.physicians import (
    add_role,
    correct_faculty_availability_template,
    correct_physician_identity,
    correct_role,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
    end_role,
    physicians_status_on,
)
from schedules.services.preceptors import (
    assign_preceptor,
    open_monthly_draft,
    remove_preceptor_assignment,
    set_preceptor_assignment_lock,
)
from schedules.services.rounding import (
    casual_rounder_rules_on,
    correct_casual_rounder_rule,
    create_casual_rounder_rule,
    missing_normal_rounder_weeks,
    normal_rounder_covers_week,
    normal_rounder_for_date,
    normal_rounder_status_on,
    record_normal_rounder,
    remove_casual_rounder_rule,
    remove_normal_rounder,
    rounding_effect_on,
    rounding_month_projection,
)
from schedules.services.vacations import (
    correct_vacation,
    create_vacation,
    remove_vacation,
    vacation_covers,
    vacations_overlapping,
)


class VacationServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = create_physician(
            display_name="No role required", scheduler=self.scheduler, reason="Initial entry"
        )

    def create_vacation(self, **overrides):
        values = {
            "physician": self.physician,
            "start_date": date(2025, 2, 3),
            "end_date": date(2025, 2, 5),
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_vacation(**values)

    def test_whole_day_default_covers_both_periods_across_inclusive_range(self):
        vacation = self.create_vacation()

        self.assertEqual(vacation.period_of_day, Vacation.PeriodOfDay.WHOLE_DAY)
        self.assertTrue(vacation_covers(vacation, date(2025, 2, 3), "AM"))
        self.assertTrue(vacation_covers(vacation, date(2025, 2, 5), "PM"))
        self.assertFalse(vacation_covers(vacation, date(2025, 2, 2), "AM"))
        self.assertEqual(
            list(vacations_overlapping(date(2025, 2, 5), date(2025, 2, 5))), [vacation]
        )

    def test_create_records_trimmed_or_blank_current_notes(self):
        noted = self.create_vacation(note="  Recorded absence  ")
        blank = self.create_vacation(start_date=date(2025, 2, 6), end_date=date(2025, 2, 6))

        noted_change, blank_change = VacationChange.objects.order_by("pk")
        self.assertEqual(noted.note, "Recorded absence")
        self.assertEqual(noted_change.previous_note, None)
        self.assertEqual(noted_change.recorded_note, "Recorded absence")
        self.assertEqual(blank.note, "")
        self.assertEqual(blank_change.previous_note, None)
        self.assertEqual(blank_change.recorded_note, "")

    def test_am_pm_precision_and_overlap_queries_are_role_independent(self):
        am_only = self.create_vacation(
            start_date=date(2025, 2, 10),
            end_date=date(2025, 2, 10),
            period_of_day=Vacation.PeriodOfDay.AM,
        )
        pm_only = self.create_vacation(
            start_date=date(2025, 2, 12),
            end_date=date(2025, 2, 14),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Afternoon absence",
        )

        self.assertTrue(am_only.covers(date(2025, 2, 10), "AM"))
        self.assertFalse(am_only.covers(date(2025, 2, 10), "PM"))
        self.assertTrue(pm_only.covers(date(2025, 2, 14), "PM"))
        self.assertFalse(pm_only.covers(date(2025, 2, 14), "AM"))
        self.assertEqual(
            list(vacations_overlapping(date(2025, 2, 11), date(2025, 2, 12))), [pm_only]
        )

        role = add_role(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Later roster entry",
        )
        correct_role(
            effective_role=role,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
            reason="Corrected roster",
        )
        self.assertEqual(Vacation.objects.get(pk=am_only.pk).physician, self.physician)
        self.assertEqual(Vacation.objects.get(pk=pm_only.pk).period_of_day, Vacation.PeriodOfDay.PM)

    def test_invalid_ranges_are_atomic_and_unchanged_create_is_idempotent(self):
        vacation = self.create_vacation()
        history_count = VacationChange.objects.count()

        self.assertEqual(self.create_vacation(note="Repeated submission").pk, vacation.pk)
        with self.assertRaises(ValidationError):
            self.create_vacation(start_date=date(2025, 2, 6), end_date=date(2025, 2, 5))
        with self.assertRaises(ValidationError):
            vacations_overlapping(date(2025, 2, 6), date(2025, 2, 5))

        self.assertEqual(Vacation.objects.count(), 1)
        self.assertEqual(VacationChange.objects.count(), history_count)

    def test_note_only_edits_and_clearing_create_history_but_a_true_noop_does_not(self):
        vacation = self.create_vacation(note="Initial note")
        correct_vacation(
            vacation=vacation,
            start_date=vacation.start_date,
            end_date=vacation.end_date,
            period_of_day=vacation.period_of_day,
            note="  Updated note  ",
            scheduler=self.scheduler,
        )
        correct_vacation(
            vacation=vacation,
            start_date=vacation.start_date,
            end_date=vacation.end_date,
            period_of_day=vacation.period_of_day,
            note="",
            scheduler=self.scheduler,
        )
        history_count = VacationChange.objects.count()
        correct_vacation(
            vacation=vacation,
            start_date=vacation.start_date,
            end_date=vacation.end_date,
            period_of_day=vacation.period_of_day,
            note="   ",
            scheduler=self.scheduler,
        )

        vacation.refresh_from_db()
        self.assertEqual(vacation.note, "")
        self.assertEqual(VacationChange.objects.count(), history_count)
        self.assertEqual(
            list(
                VacationChange.objects.order_by("pk").values_list("previous_note", "recorded_note")
            ),
            [(None, "Initial note"), ("Initial note", "Updated note"), ("Updated note", "")],
        )

    def test_past_correction_removal_and_idempotency_retain_note_snapshots(self):
        vacation = self.create_vacation(
            start_date=date(2024, 2, 1), end_date=date(2024, 2, 1), note="Initial note"
        )
        corrected = correct_vacation(
            vacation=vacation,
            start_date=date(2024, 2, 1),
            end_date=date(2024, 2, 2),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Corrected note",
            scheduler=self.scheduler,
        )
        history_count = VacationChange.objects.count()
        self.assertEqual(
            correct_vacation(
                vacation=vacation,
                start_date=date(2024, 2, 1),
                end_date=date(2024, 2, 2),
                period_of_day=Vacation.PeriodOfDay.PM,
                note="Corrected note",
                scheduler=self.scheduler,
            ),
            corrected,
        )
        remove_vacation(vacation=vacation, scheduler=self.scheduler)

        changes = list(VacationChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes], ["created", "corrected", "removed"])
        self.assertEqual(changes[1].previous_value["end_date"], "2024-02-01")
        self.assertEqual(changes[1].recorded_value["period_of_day"], "PM")
        self.assertEqual(changes[1].changed_by, self.scheduler)
        self.assertEqual(changes[1].previous_note, "Initial note")
        self.assertEqual(changes[1].recorded_note, "Corrected note")
        self.assertEqual(changes[2].previous_value["end_date"], "2024-02-02")
        self.assertIsNone(changes[2].recorded_value)
        self.assertEqual(changes[2].previous_note, "Corrected note")
        self.assertIsNone(changes[2].recorded_note)
        self.assertIsNone(changes[2].vacation)
        self.assertEqual(changes[2].vacation_id_at_recording, vacation.pk)
        self.assertEqual(history_count, 2)
        self.assertFalse(Vacation.objects.exists())

    def test_vacation_mutations_require_an_authenticated_scheduler(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        with self.assertRaises(PermissionDenied):
            self.create_vacation(scheduler=anonymous)
        self.assertEqual(Vacation.objects.count(), 0)
        self.assertEqual(VacationChange.objects.count(), 0)


class AcademicEventServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.first = create_physician(display_name="First participant", scheduler=self.scheduler)
        self.second = create_physician(display_name="Second participant", scheduler=self.scheduler)
        self.third = create_physician(display_name="Third participant", scheduler=self.scheduler)
        self.definition = AcademicEventDefinition.objects.create(
            base_name="Off-site teaching", start_academic_year=2023
        )

    def create_event(self, **overrides):
        values = {
            "definition": self.definition,
            "academic_year": 2024,
            "start_date": date(2025, 2, 3),
            "end_date": date(2025, 2, 5),
            "participants": [self.first, self.second],
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_academic_event_occurrence(**values)

    def participant_ids(self, value):
        return {participant["id"] for participant in value["participants"]}

    def test_shared_event_defaults_to_whole_day_and_queries_are_inclusive(self):
        event = self.create_event()

        self.assertEqual(event.period_of_day, AcademicEvent.PeriodOfDay.WHOLE_DAY)
        self.assertTrue(academic_event_covers(event, self.first, date(2025, 2, 3), "AM"))
        self.assertTrue(academic_event_covers(event, self.second, date(2025, 2, 5), "PM"))
        self.assertFalse(academic_event_covers(event, self.third, date(2025, 2, 3), "AM"))
        self.assertFalse(academic_event_covers(event, self.first, date(2025, 2, 6), "AM"))
        self.assertEqual(
            list(academic_events_overlapping(date(2025, 2, 5), date(2025, 2, 5))), [event]
        )
        self.assertEqual(list(academic_events_for_physician(self.second)), [event])
        self.assertEqual(
            list(academic_events_covering(self.first, date(2025, 2, 5), "AM")), [event]
        )
        self.assertEqual(
            list(academic_event_sources_for(self.first, date(2025, 2, 5), "PM")), [event]
        )
        self.assertEqual(event.title, "Off-site teaching")

    def test_am_and_pm_events_preserve_period_precision_and_role_independence(self):
        am_only = self.create_event(
            start_date=date(2025, 2, 10),
            end_date=date(2025, 2, 10),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            participants=[self.first],
        )
        pm_only = self.create_event(
            definition=AcademicEventDefinition.objects.create(
                base_name="Afternoon seminar", start_academic_year=2024
            ),
            start_date=date(2025, 2, 12),
            end_date=date(2025, 2, 14),
            period_of_day=AcademicEvent.PeriodOfDay.PM,
            participants=[self.second],
        )

        self.assertTrue(am_only.covers(date(2025, 2, 10), "AM"))
        self.assertFalse(am_only.covers(date(2025, 2, 10), "PM"))
        self.assertTrue(pm_only.covers(date(2025, 2, 14), "PM"))
        self.assertFalse(pm_only.covers(date(2025, 2, 14), "AM"))
        self.assertEqual(
            list(academic_events_overlapping(date(2025, 2, 11), date(2025, 2, 12))), [pm_only]
        )
        self.assertFalse(self.first.roles.exists())
        add_role(
            physician=self.first,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        self.assertEqual(AcademicEvent.objects.get(pk=am_only.pk).participants.get(), self.first)

    def test_invalid_ranges_empty_participants_and_duplicate_create_are_atomic(self):
        event = self.create_event()
        history_count = AcademicEventChange.objects.count()

        self.assertEqual(self.create_event(reason="Repeated submission").pk, event.pk)
        with self.assertRaises(ValidationError):
            self.create_event(start_date=date(2025, 2, 6), end_date=date(2025, 2, 5))
        unpopulated = self.create_event(
            definition=AcademicEventDefinition.objects.create(
                base_name="Unpopulated", start_academic_year=2024
            ),
            participants=[],
        )
        self.assertEqual(unpopulated.participant_status, "Scheduled, unpopulated")
        with self.assertRaises(ValidationError):
            academic_events_overlapping(date(2025, 2, 6), date(2025, 2, 5))

        self.assertEqual(AcademicEvent.objects.count(), 2)
        self.assertEqual(AcademicEventChange.objects.count(), history_count + 1)

    def test_correction_changes_the_shared_participant_set_and_removal_retains_history(self):
        event = self.create_event(
            academic_year=2023, start_date=date(2024, 2, 1), end_date=date(2024, 2, 1)
        )
        corrected = correct_academic_event_occurrence(
            event=event,
            suffix="Corrected teaching",
            start_date=date(2024, 2, 1),
            end_date=date(2024, 2, 2),
            period_of_day=AcademicEvent.PeriodOfDay.PM,
            participants=[self.second, self.third],
            scheduler=self.scheduler,
            reason="Correct historical event",
        )
        history_count = AcademicEventChange.objects.count()
        self.assertEqual(
            correct_academic_event_occurrence(
                event=event,
                suffix="Corrected teaching",
                start_date=date(2024, 2, 1),
                end_date=date(2024, 2, 2),
                period_of_day=AcademicEvent.PeriodOfDay.PM,
                participants=[self.third, self.second],
                scheduler=self.scheduler,
                reason="Repeated submission",
            ).pk,
            corrected.pk,
        )
        self.assertEqual(AcademicEventChange.objects.count(), history_count)

        correction = AcademicEventChange.objects.filter(
            action=AcademicEventChange.Action.CORRECTED
        ).get()
        self.assertEqual(
            self.participant_ids(correction.previous_value), {self.first.pk, self.second.pk}
        )
        self.assertEqual(
            self.participant_ids(correction.recorded_value), {self.second.pk, self.third.pk}
        )
        self.assertEqual(correction.changed_by, self.scheduler)
        self.assertEqual(correction.reason, "Correct historical event")
        self.assertEqual(
            {
                physician.pk
                for physician in AcademicEvent.objects.get(pk=event.pk).participants.all()
            },
            {self.second.pk, self.third.pk},
        )

        remove_academic_event_occurrence(event=event, scheduler=self.scheduler)
        changes = list(AcademicEventChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes], ["created", "corrected", "removed"])
        self.assertEqual(
            self.participant_ids(changes[2].previous_value), {self.second.pk, self.third.pk}
        )
        self.assertFalse(changes[2].recorded_value["is_active"])
        self.assertEqual(changes[2].event, event)
        self.assertEqual(changes[2].event_id_at_recording, event.pk)
        self.assertFalse(AcademicEvent.objects.get(pk=event.pk).is_active)

    def test_mutations_require_an_authenticated_scheduler_and_optional_note(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        with self.assertRaises(PermissionDenied):
            self.create_event(scheduler=anonymous)
        self.assertEqual(AcademicEvent.objects.count(), 0)
        self.assertEqual(AcademicEventChange.objects.count(), 0)

        self.create_event()
        self.assertEqual(AcademicEventChange.objects.get().reason, "")

    def test_all_available_faculty_blocks_only_effective_faculty(self):
        faculty = self.first
        community = self.second
        add_role(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        event = self.create_event(all_available_faculty=True, participants=[])

        self.assertEqual(event.participant_status, "Complete")
        self.assertTrue(academic_event_covers(event, faculty, date(2025, 2, 3), "AM"))
        self.assertFalse(academic_event_covers(event, community, date(2025, 2, 3), "AM"))
        availability = effective_preceptor_availability_on(faculty, date(2025, 2, 3), "AM")
        self.assertEqual(availability.availability, "not_available")
        self.assertEqual(availability.academic_events, (event,))
        with self.assertRaises(ValidationError):
            self.create_event(
                definition=AcademicEventDefinition.objects.create(
                    base_name="Invalid collective event", start_academic_year=2024
                ),
                all_available_faculty=True,
                participants=[faculty],
            )


class OptionalChangeNoteServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def test_every_mutation_records_auditable_changes_without_a_note(self):
        physician = create_physician(display_name="Original", scheduler=self.scheduler)
        correct_physician_identity(
            physician=physician,
            display_name="Corrected",
            scheduler=self.scheduler,
            reason="  retained note  ",
        )
        faculty_role = add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        correct_role(
            effective_role=faculty_role,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="   ",
        )
        end_role(
            effective_role=faculty_role,
            effective_end=date(2025, 12, 31),
            scheduler=self.scheduler,
        )
        slots = {
            slot: FacultyAvailabilityTemplate.AvailabilityState.OPEN
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        template = create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        corrected_slots = {
            **slots,
            "monday_am": FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
        }
        correct_faculty_availability_template(
            template=template,
            effective_start=None,
            effective_end=None,
            slots=corrected_slots,
            scheduler=self.scheduler,
        )
        template_history_count = PhysicianChange.objects.filter(
            subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE
        ).count()
        correct_faculty_availability_template(
            template=template,
            effective_start=None,
            effective_end=None,
            slots=corrected_slots,
            scheduler=self.scheduler,
        )
        self.assertEqual(
            PhysicianChange.objects.filter(
                subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE
            ).count(),
            template_history_count,
        )
        vacation = create_vacation(
            physician=physician,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 3),
            scheduler=self.scheduler,
        )
        correct_vacation(
            vacation=vacation,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 4),
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.scheduler,
            note=" \n ",
        )
        remove_vacation(vacation=vacation, scheduler=self.scheduler)

        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        replacement = create_physician(display_name="Replacement", scheduler=self.scheduler)
        add_role(
            physician=replacement,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        assignment = assign_preceptor(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        set_preceptor_assignment_lock(
            assignment=assignment, is_locked=True, scheduler=self.scheduler, reason="  "
        )
        set_preceptor_assignment_lock(
            assignment=assignment, is_locked=False, scheduler=self.scheduler
        )
        assignment = assign_preceptor(
            session=session,
            physician=replacement,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        remove_preceptor_assignment(assignment=assignment, scheduler=self.scheduler)

        self.assertEqual(
            list(
                PhysicianChange.objects.filter(
                    physician=physician, subject=PhysicianChange.Subject.IDENTITY
                )
                .order_by("pk")
                .values_list("reason", flat=True)
            ),
            ["", "retained note"],
        )
        self.assertFalse(
            PhysicianChange.objects.filter(physician=physician)
            .exclude(changed_by=self.scheduler)
            .exists()
        )
        self.assertFalse(
            PhysicianChange.objects.filter(physician=physician, changed_at__isnull=True).exists()
        )
        self.assertTrue(
            all(
                change.previous_value is not None or change.recorded_value is not None
                for change in PhysicianChange.objects.filter(physician=physician)
            )
        )
        self.assertTrue(
            all(
                change.reason == ""
                for change in PhysicianChange.objects.filter(physician=physician).exclude(
                    subject=PhysicianChange.Subject.IDENTITY
                )
            )
        )
        self.assertEqual(VacationChange.objects.count(), 3)
        self.assertTrue(
            all(
                change.previous_note in (None, "") and change.recorded_note in (None, "")
                for change in VacationChange.objects.all()
            )
        )
        self.assertTrue(
            all(
                change.changed_by == self.scheduler
                and change.changed_at is not None
                and (change.previous_value is not None or change.recorded_value is not None)
                for change in VacationChange.objects.all()
            )
        )
        self.assertEqual(
            list(
                PreceptorAssignmentChange.objects.filter(session=session)
                .order_by("pk")
                .values_list("action", "reason")
            ),
            [
                ("assigned", ""),
                ("lock_changed", ""),
                ("lock_changed", ""),
                ("assigned", ""),
                ("removed", ""),
            ],
        )
        self.assertTrue(
            all(
                change.changed_by == self.scheduler
                and change.changed_at is not None
                and (change.previous_value is not None or change.recorded_value is not None)
                for change in PreceptorAssignmentChange.objects.filter(session=session)
            )
        )
        self.assertFalse(PreceptorAssignment.objects.filter(session=session).exists())

    def test_omitted_notes_do_not_bypass_authentication_or_other_validation(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        with self.assertRaises(PermissionDenied):
            create_physician(display_name="No access", scheduler=anonymous)

        physician = create_physician(display_name="Validated", scheduler=self.scheduler)
        history_count = PhysicianChange.objects.count()
        with self.assertRaises(ValidationError):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 2, 2),
                effective_end=date(2025, 2, 1),
                scheduler=self.scheduler,
            )

        self.assertEqual(PhysicianChange.objects.count(), history_count)
        self.assertFalse(physician.roles.exists())


class RoundingScheduleServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.normal = create_physician(display_name="Normal", scheduler=self.scheduler)
        self.replacement = create_physician(display_name="Replacement", scheduler=self.scheduler)
        self.casual = create_physician(display_name="Casual", scheduler=self.scheduler)
        self.other_casual = create_physician(display_name="Other casual", scheduler=self.scheduler)
        for physician in (self.normal, self.replacement):
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2024, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )

    def test_normal_rounders_are_one_per_monday_and_missing_weeks_remain_queryable(self):
        assignment = record_normal_rounder(
            week=date(2025, 2, 3), physician=self.normal, scheduler=self.scheduler
        )
        self.assertEqual(normal_rounder_for_date(date(2025, 2, 9)), assignment)
        self.assertEqual(
            missing_normal_rounder_weeks(date(2025, 2, 3), date(2025, 2, 16)),
            (date(2025, 2, 10),),
        )
        history_count = RoundingScheduleChange.objects.count()
        self.assertEqual(
            record_normal_rounder(
                week=date(2025, 2, 3), physician=self.normal, scheduler=self.scheduler
            ),
            assignment,
        )
        with self.assertRaises(ValidationError):
            record_normal_rounder(
                week=date(2025, 2, 4), physician=self.normal, scheduler=self.scheduler
            )
        self.assertEqual(RoundingScheduleChange.objects.count(), history_count)
        self.assertEqual(NormalRounder.objects.count(), 1)

    def test_normal_rounder_correction_removal_and_effect_are_retained(self):
        assignment = record_normal_rounder(
            week=date(2024, 2, 5),
            physician=self.normal,
            scheduler=self.scheduler,
            reason="Original",
        )
        record_normal_rounder(
            week=date(2024, 2, 5),
            physician=self.replacement,
            scheduler=self.scheduler,
            reason="Historical correction",
        )
        effect = rounding_effect_on(date(2024, 2, 6), "PM")
        self.assertEqual(effect.normal_rounder, assignment)
        self.assertEqual(effect.normal_rounder_physician, self.replacement)
        self.assertEqual(effect.availability, "not_available")
        remove_normal_rounder(
            normal_rounder=assignment, scheduler=self.scheduler, reason="Incorrect week"
        )
        changes = list(RoundingScheduleChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes], ["created", "corrected", "removed"])
        self.assertEqual(changes[1].previous_value["physician_id"], self.normal.pk)
        self.assertEqual(changes[1].recorded_value["physician_id"], self.replacement.pk)
        self.assertIsNone(changes[2].normal_rounder)
        self.assertEqual(changes[2].reason, "Incorrect week")

    def test_normal_rounder_requires_full_week_eligibility(self):
        partial = create_physician(
            display_name="Partial community rounder", scheduler=self.scheduler
        )
        add_role(
            physician=partial,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 16),
            scheduler=self.scheduler,
        )
        history_count = RoundingScheduleChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "full Monday-Sunday"):
            record_normal_rounder(
                week=date(2025, 2, 3), physician=partial, scheduler=self.scheduler
            )

        self.assertFalse(NormalRounder.objects.exists())
        self.assertEqual(RoundingScheduleChange.objects.count(), history_count)
        self.assertTrue(normal_rounder_covers_week(self.normal, date(2025, 2, 3)))

        eligible = record_normal_rounder(
            week=date(2025, 2, 3), physician=self.normal, scheduler=self.scheduler
        )
        replacement_history_count = RoundingScheduleChange.objects.count()
        with self.assertRaisesRegex(ValidationError, "full Monday-Sunday"):
            record_normal_rounder(
                week=date(2025, 2, 3), physician=partial, scheduler=self.scheduler
            )
        eligible.refresh_from_db()
        self.assertEqual(eligible.physician, self.normal)
        self.assertEqual(RoundingScheduleChange.objects.count(), replacement_history_count)

    def test_community_rounder_can_cover_a_complete_week_without_precepting_eligibility(self):
        community_rounder = create_physician(
            display_name="Community rounder", scheduler=self.scheduler
        )
        add_role(
            physician=community_rounder,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start=date(2025, 2, 3),
            effective_end=date(2025, 2, 9),
            scheduler=self.scheduler,
        )

        assignment = record_normal_rounder(
            week=date(2025, 2, 3), physician=community_rounder, scheduler=self.scheduler
        )

        self.assertEqual(assignment.physician, community_rounder)
        self.assertTrue(normal_rounder_covers_week(community_rounder, date(2025, 2, 3)))
        self.assertFalse(
            physicians_status_on(community_rounder, date(2025, 2, 3)).ordinarily_eligible_to_precept
        )

    def test_later_role_correction_retains_normal_rounder_and_reports_inconsistency(self):
        assignment = record_normal_rounder(
            week=date(2025, 2, 3), physician=self.normal, scheduler=self.scheduler
        )
        faculty_role = self.normal.roles.get(role=EffectiveRole.Role.FACULTY)
        correct_role(
            effective_role=faculty_role,
            effective_start=date(2024, 1, 1),
            effective_end=date(2025, 2, 5),
            scheduler=self.scheduler,
            reason="Corrected faculty range",
        )

        status = normal_rounder_status_on(date(2025, 2, 5))
        self.assertEqual(status.normal_rounder, assignment)
        self.assertFalse(status.eligible)
        self.assertTrue(status.inconsistent)
        self.assertIn("2025-02-09", status.reason)
        self.assertTrue(NormalRounder.objects.filter(pk=assignment.pk).exists())

    def test_matching_casual_rounding_preserves_community_preceptor_eligibility_and_commitment(
        self,
    ):
        dual = create_physician(display_name="Community casual", scheduler=self.scheduler)
        add_role(
            physician=dual,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        add_role(
            physician=dual,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        community_rule = create_community_preceptor_rule(
            physician=dual,
            weekday=1,
            ordinals=[2],
            periods=[CommunityPreceptorRule.PeriodOfDay.AM],
            scheduler=self.scheduler,
        )
        casual_rule = create_casual_rounder_rule(
            physician=dual,
            weekday=1,
            ordinals=[2],
            scheduler=self.scheduler,
        )

        on_date = date(2025, 2, 11)
        self.assertTrue(physicians_status_on(dual, on_date).ordinarily_eligible_to_precept)
        self.assertEqual(
            community_preceptor_availability_on(dual, on_date, "AM").supporting_rule,
            community_rule,
        )
        self.assertEqual(casual_rounder_rules_on(on_date), (casual_rule,))

    def test_casual_rules_resolve_all_ordinals_fifth_and_inclusive_effective_edges(self):
        rule = create_casual_rounder_rule(
            physician=self.casual,
            weekday=0,
            ordinals="all",
            effective_start=date(2025, 1, 6),
            effective_end=date(2025, 3, 31),
            scheduler=self.scheduler,
        )
        fifth = create_casual_rounder_rule(
            physician=self.casual,
            weekday=1,
            ordinals=[5],
            effective_start=None,
            effective_end=None,
            scheduler=self.scheduler,
        )
        self.assertEqual(rule.ordinals, [1, 2, 3, 4, 5])
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 6)), (rule,))
        self.assertEqual(casual_rounder_rules_on(date(2025, 3, 31)), (rule,))
        self.assertEqual(casual_rounder_rules_on(date(2025, 4, 7)), ())
        self.assertEqual(casual_rounder_rules_on(date(2025, 4, 29)), (fifth,))
        self.assertEqual(casual_rounder_rules_on(date(2025, 4, 22)), ())

    def test_casual_rules_resolve_mixed_ordinal_combinations(self):
        rule = create_casual_rounder_rule(
            physician=self.casual, weekday=2, ordinals=[1, 3, 5], scheduler=self.scheduler
        )

        self.assertEqual(rule.ordinals, [1, 3, 5])
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 1)), (rule,))
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 8)), ())
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 15)), (rule,))
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 22)), ())
        self.assertEqual(casual_rounder_rules_on(date(2025, 1, 29)), (rule,))

    def test_invalid_casual_rule_ordinals_and_effective_ranges_are_atomic(self):
        history_count = RoundingScheduleChange.objects.count()

        with self.assertRaises(ValidationError):
            create_casual_rounder_rule(
                physician=self.casual, weekday=0, ordinals=[], scheduler=self.scheduler
            )
        with self.assertRaises(ValidationError):
            create_casual_rounder_rule(
                physician=self.casual,
                weekday=0,
                ordinals=[1],
                effective_start=date(2025, 2, 2),
                effective_end=date(2025, 2, 1),
                scheduler=self.scheduler,
            )

        self.assertFalse(CasualRounderRule.objects.exists())
        self.assertEqual(RoundingScheduleChange.objects.count(), history_count)

    def test_casual_rule_ranges_are_scoped_by_physician_and_weekday_and_reject_overlaps_atomically(
        self,
    ):
        first = create_casual_rounder_rule(
            physician=self.casual,
            weekday=2,
            ordinals=[1],
            effective_start=None,
            effective_end=date(2025, 1, 31),
            scheduler=self.scheduler,
        )
        later = create_casual_rounder_rule(
            physician=self.casual,
            weekday=2,
            ordinals=[2],
            effective_start=date(2025, 2, 2),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_casual_rounder_rule(
            physician=self.casual,
            weekday=3,
            ordinals=[1],
            effective_start=None,
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_casual_rounder_rule(
            physician=self.other_casual,
            weekday=2,
            ordinals=[1],
            effective_start=None,
            effective_end=None,
            scheduler=self.scheduler,
        )
        count = CasualRounderRule.objects.count()
        history_count = RoundingScheduleChange.objects.count()
        with self.assertRaises(ValidationError):
            create_casual_rounder_rule(
                physician=self.casual,
                weekday=2,
                ordinals=[5],
                effective_start=date(2025, 1, 31),
                effective_end=None,
                scheduler=self.scheduler,
            )
        self.assertEqual(CasualRounderRule.objects.count(), count)
        self.assertEqual(RoundingScheduleChange.objects.count(), history_count)
        self.assertEqual((first.effective_start, later.effective_end), (None, None))

    def test_casual_effect_is_all_day_and_history_is_idempotent_and_authenticated(self):
        normal = record_normal_rounder(
            week=date(2025, 2, 10), physician=self.normal, scheduler=self.scheduler
        )
        rule = create_casual_rounder_rule(
            physician=self.casual,
            weekday=1,
            ordinals=[2],
            effective_start=None,
            effective_end=None,
            scheduler=self.scheduler,
        )
        for period in ("AM", "PM"):
            effect = rounding_effect_on(date(2025, 2, 11), period)
            self.assertEqual(effect.normal_rounder, normal)
            self.assertEqual(effect.casual_rounder_rules, (rule,))
            self.assertEqual(effect.casual_rounder_physicians, (self.casual,))
            self.assertEqual(effect.availability, "backup_only")
        history_count = RoundingScheduleChange.objects.count()
        correct_casual_rounder_rule(
            rule=rule,
            physician=self.casual,
            weekday=1,
            ordinals=[2],
            effective_start=None,
            effective_end=None,
            scheduler=self.scheduler,
            reason="Repeated",
        )
        self.assertEqual(RoundingScheduleChange.objects.count(), history_count)
        remove_casual_rounder_rule(rule=rule, scheduler=self.scheduler)
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        with self.assertRaises(PermissionDenied):
            record_normal_rounder(
                week=date(2025, 2, 10), physician=self.normal, scheduler=anonymous
            )

    def test_casual_rule_history_retains_complete_structured_provenance(self):
        rule = create_casual_rounder_rule(
            physician=self.casual,
            weekday=1,
            ordinals=[5, 1, 3],
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 6, 30),
            scheduler=self.scheduler,
            reason="Original coverage",
        )
        original_value = {
            "physician_id": self.casual.pk,
            "physician_display_name": "Casual",
            "weekday": 1,
            "ordinals": [1, 3, 5],
            "effective_start": "2025-01-01",
            "effective_end": "2025-06-30",
        }
        created = RoundingScheduleChange.objects.get()
        self.assertEqual(created.subject, RoundingScheduleChange.Subject.CASUAL_ROUNDER_RULE)
        self.assertEqual(created.action, RoundingScheduleChange.Action.CREATED)
        self.assertEqual(created.casual_rounder_rule, rule)
        self.assertEqual(created.source_id_at_recording, rule.pk)
        self.assertEqual(created.physician, self.casual)
        self.assertIsNone(created.previous_value)
        self.assertEqual(created.recorded_value, original_value)
        self.assertEqual(created.changed_by, self.scheduler)
        self.assertIsNotNone(created.changed_at)
        self.assertEqual(created.reason, "Original coverage")

        correct_casual_rounder_rule(
            rule=rule,
            physician=self.other_casual,
            weekday=4,
            ordinals=[2, 4],
            effective_start=date(2025, 7, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="New coverage",
        )
        corrected_value = {
            "physician_id": self.other_casual.pk,
            "physician_display_name": "Other casual",
            "weekday": 4,
            "ordinals": [2, 4],
            "effective_start": "2025-07-01",
            "effective_end": None,
        }
        corrected = RoundingScheduleChange.objects.get(
            action=RoundingScheduleChange.Action.CORRECTED
        )
        self.assertEqual(corrected.casual_rounder_rule, rule)
        self.assertEqual(corrected.source_id_at_recording, rule.pk)
        self.assertEqual(corrected.physician, self.other_casual)
        self.assertEqual(corrected.previous_value, original_value)
        self.assertEqual(corrected.recorded_value, corrected_value)
        self.assertEqual(corrected.changed_by, self.scheduler)
        self.assertIsNotNone(corrected.changed_at)
        self.assertEqual(corrected.reason, "New coverage")

        remove_casual_rounder_rule(rule=rule, scheduler=self.scheduler, reason="Retired")
        removed = RoundingScheduleChange.objects.get(action=RoundingScheduleChange.Action.REMOVED)
        self.assertIsNone(removed.casual_rounder_rule)
        self.assertEqual(removed.source_id_at_recording, rule.pk)
        self.assertEqual(removed.physician, self.other_casual)
        self.assertEqual(removed.previous_value, corrected_value)
        self.assertIsNone(removed.recorded_value)
        self.assertEqual(removed.changed_by, self.scheduler)
        self.assertIsNotNone(removed.changed_at)
        self.assertEqual(removed.reason, "Retired")


class CommunityPreceptorScheduleServicesTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = create_physician(
            display_name="Community", scheduler=self.scheduler, reason="Initial physician"
        )
        self.other_physician = create_physician(
            display_name="Other community", scheduler=self.scheduler, reason="Initial physician"
        )

    def create_rule(self, **overrides):
        values = {
            "physician": self.physician,
            "weekday": 1,
            "ordinals": [1],
            "periods": ["AM", "PM"],
            "effective_start": None,
            "effective_end": None,
            "scheduler": self.scheduler,
            "reason": "Initial Community commitment",
        }
        values.update(overrides)
        return create_community_preceptor_rule(**values)

    def test_ordinal_combinations_all_and_missing_fifth_resolve_without_role_state(self):
        mixed = self.create_rule(
            weekday=2,
            ordinals=[5, 1, 3],
            periods=[CommunityPreceptorRule.PeriodOfDay.AM],
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
        )
        all_rule = self.create_rule(
            weekday=1,
            ordinals="all",
            periods=[CommunityPreceptorRule.PeriodOfDay.PM],
            effective_start=date(2025, 4, 1),
            effective_end=date(2025, 4, 30),
        )
        self.create_rule(
            weekday=0,
            ordinals=[5],
            effective_start=date(2025, 2, 1),
            effective_end=date(2025, 2, 28),
        )

        self.assertEqual(mixed.ordinals, [1, 3, 5])
        self.assertEqual(all_rule.ordinals, [1, 2, 3, 4, 5])
        self.assertEqual(community_preceptor_rules_on(date(2025, 1, 1)), (mixed,))
        self.assertEqual(community_preceptor_rules_on(date(2025, 1, 15)), (mixed,))
        self.assertEqual(community_preceptor_rules_on(date(2025, 1, 29)), (mixed,))
        self.assertEqual(community_preceptor_rules_on(date(2025, 1, 8)), ())
        self.assertEqual(community_preceptor_rules_on(date(2025, 4, 29)), (all_rule,))
        self.assertEqual(community_preceptor_rules_on(date(2025, 4, 8)), (all_rule,))
        self.assertEqual(community_preceptor_rules_on(date(2025, 2, 24)), ())
        self.assertFalse(self.physician.roles.exists())

    def test_default_both_am_pm_and_expected_unmatched_status_are_role_independent(self):
        rule = self.create_rule(
            weekday=1,
            ordinals=[2],
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            periods=None,
        )

        self.assertEqual(rule.periods, ["AM", "PM"])
        for period in ("AM", "PM"):
            status = community_preceptor_availability_on(self.physician, date(2025, 1, 14), period)
            self.assertEqual(status.base_availability, "available")
            self.assertTrue(status.is_expected_commitment)
            self.assertTrue(status.expected_commitment)
            self.assertEqual(status.supporting_rule, rule)
            self.assertEqual(status.source, "manual")

        unmatched = community_preceptor_availability_on(self.physician, date(2025, 1, 14), "AM")
        self.assertEqual(unmatched.supporting_rule, rule)
        not_selected = community_preceptor_availability_on(self.physician, date(2025, 1, 14), "PM")
        self.assertEqual(not_selected.base_availability, "available")

        no_match = community_preceptor_availability_on(self.physician, date(2025, 1, 21), "AM")
        self.assertEqual(no_match.base_availability, "not_available")
        self.assertFalse(no_match.is_expected_commitment)
        self.assertIsNone(no_match.supporting_rule)
        self.assertIsNone(no_match.source)

    def test_am_only_pm_only_and_inclusive_effective_edges(self):
        am_only = self.create_rule(
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        pm_only = self.create_rule(
            weekday=1,
            ordinals=[2],
            periods=["PM"],
            effective_start=date(2025, 2, 5),
            effective_end=date(2025, 2, 11),
        )

        self.assertEqual(
            community_preceptor_availability_on(
                self.physician, date(2025, 2, 4), "AM"
            ).supporting_rule,
            am_only,
        )
        self.assertEqual(
            community_preceptor_availability_on(
                self.physician, date(2025, 2, 4), "PM"
            ).base_availability,
            "not_available",
        )
        self.assertEqual(
            community_preceptor_availability_on(
                self.physician, date(2025, 2, 11), "PM"
            ).supporting_rule,
            pm_only,
        )
        self.assertEqual(
            community_preceptor_availability_on(
                self.physician, date(2025, 2, 11), "AM"
            ).base_availability,
            "not_available",
        )
        self.assertEqual(
            community_preceptor_availability_on(
                self.physician, date(2025, 2, 12), "PM"
            ).base_availability,
            "not_available",
        )

    def test_occurrences_preserve_periods_physicians_effective_edges_and_missing_fifth(self):
        community = self.create_rule(
            weekday=1,
            ordinals=[1],
            periods=["AM", "PM"],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        other = self.create_rule(
            physician=self.other_physician,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        second_and_fifth = self.create_rule(
            physician=create_physician(display_name="Fifth", scheduler=self.scheduler),
            weekday=1,
            ordinals=[2, 5],
            periods=["PM"],
        )

        occurrences = community_preceptor_occurrences(date(2025, 2, 4), date(2025, 2, 28))

        self.assertEqual(
            [
                (
                    occurrence.date,
                    occurrence.period,
                    occurrence.physician_display_name,
                    occurrence.physician_id,
                    occurrence.rule_id,
                )
                for occurrence in occurrences
            ],
            [
                (date(2025, 2, 4), "AM", "Community", self.physician.pk, community.pk),
                (date(2025, 2, 4), "AM", "Other community", self.other_physician.pk, other.pk),
                (date(2025, 2, 4), "PM", "Community", self.physician.pk, community.pk),
                (
                    date(2025, 2, 11),
                    "PM",
                    "Fifth",
                    second_and_fifth.physician_id,
                    second_and_fifth.pk,
                ),
            ],
        )
        self.assertEqual(community_preceptor_occurrences(date(2025, 2, 5), date(2025, 2, 5)), ())
        self.assertNotIn(date(2025, 2, 25), [occurrence.date for occurrence in occurrences])

    def test_month_projection_uses_real_month_dates_and_bounded_rule_and_holiday_queries(self):
        in_month = self.create_rule(
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        outside_month = self.create_rule(
            physician=self.other_physician,
            weekday=1,
            ordinals=[1],
            periods=["PM"],
            effective_start=date(2025, 3, 4),
            effective_end=date(2025, 3, 4),
        )

        with CaptureQueriesContext(connection) as queries:
            occurrences = community_preceptor_month_projection(date(2025, 2, 17))

        self.assertLessEqual(len(queries), 2)
        self.assertEqual(
            [
                (occurrence.date, occurrence.period, occurrence.rule_id)
                for occurrence in occurrences
            ],
            [(date(2025, 2, 4), "AM", in_month.pk)],
        )
        self.assertNotIn(outside_month.pk, [occurrence.rule_id for occurrence in occurrences])

    def test_occurrences_exclude_holiday_closed_coverage(self):
        self.create_rule(weekday=3, ordinals=[4], periods=["AM", "PM"])

        occurrences = community_preceptor_occurrences(date(2025, 12, 25), date(2025, 12, 25))

        self.assertEqual(occurrences, ())

    def test_ranges_are_scoped_by_physician_and_weekday_allow_gaps_and_reject_overlap_edges(self):
        first = self.create_rule(
            effective_end=date(2025, 1, 31),
            reason="First range",
        )
        later = self.create_rule(
            ordinals=[2],
            effective_start=date(2025, 2, 2),
            effective_end=None,
            reason="Later range",
        )
        self.create_rule(weekday=2, reason="Different weekday")
        self.create_rule(physician=self.other_physician, reason="Different physician")
        count = CommunityPreceptorRule.objects.count()
        history_count = CommunityPreceptorScheduleChange.objects.count()

        with self.assertRaises(ValidationError):
            self.create_rule(
                ordinals=[3],
                effective_start=date(2025, 1, 31),
                effective_end=None,
            )

        self.assertEqual(CommunityPreceptorRule.objects.count(), count)
        self.assertEqual(CommunityPreceptorScheduleChange.objects.count(), history_count)
        self.assertIsNone(first.effective_start)
        self.assertIsNone(later.effective_end)

    def test_reversed_empty_and_invalid_period_mutations_are_atomic(self):
        history_count = CommunityPreceptorScheduleChange.objects.count()
        for values in (
            {"ordinals": []},
            {"periods": []},
            {"effective_start": date(2025, 3, 2), "effective_end": date(2025, 3, 1)},
            {"periods": ["AM", "invalid"]},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.create_rule(**values)

        self.assertFalse(CommunityPreceptorRule.objects.exists())
        self.assertEqual(CommunityPreceptorScheduleChange.objects.count(), history_count)

    def test_correction_removal_history_is_complete_and_unchanged_is_idempotent(self):
        rule = self.create_rule(
            weekday=1,
            ordinals=[1, 3, 5],
            periods=["AM"],
            effective_start=date(2024, 1, 1),
            effective_end=date(2024, 6, 30),
            reason="Original coverage",
        )
        original_value = {
            "physician_id": self.physician.pk,
            "physician_display_name": "Community",
            "weekday": 1,
            "ordinals": [1, 3, 5],
            "periods": ["AM"],
            "effective_start": "2024-01-01",
            "effective_end": "2024-06-30",
        }
        created = CommunityPreceptorScheduleChange.objects.get()
        self.assertEqual(created.action, CommunityPreceptorScheduleChange.Action.CREATED)
        self.assertEqual(created.community_preceptor_rule, rule)
        self.assertEqual(created.source_id_at_recording, rule.pk)
        self.assertEqual(created.physician, self.physician)
        self.assertEqual(created.recorded_value, original_value)
        self.assertEqual(created.changed_by, self.scheduler)
        self.assertEqual(created.reason, "Original coverage")

        corrected = correct_community_preceptor_rule(
            rule=rule,
            physician=self.other_physician,
            weekday=4,
            ordinals=[2, 4],
            periods=["PM"],
            effective_start=date(2024, 7, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Historical correction",
        )
        corrected_value = {
            "physician_id": self.other_physician.pk,
            "physician_display_name": "Other community",
            "weekday": 4,
            "ordinals": [2, 4],
            "periods": ["PM"],
            "effective_start": "2024-07-01",
            "effective_end": None,
        }
        history_count = CommunityPreceptorScheduleChange.objects.count()
        self.assertEqual(corrected, rule)
        self.assertEqual(
            CommunityPreceptorScheduleChange.objects.get(
                action=CommunityPreceptorScheduleChange.Action.CORRECTED
            ).previous_value,
            original_value,
        )
        correction = CommunityPreceptorScheduleChange.objects.get(
            action=CommunityPreceptorScheduleChange.Action.CORRECTED
        )
        self.assertEqual(correction.recorded_value, corrected_value)
        self.assertEqual(correction.physician, self.other_physician)
        self.assertEqual(correction.reason, "Historical correction")

        self.assertEqual(
            correct_community_preceptor_rule(
                rule=rule,
                physician=self.other_physician,
                weekday=4,
                ordinals=[4, 2],
                periods=["PM"],
                effective_start=date(2024, 7, 1),
                effective_end=None,
                scheduler=self.scheduler,
                reason="Repeated submission",
            ),
            rule,
        )
        self.assertEqual(CommunityPreceptorScheduleChange.objects.count(), history_count)

        remove_community_preceptor_rule(rule=rule, scheduler=self.scheduler, reason="Retired")
        changes = list(CommunityPreceptorScheduleChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes], ["created", "corrected", "removed"])
        self.assertIsNone(changes[-1].community_preceptor_rule)
        self.assertEqual(changes[-1].source_id_at_recording, rule.pk)
        self.assertEqual(changes[-1].physician, self.other_physician)
        self.assertEqual(changes[-1].previous_value, corrected_value)
        self.assertIsNone(changes[-1].recorded_value)
        self.assertEqual(changes[-1].reason, "Retired")
        self.assertFalse(CommunityPreceptorRule.objects.filter(pk=rule.pk).exists())

    def test_mutations_require_an_authenticated_scheduler(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        with self.assertRaises(PermissionDenied):
            self.create_rule(scheduler=anonymous)
        self.assertFalse(CommunityPreceptorRule.objects.exists())
        self.assertFalse(CommunityPreceptorScheduleChange.objects.exists())


class EffectivePreceptorAvailabilityTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.faculty = self.physician_with_role("Faculty", EffectiveRole.Role.FACULTY)
        self.community = self.physician_with_role(
            "Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR
        )
        self.casual = create_physician(display_name="Casual", scheduler=self.scheduler)

    def physician_with_role(self, name, role):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=role,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        return physician

    def faculty_template(self, **overrides):
        slots = {
            slot: FacultyAvailabilityTemplate.AvailabilityState.OPEN
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        slots.update(overrides)
        return create_faculty_availability_template(
            physician=self.faculty,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )

    def test_selects_one_role_base_and_preserves_each_faculty_state(self):
        template = self.faculty_template(monday_pm="backup_only", tuesday_am="off")

        available = effective_preceptor_availability_on(self.faculty, date(2025, 1, 6), "AM")
        backup_only = effective_preceptor_availability_on(self.faculty, date(2025, 1, 6), "PM")
        not_available = effective_preceptor_availability_on(self.faculty, date(2025, 1, 7), "AM")

        self.assertEqual(
            [result.availability for result in (available, backup_only, not_available)],
            ["available", "backup_only", "not_available"],
        )
        self.assertTrue(available.ordinarily_eligible_to_precept)
        self.assertTrue(available.faculty_equivalent)
        self.assertEqual(available.effective_category, EffectiveRole.Role.FACULTY)
        self.assertEqual(available.base_state, "open")
        self.assertEqual(available.sources[0].record, template)
        self.assertEqual(available.sources[0].reason, "base_availability")

    def test_patient_care_retains_its_base_state_but_is_effectively_backup_only(self):
        template = self.faculty_template(monday_am="patient_care")

        result = effective_preceptor_availability_on(self.faculty, date(2025, 1, 6), "AM")

        self.assertEqual(result.base_availability, "patient_care")
        self.assertEqual(result.base_availability_display, "Patient care")
        self.assertEqual(result.availability, "backup_only")
        self.assertEqual(
            (
                result.sources[0].source_type,
                result.sources[0].contribution,
                result.sources[0].record,
            ),
            ("faculty_template", "patient_care", template),
        )

    def test_external_patient_care_restricts_patient_care_only_within_its_dates(self):
        template = self.faculty_template(monday_am="patient_care")
        role = add_role(
            physician=self.faculty,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start=date(2025, 1, 13),
            effective_end=date(2025, 1, 19),
            scheduler=self.scheduler,
        )

        before = effective_preceptor_availability_on(self.faculty, date(2025, 1, 6), "AM")
        during = effective_preceptor_availability_on(self.faculty, date(2025, 1, 13), "AM")
        after = effective_preceptor_availability_on(self.faculty, date(2025, 1, 20), "AM")

        self.assertEqual(
            [(result.base_availability, result.availability) for result in (before, during, after)],
            [
                ("patient_care", "backup_only"),
                ("patient_care", "not_available"),
                ("patient_care", "backup_only"),
            ],
        )
        self.assertEqual(
            [
                (source.source_type, source.source_id, source.contribution)
                for source in during.sources
            ],
            [
                ("faculty_template", template.pk, "patient_care"),
                ("external_patient_care", role.pk, "not_available"),
            ],
        )

    def test_patient_care_policy_preserves_monthly_custom_and_hard_event_composition(self):
        template = self.faculty_template(monday_am="open")
        create_monthly_physician_availability_rule(
            physician=self.faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("patient_care"),
            scheduler=self.scheduler,
        )
        category = create_custom_availability_category(
            name="Office work", impact="available", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.faculty], scheduler=self.scheduler
        )
        slots = dict(template.slots)
        slots["tuesday_am"] = FacultyAvailabilityTemplate.category_selection(category)
        correct_faculty_availability_template(
            template=template,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        definition = AcademicEventDefinition.objects.create(
            base_name="Hard event", start_academic_year=2024
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=date(2025, 1, 20),
            end_date=date(2025, 1, 20),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            participants=[self.faculty],
            scheduler=self.scheduler,
        )

        monthly_patient_care = effective_preceptor_availability_on(
            self.faculty, date(2025, 1, 6), "AM"
        )
        custom_option = effective_preceptor_availability_on(self.faculty, date(2025, 1, 7), "AM")
        hard_event = effective_preceptor_availability_on(self.faculty, date(2025, 1, 20), "AM")

        self.assertEqual(
            (monthly_patient_care.base_availability, monthly_patient_care.availability),
            ("patient_care", "backup_only"),
        )
        self.assertEqual(
            (custom_option.base_availability, custom_option.availability),
            ("available", "available"),
        )
        self.assertEqual(
            (hard_event.base_availability, hard_event.availability),
            ("open", "not_available"),
        )

    def test_community_commitment_and_blockers_keep_all_source_identities(self):
        rule = create_community_preceptor_rule(
            physician=self.community,
            weekday=1,
            ordinals=[1],
            periods=["AM", "PM"],
            scheduler=self.scheduler,
        )
        vacation = create_vacation(
            physician=self.community,
            start_date=date(2025, 2, 4),
            end_date=date(2025, 2, 4),
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Teaching", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=date(2025, 2, 4),
            end_date=date(2025, 2, 4),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            participants=[self.community],
            scheduler=self.scheduler,
        )

        blocked = effective_preceptor_availability_on(self.community, date(2025, 2, 4), "AM")
        pm = effective_preceptor_availability_on(self.community, date(2025, 2, 4), "PM")

        self.assertEqual(blocked.effective_category, EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.assertEqual(blocked.base_availability, "available")
        self.assertEqual(blocked.availability, "not_available")
        self.assertTrue(blocked.expected_community_commitment)
        self.assertTrue(blocked.expected_but_blocked)
        self.assertEqual(blocked.community_preceptor_rule, rule)
        self.assertEqual(blocked.vacations, (vacation,))
        self.assertEqual(blocked.academic_events, (event,))
        self.assertEqual(
            [(source.source_type, source.source_id) for source in blocked.sources],
            [
                ("community_preceptor_rule", rule.pk),
                ("vacation", vacation.pk),
                ("academic_event", event.pk),
            ],
        )
        self.assertEqual(pm.availability, "available")
        self.assertTrue(pm.is_expected_community_commitment)

    def test_community_preceptor_without_matching_rule_is_not_available(self):
        result = effective_preceptor_availability_on(self.community, date(2025, 2, 3), "AM")

        self.assertTrue(result.ordinarily_eligible_to_precept)
        self.assertEqual(result.effective_category, EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        self.assertEqual(result.base_availability, "not_available")
        self.assertEqual(result.availability, "not_available")
        self.assertFalse(result.expected_community_commitment)
        self.assertIsNone(result.community_preceptor_rule)
        self.assertEqual(result.sources, ())

    def test_rounding_is_restrictive_for_only_the_normal_faculty_rounder(self):
        self.faculty_template(tuesday_am="off")
        normal = record_normal_rounder(
            week=date(2025, 2, 3), physician=self.faculty, scheduler=self.scheduler
        )
        casual_rule = create_casual_rounder_rule(
            physician=self.community, weekday=1, ordinals=[1], scheduler=self.scheduler
        )
        other_casual_rule = create_casual_rounder_rule(
            physician=self.casual, weekday=1, ordinals=[1], scheduler=self.scheduler
        )
        add_role(
            physician=self.community,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        community_rule = create_community_preceptor_rule(
            physician=self.community,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )

        ordinary_rounding = effective_preceptor_availability_on(
            self.faculty, date(2025, 2, 3), "AM"
        )
        casual_coverage = effective_preceptor_availability_on(self.faculty, date(2025, 2, 4), "PM")
        blocked_base = effective_preceptor_availability_on(self.faculty, date(2025, 2, 4), "AM")
        community = effective_preceptor_availability_on(self.community, date(2025, 2, 4), "AM")
        event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Covered meeting", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=date(2025, 2, 4),
            end_date=date(2025, 2, 4),
            period_of_day=AcademicEvent.PeriodOfDay.PM,
            participants=[self.faculty],
            scheduler=self.scheduler,
        )
        blocked_casual_coverage = effective_preceptor_availability_on(
            self.faculty, date(2025, 2, 4), "PM"
        )

        self.assertEqual(ordinary_rounding.availability, "not_available")
        self.assertEqual(casual_coverage.availability, "backup_only")
        self.assertEqual(blocked_base.base_availability, "off")
        self.assertEqual(blocked_base.availability, "not_available")
        self.assertEqual(casual_coverage.normal_rounder, normal)
        self.assertEqual(
            {rule.pk for rule in casual_coverage.casual_rounder_rules},
            {casual_rule.pk, other_casual_rule.pk},
        )
        self.assertEqual(
            [source.source_type for source in casual_coverage.sources[-3:]],
            ["normal_rounder", "casual_rounder_rule", "casual_rounder_rule"],
        )
        self.assertEqual(blocked_casual_coverage.availability, "not_available")
        self.assertEqual(blocked_casual_coverage.academic_events, (event,))
        self.assertEqual(community.availability, "available")
        self.assertEqual(community.community_preceptor_rule, community_rule)
        self.assertIsNone(community.normal_rounder)
        self.assertEqual(community.casual_rounder_rules, ())

    def test_period_ranges_and_blocker_precedence_preserve_every_match(self):
        self.faculty_template()
        vacation = create_vacation(
            physician=self.faculty,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 5),
            period_of_day=Vacation.PeriodOfDay.PM,
            scheduler=self.scheduler,
        )
        event = create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="All-day meeting", start_academic_year=2024
            ),
            academic_year=2024,
            start_date=date(2025, 2, 5),
            end_date=date(2025, 2, 5),
            participants=[self.faculty],
            scheduler=self.scheduler,
        )

        before_period = effective_preceptor_availability_on(self.faculty, date(2025, 2, 3), "AM")
        range_start = effective_preceptor_availability_on(self.faculty, date(2025, 2, 3), "PM")
        range_end = effective_preceptor_availability_on(self.faculty, date(2025, 2, 5), "PM")

        self.assertEqual(before_period.availability, "available")
        self.assertEqual(range_start.availability, "not_available")
        self.assertEqual(range_end.availability, "not_available")
        self.assertEqual(
            [(source.source_type, source.source_id) for source in range_end.sources[-2:]],
            [("vacation", vacation.pk), ("academic_event", event.pk)],
        )

    def test_ineligible_retained_sources_are_explanatory_not_a_base_category(self):
        inactive = create_physician(display_name="Inactive", scheduler=self.scheduler)
        vacation = create_vacation(
            physician=inactive,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 3),
            scheduler=self.scheduler,
        )

        result = effective_preceptor_availability_on(inactive, date(2025, 2, 3), "AM")

        self.assertFalse(result.ordinarily_eligible_to_precept)
        self.assertIsNone(result.effective_category)
        self.assertIsNone(result.base_availability)
        self.assertIsNone(result.availability)
        self.assertEqual(result.vacations, (vacation,))
        self.assertEqual(result.sources[0].source_type, "vacation")

    def test_weekends_and_batched_queries_are_deterministic_and_read_only(self):
        self.faculty_template()
        create_community_preceptor_rule(
            physician=self.community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        before = (
            Physician.objects.count(),
            Vacation.objects.count(),
            AcademicEvent.objects.count(),
            NormalRounder.objects.count(),
            CasualRounderRule.objects.count(),
            CommunityPreceptorRule.objects.count(),
            SchedulePeriod.objects.count(),
            PreceptorSession.objects.count(),
            PreceptorAssignment.objects.count(),
            PhysicianChange.objects.count(),
            VacationChange.objects.count(),
            AcademicEventChange.objects.count(),
            RoundingScheduleChange.objects.count(),
            CommunityPreceptorScheduleChange.objects.count(),
        )

        weekend = effective_preceptor_availability_on(self.faculty, date(2025, 2, 8), "AM")
        first = effective_preceptor_availability_for_physicians(
            (self.faculty, self.community), date(2025, 2, 3), "AM"
        )
        second = effective_preceptor_availability_for_physicians(
            (self.faculty, self.community), date(2025, 2, 3), "AM"
        )

        self.assertFalse(weekend.is_applicable)
        self.assertIsNone(weekend.availability)
        self.assertEqual(weekend.sources, ())
        self.assertEqual(
            [(result.physician.pk, result.availability, result.sources) for result in first],
            [(result.physician.pk, result.availability, result.sources) for result in second],
        )
        self.assertEqual(
            (
                Physician.objects.count(),
                Vacation.objects.count(),
                AcademicEvent.objects.count(),
                NormalRounder.objects.count(),
                CasualRounderRule.objects.count(),
                CommunityPreceptorRule.objects.count(),
                SchedulePeriod.objects.count(),
                PreceptorSession.objects.count(),
                PreceptorAssignment.objects.count(),
                PhysicianChange.objects.count(),
                VacationChange.objects.count(),
                AcademicEventChange.objects.count(),
                RoundingScheduleChange.objects.count(),
                CommunityPreceptorScheduleChange.objects.count(),
            ),
            before,
        )


class RoundingMonthProjectionTests(TestCase):
    def setUp(self):
        self.normal = Physician.objects.create(display_name="Normal")
        self.role = EffectiveRole.objects.create(
            physician=self.normal,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
        )

    def test_uses_complete_sunday_rows_and_splits_normal_weeks_at_row_boundaries(self):
        assignment = NormalRounder.objects.create(week=date(2026, 7, 27), physician=self.normal)

        projection = rounding_month_projection(date(2026, 8, 14))

        self.assertEqual(projection.selected_month, date(2026, 8, 1))
        self.assertEqual(projection.calendar_weeks[0].start_date, date(2026, 7, 26))
        self.assertEqual(projection.calendar_weeks[-1].end_date, date(2026, 9, 5))
        self.assertEqual(len(projection.grid_dates), 42)
        self.assertEqual(
            [week.week for week in projection.normal_weeks],
            [
                date(2026, 7, 27),
                date(2026, 8, 3),
                date(2026, 8, 10),
                date(2026, 8, 17),
                date(2026, 8, 24),
                date(2026, 8, 31),
            ],
        )
        segments = [
            segment
            for segment in projection.normal_week_segments
            if segment.source_id == assignment.pk
        ]
        self.assertEqual(
            [(segment.start_date, segment.end_date) for segment in segments],
            [(date(2026, 7, 27), date(2026, 8, 1)), (date(2026, 8, 2), date(2026, 8, 2))],
        )
        self.assertEqual(
            segments[0].displayed_dates,
            tuple(date(2026, 7, 27) + timedelta(days=i) for i in range(6)),
        )
        self.assertEqual(segments[0].physician, self.normal)
        self.assertFalse(segments[0].missing)

    def test_resolves_casual_rules_in_display_order_with_effective_bounds_and_no_fifth_date(self):
        alpha = Physician.objects.create(display_name="alpha")
        beta = Physician.objects.create(display_name="Beta")
        alpha_rule = CasualRounderRule.objects.create(
            physician=alpha,
            weekday=1,
            ordinals=[1],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        beta_rule = CasualRounderRule.objects.create(
            physician=beta,
            weekday=1,
            ordinals=[1],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        fifth_tuesday = CasualRounderRule.objects.create(
            physician=self.normal,
            weekday=1,
            ordinals=[5],
        )

        projection = rounding_month_projection(date(2025, 2, 1))
        february_fourth = next(row for row in projection.grid_dates if row.date == date(2025, 2, 4))

        self.assertEqual(february_fourth.casual_rounder_rules, (alpha_rule, beta_rule))
        self.assertEqual(february_fourth.casual_rounder_physicians, (alpha, beta))
        self.assertTrue(
            all(fifth_tuesday not in day.casual_rounder_rules for day in projection.grid_dates)
        )
        self.assertEqual(len(projection.list_rows), 5)

    def test_list_rows_are_monday_sunday_weeks_that_intersect_the_selected_month(self):
        for month, expected_weeks in (
            (
                date(2024, 2, 20),
                [
                    date(2024, 1, 29),
                    date(2024, 2, 5),
                    date(2024, 2, 12),
                    date(2024, 2, 19),
                    date(2024, 2, 26),
                ],
            ),
            (
                date(2025, 4, 20),
                [
                    date(2025, 3, 31),
                    date(2025, 4, 7),
                    date(2025, 4, 14),
                    date(2025, 4, 21),
                    date(2025, 4, 28),
                ],
            ),
        ):
            with self.subTest(month=month):
                projection = rounding_month_projection(month)

                self.assertEqual(
                    [row.normal_week.week for row in projection.list_rows], expected_weeks
                )
                self.assertTrue(
                    all(
                        row.end_date == row.normal_week.week + timedelta(days=6)
                        for row in projection.list_rows
                    )
                )

    def test_list_rows_include_chronological_deterministic_casual_occurrences(self):
        assignment = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        alpha = Physician.objects.create(display_name="Alpha casual")
        beta = Physician.objects.create(display_name="Beta casual")
        thursday = Physician.objects.create(display_name="Thursday casual")
        boundary = Physician.objects.create(display_name="Boundary casual")
        alpha_rule = CasualRounderRule.objects.create(physician=alpha, weekday=1, ordinals=[1])
        beta_rule = CasualRounderRule.objects.create(physician=beta, weekday=1, ordinals=[1])
        thursday_rule = CasualRounderRule.objects.create(
            physician=thursday, weekday=3, ordinals=[1]
        )
        boundary_rule = CasualRounderRule.objects.create(
            physician=boundary, weekday=6, ordinals=[1]
        )

        projection = rounding_month_projection(date(2025, 2, 1))
        row = next(row for row in projection.list_rows if row.normal_week.week == assignment.week)

        self.assertFalse(row.normal_week.missing)
        self.assertFalse(row.normal_week.inconsistent)
        self.assertEqual(
            [(occurrence.rule, occurrence.date) for occurrence in row.casual_rounder_occurrences],
            [
                (alpha_rule, date(2025, 2, 4)),
                (beta_rule, date(2025, 2, 4)),
                (thursday_rule, date(2025, 2, 6)),
            ],
        )
        boundary_row = projection.list_rows[-1]
        self.assertEqual(boundary_row.normal_week.week, date(2025, 2, 24))
        self.assertEqual(
            [
                (occurrence.rule, occurrence.date)
                for occurrence in boundary_row.casual_rounder_occurrences
            ],
            [(boundary_rule, date(2025, 3, 2))],
        )

    def test_summarizes_unique_missing_and_inconsistent_weeks_without_adjacent_day_inflation(self):
        assignment = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        self.role.effective_end = date(2025, 2, 5)
        self.role.save(update_fields=("effective_end",))

        projection = rounding_month_projection(date(2025, 2, 15))
        assigned_week = next(
            week for week in projection.normal_weeks if week.week == assignment.week
        )

        self.assertTrue(assigned_week.inconsistent)
        self.assertIn("2025-02-09", assigned_week.inconsistency_reason)
        self.assertEqual(assigned_week.source_id, assignment.pk)
        self.assertEqual(projection.work_summary.inconsistent_normal_weeks, (assigned_week,))
        self.assertEqual(projection.work_summary.inconsistent_normal_week_count, 1)
        self.assertEqual(projection.work_summary.missing_normal_week_count, 4)
        self.assertEqual(
            [week.week for week in projection.work_summary.missing_normal_weeks],
            [date(2025, 1, 27), date(2025, 2, 10), date(2025, 2, 17), date(2025, 2, 24)],
        )

    def test_uses_bounded_queries_for_the_whole_month(self):
        NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        CasualRounderRule.objects.create(physician=self.normal, weekday=1, ordinals=[1])

        with CaptureQueriesContext(connection) as queries:
            projection = rounding_month_projection(date(2025, 2, 1))

        self.assertLessEqual(len(queries), 3)
        self.assertEqual(len(projection.grid_dates), 35)
        self.assertEqual(
            len(projection.grid_dates), len({grid_date.date for grid_date in projection.grid_dates})
        )
