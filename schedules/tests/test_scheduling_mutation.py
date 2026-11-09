from datetime import UTC, date, datetime
from threading import Event, RLock, Thread, current_thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

import schedules.services._mutation as _mutation
from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    PreceptorAssignment,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    assign_preceptor,
    open_weekly_draft,
    remove_preceptor_assignment,
    set_preceptor_assignment_lock,
)
from schedules.services.rounding import record_normal_rounder


class RetiredWeeklyPublicationViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def test_workspace_has_no_publication_action_or_endpoint(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        self.assertNotContains(response, "Publish week")
        self.assertNotContains(response, "Publication history")
        self.assertEqual(self.client.post("/week/publish/").status_code, 404)
        for path in (
            "/publications/?month=2025-02",
            "/week/revisions/?date=2025-02-03",
            "/week/revisions/00000000-0000-0000-0000-000000000000/",
            "/week/revisions/00000000-0000-0000-0000-000000000000/pdf/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_operational_hard_findings_remain_visible_without_a_publication_control(self, _now):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        self.assertTrue(any(f.severity == "hard" for f in response.context["findings"]))
        self.assertNotContains(response, "Publish week")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
    )
    def test_current_week_treats_an_elapsed_weekday_hard_finding_as_historical(self, _now):
        week = date(2025, 2, 3)
        physicians = []
        for name in ("First", "Second", "Rounder"):
            physician = create_physician(display_name=name, scheduler=self.user)
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.user,
            )
            create_faculty_availability_template(
                physician=physician,
                effective_start=None,
                effective_end=None,
                slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
                scheduler=self.user,
            )
            physicians.append(physician)
        record_normal_rounder(week=week, physician=physicians[2], scheduler=self.user)
        for session in open_weekly_draft(week):
            for position, physician in enumerate(physicians[:2], start=1):
                assign_preceptor(
                    session=session,
                    physician=physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=position,
                    scheduler=self.user,
                )
        monday_assignment = PreceptorAssignment.objects.get(
            session__date=week,
            session__period_of_day="AM",
            physician=physicians[0],
        )
        remove_preceptor_assignment(assignment=monday_assignment, scheduler=self.user)

        response = self.client.get(reverse("monthly-workspace"), {"month": week.strftime("%Y-%m")})

        self.assertFalse(
            any(f.session and f.session.date == week for f in response.context["findings"])
        )
        self.assertEqual(response.context["week_findings"][week], ())

        self.assertNotContains(response, "Publish week")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
    )
    def test_current_week_keeps_future_hard_findings_as_work(self, _now):
        week = date(2025, 2, 3)
        response = self.client.get(reverse("monthly-workspace"), {"month": week.strftime("%Y-%m")})

        self.assertTrue(any(f.severity == "hard" for f in response.context["findings"]))
        self.assertGreater(response.context["work_remaining"].required_item_count, 0)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_operational_preference_findings_remain_advisory(self, _now):
        # An otherwise empty week has hard findings; replacing them with a preference isolates the control rule.
        with patch(
            "schedules.views.preceptors.projected_draft_findings_for_weeks",
            return_value=DraftFindingResult(
                (
                    DraftFinding(
                        "backup_only_assignment",
                        "preference",
                        None,
                        date(2025, 2, 3),
                        (),
                        {},
                    ),
                )
            ),
        ):
            response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        self.assertTrue(response.context["findings"])
        self.assertTrue(all(f.severity == "preference" for f in response.context["findings"]))
        self.assertNotContains(response, "Publish week")

    def test_week_workspace_route_is_retired_without_initializing_sessions(self):
        from django.urls import NoReverseMatch

        from schedules.models import PreceptorSession, SchedulePeriod

        with self.assertRaises(NoReverseMatch):
            reverse("weekly-workspace")
        for client_authenticated in (True, False):
            if not client_authenticated:
                self.client.logout()
            self.assertEqual(self.client.get("/week/?date=2025-02-03").status_code, 404)
            self.assertEqual(self.client.post("/week/", {"date": "2025-02-03"}).status_code, 404)
        self.assertFalse(PreceptorSession.objects.exists())
        self.assertFalse(SchedulePeriod.objects.exists())


class SchedulingSerializationTests(TransactionTestCase):
    week = date(2025, 2, 3)

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
        physicians = []
        for name in ("First", "Second", "Rounder"):
            physician = create_physician(display_name=name, scheduler=self.scheduler)
            add_role(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 1),
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
            physicians.append(physician)
        record_normal_rounder(week=self.week, physician=physicians[2], scheduler=self.scheduler)
        for session in open_weekly_draft(self.week):
            for position, physician in enumerate(physicians[:2], start=1):
                assign_preceptor(
                    session=session,
                    physician=physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=position,
                    scheduler=self.scheduler,
                )
        self.assignment = PreceptorAssignment.objects.get(
            session__date=self.week,
            session__period_of_day="AM",
            physician=physicians[0],
        )

    def test_concurrent_lock_mutation_waits_for_coherent_scheduling_read(self):
        mutation_waits_for_authority = Event()
        mutation_finished = Event()
        errors = []

        def change_lock():
            close_old_connections()
            try:
                set_preceptor_assignment_lock(
                    assignment=self.assignment,
                    is_locked=True,
                    scheduler=self.scheduler,
                )
            except Exception as error:
                errors.append(error)
            finally:
                mutation_finished.set()
                close_old_connections()

        worker = Thread(target=change_lock)

        class TrackingLock:
            def __init__(self):
                self.lock = RLock()

            def __enter__(self):
                if current_thread() is worker:
                    mutation_waits_for_authority.set()
                self.lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.lock.release()

        with patch.object(_mutation, "_scheduling_mutation_lock", TrackingLock()):
            try:
                with _mutation.serialized_scheduling_state():
                    worker.start()
                    self.assertTrue(mutation_waits_for_authority.wait(5))
                    self.assertFalse(mutation_finished.is_set())
                    self.assignment.refresh_from_db()
                    self.assertFalse(self.assignment.is_locked)
            finally:
                worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.is_locked)
        self.assignment.session.period.refresh_from_db()
        self.assertIsNotNone(self.assignment.session.period.last_edited_at)
