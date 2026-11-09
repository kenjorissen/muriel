from datetime import UTC, date, datetime
from threading import Barrier, Lock, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    ClinicHoliday,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    LateDocAssignment,
    LateDocAssignmentChange,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.academic_events import create_academic_event_occurrence
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence
from schedules.services.late_docs import (
    assign_late_doc,
    late_doc_assignment_candidates_on,
    late_doc_assignment_for_date,
    remove_late_doc_assignment,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import (
    assign_preceptor,
    draft_findings_for_week,
    open_weekly_draft,
)
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser


def assert_rendered_late_doc_action(test_case, response, *, action, physician):
    """Locate a Late Doc action by its rendered target rather than calendar layout."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    links = [
        element
        for element in parser.elements
        if element.tag == "a" and _action_matches(element.attributes.get("href", ""), action)
    ]
    test_case.assertEqual(len(links), 1)
    test_case.assertIn(physician, links[0].text())
    test_case.assertIn("Late Doc", links[0].attributes.get("aria-label", ""))
    return links[0]


def late_doc_list_cell(response, on_date):
    """Return the rendered Late Doc field for the dated list record."""
    table = _RenderedDataTable(
        response.content.decode(), table_class="preceptor-schedule-list-table"
    )
    rows = [
        row
        for row in table.body_rows()
        if any(
            cell.tag == "time" and cell.attributes.get("datetime") == on_date.isoformat()
            for cell in table.cell_for_header(row, "Date").descendants_named("time")
        )
        and table.cell_for_header(row, "Late Doc").text().strip()
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"Expected one rendered schedule record for {on_date}, found {len(rows)}."
        )
    return table.cell_for_header(rows[0], "Late Doc")


def assert_rendered_late_doc_slot(test_case, response, *, on_date, physician):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    slots = [
        element
        for element in parser.elements
        if element.tag == "li"
        and "Late Doc" in element.attributes.get("aria-label", "")
        and physician in element.text()
    ]
    test_case.assertEqual(len(slots), 1)
    week = next(
        ancestor
        for ancestor in slots[0].ancestors()
        if ancestor.tag == "section" and ancestor.attributes.get("role") == "rowgroup"
    )
    test_case.assertEqual(
        [
            header.attributes.get("datetime")
            for header in week.descendants()
            if header.tag == "time" and header.attributes.get("datetime") == on_date.isoformat()
        ],
        [on_date.isoformat()],
    )
    test_case.assertIn("5-6 PM", slots[0].attributes.get("aria-label", ""))
    test_case.assertIn("Monday", slots[0].attributes.get("aria-label", ""))


class LateDocAssignmentServicesTests(TestCase):
    monday = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )

    def physician(self, name, role=None, *, faculty_equivalent=False):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        if role:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2024, 1, 1),
                effective_end=None,
                faculty_equivalent=faculty_equivalent,
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

    def test_create_replace_remove_and_past_correction_retain_auditable_values(self):
        first = self.faculty("First")
        replacement = self.faculty("Replacement")
        assignment = assign_late_doc(
            on_date=self.monday,
            physician=first,
            scheduler=self.scheduler,
            note="  initial choice  ",
        )
        replacement_assignment = assign_late_doc(
            on_date=self.monday,
            physician=replacement,
            scheduler=self.scheduler,
            note="Correct selection",
        )
        remove_late_doc_assignment(
            assignment=replacement_assignment,
            scheduler=self.scheduler,
            note="No longer needed",
        )
        historical = self.faculty("Historical")
        past = assign_late_doc(
            on_date=date(2024, 2, 5), physician=historical, scheduler=self.scheduler
        )

        changes = list(LateDocAssignmentChange.objects.filter(date=self.monday).order_by("pk"))
        self.assertEqual(assignment.pk, replacement_assignment.pk)
        self.assertIsNone(late_doc_assignment_for_date(self.monday))
        self.assertEqual(past.physician, historical)
        self.assertEqual([change.action for change in changes], ["assigned", "assigned", "removed"])
        self.assertIsNone(changes[0].previous_value)
        self.assertEqual(changes[0].recorded_value["physician_id"], first.pk)
        self.assertEqual(changes[0].note, "initial choice")
        self.assertEqual(changes[1].previous_value["physician_id"], first.pk)
        self.assertEqual(changes[1].recorded_value["physician_id"], replacement.pk)
        self.assertEqual(changes[2].previous_value["physician_id"], replacement.pk)
        self.assertIsNone(changes[2].recorded_value)
        self.assertIsNone(changes[2].assignment)
        self.assertTrue(all(change.changed_by == self.scheduler for change in changes))
        self.assertTrue(all(change.changed_at is not None for change in changes))

    def test_model_and_service_reject_non_monday_and_duplicate_current_assignments(self):
        faculty = self.faculty("Faculty")
        non_monday = LateDocAssignment(date=date(2025, 2, 4), physician=faculty)

        with self.assertRaisesRegex(ValidationError, "Monday"):
            non_monday.full_clean()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            LateDocAssignment.objects.create(date=date(2025, 2, 4), physician=faculty)
        assign_late_doc(on_date=self.monday, physician=faculty, scheduler=self.scheduler)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            LateDocAssignment.objects.create(date=self.monday, physician=faculty)
        with self.assertRaisesRegex(ValidationError, "Monday"):
            assign_late_doc(on_date=date(2025, 2, 4), physician=faculty, scheduler=self.scheduler)

    def test_only_effective_faculty_is_assignable_or_offered(self):
        faculty = self.faculty("Faculty")
        equivalent = self.physician(
            "Faculty-equivalent Community",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
        )
        anonymous = type("Anonymous", (), {"is_authenticated": False})()

        with self.assertRaisesRegex(ValidationError, "effective Faculty role"):
            assign_late_doc(on_date=self.monday, physician=equivalent, scheduler=self.scheduler)
        assignment = assign_late_doc(
            on_date=self.monday,
            physician=equivalent,
            scheduler=self.scheduler,
            override=True,
        )
        with self.assertRaises(PermissionDenied):
            assign_late_doc(on_date=self.monday, physician=faculty, scheduler=anonymous)
        candidates = late_doc_assignment_candidates_on(self.monday)
        self.assertEqual([candidate.physician for candidate in candidates], [faculty])
        self.assertNotIn(equivalent, [candidate.physician for candidate in candidates])
        self.assertEqual(assignment.physician, equivalent)

    def test_candidates_use_monday_pm_availability_and_allow_pm_preceptor_overlap(self):
        available = self.faculty("Available")
        backup = self.faculty("Backup", monday_pm="backup_only")
        unavailable = self.faculty("Unavailable", monday_pm="off")
        session = next(
            session
            for session in open_weekly_draft(self.monday)
            if session.date == self.monday
            and session.period_of_day == PreceptorSession.PeriodOfDay.PM
        )
        preceptor_assignment = assign_preceptor(
            session=session,
            physician=available,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        candidates = late_doc_assignment_candidates_on(self.monday)
        self.assertEqual(
            [(candidate.physician, candidate.tier) for candidate in candidates],
            [
                (available, "available_other"),
                (backup, "backup_only"),
                (unavailable, "not_available"),
            ],
        )
        self.assertTrue(candidates[2].requires_override)
        with self.assertRaisesRegex(ValidationError, "explicit override"):
            assign_late_doc(on_date=self.monday, physician=unavailable, scheduler=self.scheduler)
        unavailable_assignment = assign_late_doc(
            on_date=self.monday,
            physician=unavailable,
            scheduler=self.scheduler,
            override=True,
        )
        assignment = assign_late_doc(
            on_date=self.monday, physician=available, scheduler=self.scheduler
        )
        current = late_doc_assignment_candidates_on(self.monday).current
        self.assertEqual(preceptor_assignment.physician, assignment.physician)
        self.assertEqual(unavailable_assignment.pk, assignment.pk)
        self.assertEqual(current.assignment, assignment)
        self.assertEqual(current.availability, "available")

    def test_soft_academic_event_candidate_is_ranked_after_clean_faculty_and_creates_finding(self):
        monday = date(2027, 2, 1)
        open_weekly_draft(monday)
        clean = self.faculty("Clean Faculty")
        soft = self.faculty("Soft Academic Event Faculty")
        create_academic_event_occurrence(
            definition=AcademicEventDefinition.objects.create(
                base_name="Soft Monday event", start_academic_year=2026
            ),
            academic_year=2026,
            start_date=monday,
            end_date=monday,
            period_of_day=AcademicEvent.PeriodOfDay.PM,
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            participants=(soft,),
            scheduler=self.scheduler,
        )

        candidates = late_doc_assignment_candidates_on(monday)

        self.assertEqual(
            [(candidate.physician, candidate.tier) for candidate in candidates],
            [(clean, "available_other"), (soft, "available_soft_academic_event")],
        )
        soft_candidate = next(candidate for candidate in candidates if candidate.physician == soft)
        self.assertIn("soft academic event", soft_candidate.selection_consequence.casefold())
        self.assertIn("preference", soft_candidate.selection_consequence.casefold())
        assign_late_doc(on_date=monday, physician=soft, scheduler=self.scheduler)
        soft_findings = [
            finding
            for finding in draft_findings_for_week(monday).findings
            if finding.code == "soft_academic_event_late_doc"
        ]
        self.assertEqual(len(soft_findings), 1)
        self.assertEqual(soft_findings[0].severity, "preference")
        self.assertEqual(soft_findings[0].session.date, monday)
        self.assertEqual(soft_findings[0].physicians, (soft,))

    def test_closed_monday_retains_only_the_current_late_doc_candidate(self):
        monday = date(2027, 2, 1)
        faculty = self.faculty("Retained Faculty")
        replacement = self.faculty("Replacement Faculty")
        assignment = assign_late_doc(on_date=monday, physician=faculty, scheduler=self.scheduler)
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.CHRISTMAS_DAY),
            observed_date=monday,
            scheduler=self.scheduler,
        )

        candidates = late_doc_assignment_candidates_on(monday)

        self.assertEqual(candidates.current.assignment, assignment)
        self.assertEqual(tuple(candidates), ())
        with self.assertRaisesRegex(ValidationError, "Christmas Day closes"):
            assign_late_doc(on_date=monday, physician=replacement, scheduler=self.scheduler)


class LateDocAssignmentConcurrencyTests(TransactionTestCase):
    monday = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.physicians = []
        for name in ("First", "Second"):
            physician = create_physician(display_name=name, scheduler=self.scheduler)
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2024, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
            create_faculty_availability_template(
                physician=physician,
                effective_start=None,
                effective_end=None,
                slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
                scheduler=self.scheduler,
            )
            self.physicians.append(physician)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_creates_leave_one_current_assignment_with_retained_history(self):
        barrier = Barrier(2)
        errors = []
        error_lock = Lock()

        def assign(physician):
            close_old_connections()
            try:
                barrier.wait(timeout=1)
                assign_late_doc(on_date=self.monday, physician=physician, scheduler=self.scheduler)
            except Exception as error:
                with error_lock:
                    errors.append(error)
            finally:
                close_old_connections()

        threads = [Thread(target=assign, args=(physician,)) for physician in self.physicians]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(LateDocAssignment.objects.filter(date=self.monday).count(), 1)
        self.assertEqual(LateDocAssignmentChange.objects.filter(date=self.monday).count(), 2)


class LateDocPresentationTests(TestCase):
    monday = date(2025, 2, 3)

    def test_month_origin_survives_invalid_save_and_removal(self):
        query = "?month=2025-01"
        editor = reverse("late-doc-edit", args=(self.monday.isoformat(),)) + query
        origin = reverse("monthly-workspace") + "?month=2025-01"
        invalid = self.client.post(editor, {"physician": "invalid"})
        self.assertTrue(invalid.context["form"].errors)
        self.assertEqual(invalid.context["return_url"], origin)
        assign_late_doc(on_date=self.monday, physician=self.faculty, scheduler=self.scheduler)
        removal = reverse("late-doc-remove", args=(self.monday.isoformat(),)) + query
        invalid_removal = self.client.post(removal, {"note": "invalid\x00note"})
        self.assertTrue(invalid_removal.context["remove_form"].errors)
        self.assertEqual(invalid_removal.context["form_action"], editor)
        self.assertEqual(invalid_removal.context["return_url"], origin)
        saved = self.client.post(editor, {"physician": self.faculty.pk})
        self.assertRedirects(saved, origin, fetch_redirect_response=False)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.client.force_login(self.scheduler)
        self.faculty = create_physician(display_name="Late Faculty", scheduler=self.scheduler)
        add_role(
            physician=self.faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=self.faculty,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_calendar_and_list_present_the_separate_monday_slot(self, _now):
        assign_late_doc(on_date=self.monday, physician=self.faculty, scheduler=self.scheduler)

        listing = self.client.get(reverse("preceptor-list"), {"month": "2025-02"})
        calendar = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        late_doc_field = late_doc_list_cell(listing, self.monday)
        self.assertEqual(late_doc_field.text().strip(), "Late Faculty")
        assert_rendered_late_doc_slot(self, calendar, on_date=self.monday, physician="Late Faculty")

    def test_late_doc_editor_assigns_removes_and_shows_retained_history(self):
        edit_url = reverse("late-doc-edit", args=(self.monday.isoformat(),))

        response = self.client.post(edit_url, {"physician": self.faculty.pk, "note": "Initial"})

        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment = LateDocAssignment.objects.get(date=self.monday)
        history = self.client.get(reverse("late-doc-history", args=(self.monday.isoformat(),)))
        removed = self.client.post(
            reverse("late-doc-remove", args=(self.monday.isoformat(),)), {"note": "Corrected"}
        )

        self.assertContains(history, "Initial")
        self.assertRedirects(
            removed,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        self.assertFalse(LateDocAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(LateDocAssignmentChange.objects.filter(date=self.monday).count(), 2)

    def test_late_doc_editor_describes_the_manual_float_exception_without_relabeling_roles(self):
        editor = self.client.get(reverse("late-doc-edit", args=(self.monday.isoformat(),)))

        self.assertContains(editor, "Late Doc assignment")
        self.assertContains(editor, "normally requires an effective Faculty physician")
        self.assertContains(editor, "Available as Float physician is a manual-only exception")
        self.assertContains(editor, "Late Doc physician")
        self.assertNotContains(editor, "Faculty assignment")

    def test_late_doc_drilldowns_use_authority_aware_precepting_navigation(self):
        assignment = assign_late_doc(
            on_date=self.monday, physician=self.faculty, scheduler=self.scheduler
        )
        edit_url = reverse("late-doc-edit", args=(self.monday.isoformat(),))
        history_url = reverse("late-doc-history", args=(self.monday.isoformat(),))
        remove_url = reverse("late-doc-remove", args=(self.monday.isoformat(),))
        administrator_responses = (
            self.client.get(edit_url),
            self.client.post(remove_url, {"note": "\x00"}),
            self.client.get(history_url),
        )
        ordinary_user = get_user_model().objects.create_user(
            username="ordinary", password="test-password"
        )
        self.client.force_login(ordinary_user)
        ordinary_responses = (
            self.client.get(edit_url),
            self.client.post(remove_url, {"note": "\x00"}),
            self.client.get(history_url),
        )

        for response in administrator_responses:
            with self.subTest(authority="administrator", path=response.request["PATH_INFO"]):
                navigation = response.context["schedule_workspace_navigation"]
                self.assertEqual(
                    [item["label"] for item in navigation],
                    [
                        "Calendar",
                        "List",
                        "Exceptions",
                        "Schedule Bot",
                        "Import Schedule",
                    ],
                )
                self.assertTrue(navigation[0]["current"])
                self.assertTemplateUsed(response, "schedules/schedule_workspace_base.html")

        for response in ordinary_responses:
            with self.subTest(authority="ordinary", path=response.request["PATH_INFO"]):
                navigation = response.context["schedule_workspace_navigation"]
                self.assertEqual(
                    [item["label"] for item in navigation],
                    ["Calendar", "List", "Exceptions", "Schedule Bot"],
                )
                self.assertTrue(navigation[0]["current"])
                self.assertTemplateUsed(response, "schedules/schedule_workspace_base.html")

        self.assertTrue(LateDocAssignment.objects.filter(pk=assignment.pk).exists())

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_late_doc_editor_shows_current_availability_finding_and_source(self, _now):
        unavailable = create_physician(
            display_name="Unavailable Late Faculty", scheduler=self.scheduler
        )
        add_role(
            physician=unavailable,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        slots = {slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        slots["monday_pm"] = "off"
        template = create_faculty_availability_template(
            physician=unavailable,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        assign_late_doc(
            on_date=self.monday,
            physician=unavailable,
            scheduler=self.scheduler,
            override=True,
        )

        response = self.client.get(reverse("late-doc-edit", args=(self.monday.isoformat(),)))

        self.assertContains(response, "Current findings")
        self.assertContains(response, "Late Doc physician is not available Monday PM.")
        self.assertContains(
            response,
            reverse(
                "physician-faculty-availability-correct",
                args=(unavailable.pk, template.pk),
            ),
        )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2026, 1, 1),
    )
    def test_closed_retained_late_doc_is_removable_from_month_without_replacement(self, _today):
        monday = date(2027, 2, 1)
        replacement = create_physician(display_name="Replacement Faculty", scheduler=self.scheduler)
        add_role(
            physician=replacement,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=replacement,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        assignment = assign_late_doc(
            on_date=monday, physician=self.faculty, scheduler=self.scheduler
        )
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.CHRISTMAS_DAY),
            observed_date=monday,
            scheduler=self.scheduler,
        )
        edit_url = reverse("late-doc-edit", args=(monday.isoformat(),))

        month = self.client.get(reverse("monthly-workspace"), {"month": "2027-02"})
        cleanup = self.client.get(edit_url)

        assert_rendered_late_doc_action(
            self,
            month,
            action=f"{edit_url}?month=2027-02",
            physician="Late Faculty",
        )
        self.assertContains(cleanup, "Christmas Day closes this AM and PM clinic shift.")
        self.assertContains(cleanup, "Remove Late Doc")
        self.assertNotContains(cleanup, "Faculty assignment")
        self.assertNotContains(cleanup, 'name="physician"')

        rejected = self.client.post(edit_url, {"physician": replacement.pk})
        self.assertEqual(rejected.status_code, 400)
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, self.faculty)

        removed = self.client.post(
            reverse("late-doc-remove", args=(monday.isoformat(),)), {"note": "Holiday cleanup"}
        )
        self.assertRedirects(
            removed,
            reverse("monthly-workspace") + "?month=2027-02",
            fetch_redirect_response=False,
        )
        change = LateDocAssignmentChange.objects.get(date=monday, action="removed")
        self.assertEqual(change.note, "Holiday cleanup")
        self.assertEqual(self.client.get(edit_url).status_code, 404)
        parser = _RenderedSurfaceParser()
        parser.feed(
            self.client.get(
                reverse("monthly-workspace"), {"month": monday.strftime("%Y-%m")}
            ).content.decode()
        )
        self.assertFalse(
            any(
                element.tag == "a" and _action_matches(element.attributes.get("href", ""), edit_url)
                for element in parser.elements
            )
        )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2028, 1, 1),
    )
    def test_historical_closed_late_doc_uses_normal_editor_and_retains_correction_history(
        self, _today
    ):
        monday = date(2027, 2, 1)
        replacement = create_physician(
            display_name="Historical replacement", scheduler=self.scheduler
        )
        add_role(
            physician=replacement,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=replacement,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        assignment = assign_late_doc(
            on_date=monday, physician=self.faculty, scheduler=self.scheduler
        )
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.CHRISTMAS_DAY),
            observed_date=monday,
            scheduler=self.scheduler,
        )
        edit_url = reverse("late-doc-edit", args=(monday.isoformat(),))

        editor = self.client.get(edit_url)
        corrected = self.client.post(
            edit_url,
            {"physician": replacement.pk, "note": "Correct retained history"},
        )

        self.assertContains(editor, "Late Doc assignment")
        self.assertContains(editor, 'name="physician"')
        self.assertNotContains(editor, "can only be removed")
        self.assertRedirects(
            corrected,
            reverse("monthly-workspace") + "?month=2027-02",
            fetch_redirect_response=False,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, replacement)
        change = LateDocAssignmentChange.objects.filter(
            date=monday, action=LateDocAssignmentChange.Action.ASSIGNED
        ).latest("pk")
        self.assertEqual(change.previous_value["physician_id"], self.faculty.pk)
        self.assertEqual(change.recorded_value["physician_id"], replacement.pk)
        self.assertEqual(change.note, "Correct retained history")
