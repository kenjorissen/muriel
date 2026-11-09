"""Month edit provenance rendered independently of the recorder's policy matrix."""

from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from schedules.models import EffectiveRole, FacultyAvailabilityTemplate, Physician, SchedulePeriod
from schedules.physician_scope import SESSION_KEY
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


class MonthEditDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.scheduler = get_user_model().objects.create_user(username="edit-display")
        cls.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        cls.physician = Physician.objects.create(display_name="Display Faculty")
        EffectiveRole.objects.create(
            physician=cls.physician, role="faculty", effective_start=date(2020, 1, 1)
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=cls.physician,
            slots={
                slot: {"base_state": "open"} for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )

    def setUp(self):
        clock = patch("django.utils.timezone.now", return_value=datetime(2026, 11, 3, tzinfo=UTC))
        clock.start()
        self.addCleanup(clock.stop)
        self.client.force_login(self.scheduler)

    def display(self, month):
        response = self.client.get(reverse("monthly-workspace"), {"month": month})
        self.assertEqual(response.status_code, 200)
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        fields = [
            element
            for element in parser.elements
            if element.attributes.get("id") == "month-last-edit"
        ]
        self.assertEqual(len(fields), 1)
        return fields[0]

    def assert_recorded_display(self, field, recorded, *, month, date_parts, clock, zone):
        visible = " ".join(field.text().split())
        self.assertIn(month, visible)
        times = [element for element in field.descendants() if element.tag == "time"]
        self.assertEqual(len(times), 1)
        self.assertEqual(datetime.fromisoformat(times[0].attributes["datetime"]), recorded)
        # Check the visible local date/time too; hidden datetime metadata is insufficient.
        for part in date_parts:
            self.assertIn(part, times[0].text())
        self.assertRegex(times[0].text(), clock)
        self.assertIn(zone, visible)
        self.assertIn("America/Chicago", visible)
        self.assertNotIn("no edits yet", visible)

    def test_initial_label_is_replaced_by_the_recorded_first_edit(self):
        self.assertFalse(SchedulePeriod.objects.exists())
        field = self.display("2026-09")
        self.assertIn("September 2026", field.text())
        # TEXT-CONTRACT: precepting-month-no-edits (human-selected initial label).
        labels = [element.text() for element in field.descendants() if element.tag == "span"]
        self.assertEqual(labels, ["no edits yet"])
        self.assertIsNone(SchedulePeriod.objects.get(month=date(2026, 9, 1)).last_edited_at)

        recorded = datetime(2026, 9, 1, 3, 15, tzinfo=UTC)
        session = open_weekly_draft(date(2026, 9, 14))[0]
        with patch("django.utils.timezone.now", return_value=recorded):
            assign_preceptor(
                session=session,
                physician=self.physician,
                responsibility="primary",
                primary_position=1,
                scheduler=self.scheduler,
            )
        # A fresh client and a later request read the retained edit across the local date boundary.
        self.client = Client()
        self.client.force_login(self.scheduler)
        with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 10, tzinfo=UTC)):
            self.assert_recorded_display(
                self.display("2026-09"),
                recorded,
                month="September 2026",
                date_parts=("Aug", "31", "2026"),
                clock=r"(?:10:15\s*p\.?m\.?|22:15)",
                zone="CDT",
            )

    def test_navigation_focus_and_reload_keep_each_months_retained_value(self):
        september = datetime(2026, 9, 1, 3, 15, tzinfo=UTC)
        october = datetime(2026, 11, 2, 4, 25, tzinfo=UTC)
        for month, recorded in ((date(2026, 9, 1), september), (date(2026, 10, 1), october)):
            # Empty periods can retain edits; no current assignment is needed to show them.
            SchedulePeriod.objects.create(month=month, last_edited_at=recorded)
        session = self.client.session
        session[SESSION_KEY] = [self.physician.pk]
        session.save()
        with (
            patch("django.utils.timezone.now", return_value=datetime(2026, 11, 3, tzinfo=UTC)),
            timezone.override("Asia/Tokyo"),
        ):
            for month in ("2026-09", "2026-10", "2026-09"):
                with self.subTest(month=month):
                    if month == "2026-09":
                        expected = (
                            september,
                            "September 2026",
                            ("Aug", "31", "2026"),
                            r"(?:10:15\s*p\.?m\.?|22:15)",
                            "CDT",
                        )
                    else:
                        expected = (
                            october,
                            "October 2026",
                            ("Nov", "1", "2026"),
                            r"(?:10:25\s*p\.?m\.?|22:25)",
                            "CST",
                        )
                    recorded, label, parts, clock, zone = expected
                    self.assert_recorded_display(
                        self.display(month),
                        recorded,
                        month=label,
                        date_parts=parts,
                        clock=clock,
                        zone=zone,
                    )
        self.assertEqual(
            SchedulePeriod.objects.get(month=date(2026, 9, 1)).last_edited_at, september
        )
        self.assertEqual(
            SchedulePeriod.objects.get(month=date(2026, 10, 1)).last_edited_at, october
        )
