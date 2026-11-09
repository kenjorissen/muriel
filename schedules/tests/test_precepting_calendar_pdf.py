from datetime import UTC, date, datetime, timedelta
from email.message import Message
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from schedules.models import EffectiveRole, PreceptorAssignment, PreceptorSession, SchedulePeriod
from schedules.pdf import (
    build_precepting_calendar_pdf,
)
from schedules.physician_scope import SESSION_KEY
from schedules.services.late_docs import assign_late_doc
from schedules.services.physicians import add_role, create_physician
from schedules.services.preceptors import assign_preceptor, open_monthly_draft, open_weekly_draft
from schedules.views.preceptors import _precepting_calendar_pdf_weeks


class PreceptingCalendarPdfTests(TestCase):
    edge_monday = date(2024, 12, 30)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.scheduler)

    def physician(self, name):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        return physician

    def pdf_text(self, response):
        return " ".join(
            " ".join(page.extract_text().split())
            for page in PdfReader(BytesIO(response.content)).pages
        )

    def test_retained_month_edit_is_local_and_independent_of_scope_and_generation(self):
        focused = self.physician("Focused Faculty")
        september = datetime(2026, 9, 1, 3, 15, tzinfo=UTC)
        october = datetime(2026, 11, 2, 4, 25, tzinfo=UTC)
        # Empty periods retain their provenance after assignments/findings are gone.
        for month, recorded in ((date(2026, 9, 1), september), (date(2026, 10, 1), october)):
            SchedulePeriod.objects.create(month=month, last_edited_at=recorded)
        for month, scope, generated, date_label, clock, zone in (
            ("2026-09", [], datetime(2026, 11, 3, 12, tzinfo=UTC), "2026-08-31", "22:15", "CDT"),
            (
                "2026-10",
                [focused.pk],
                datetime(2026, 11, 4, 12, tzinfo=UTC),
                "2026-11-01",
                "22:25",
                "CST",
            ),
            (
                "2026-09",
                [focused.pk],
                datetime(2026, 12, 5, 12, tzinfo=UTC),
                "2026-08-31",
                "22:15",
                "CDT",
            ),
        ):
            with (
                self.subTest(month=month, generated=generated),
                patch("django.utils.timezone.now", return_value=generated),
                timezone.override("Asia/Tokyo"),
            ):
                self.client.force_login(self.scheduler)
                response = self.client.get(
                    reverse("precepting-calendar-pdf"), {"month": month, "physicians": scope}
                )
                self.assertEqual(response.status_code, 200)
                text = self.pdf_text(response)
                edit_label = f"Last edit: {date_label} {clock} {zone} (America/Chicago)"
                _, locations, _, _ = _pdf_artifact(response.content, {edit_label})
                label = _single_location(locations, edit_label)
                self.assertTrue(_text_bounds_are_within_page(label["bounds"]))
                self.assertIn(f"Generated {generated:%Y-%m-%d}", text)
                self.assertNotIn("no edits yet", text)
        self.assertEqual(
            SchedulePeriod.objects.get(month=date(2026, 9, 1)).last_edited_at, september
        )
        self.assertEqual(
            SchedulePeriod.objects.get(month=date(2026, 10, 1)).last_edited_at, october
        )

    def test_unedited_month_pdf_uses_the_initial_label_without_recording_an_edit(self):
        # An adjacent month's edit must not become this month's provenance.
        SchedulePeriod.objects.create(
            month=date(2026, 8, 1), last_edited_at=datetime(2026, 8, 31, 12, tzinfo=UTC)
        )
        for initialized in (False, True):
            with self.subTest(initialized=initialized):
                if initialized:
                    SchedulePeriod.objects.create(month=date(2026, 9, 1))
                response = self.client.get(reverse("precepting-calendar-pdf"), {"month": "2026-09"})
                self.assertEqual(response.status_code, 200)
                # TEXT-CONTRACT: precepting-month-no-edits (human-selected initial label).
                _, locations, _, _ = _pdf_artifact(response.content, {"Last edit: no edits yet"})
                label = _single_location(locations, "Last edit: no edits yet")
                self.assertTrue(_text_bounds_are_within_page(label["bounds"]))
                self.assertFalse(
                    SchedulePeriod.objects.filter(
                        month=date(2026, 9, 1), last_edited_at__isnull=False
                    ).exists()
                )

    def _assert_date_is_under_weekday(self, content, date_label, weekday):
        _, locations, _, grid_lines = _pdf_artifact(content, {date_label, weekday})
        date_location = _single_location(locations, date_label)
        date_cell = _grid_cell_at(grid_lines, date_location)
        weekday_locations = [
            location
            for location in locations[weekday]
            if location["page"] == date_location["page"]
            and date_cell["left"] < location["x"] < date_cell["right"]
            and location["y"] > date_location["y"]
        ]

        self.assertTrue(weekday_locations, f"{date_label} is not beneath its {weekday} heading")

    def _assert_complete_weekday_heading_row(self, locations, grid_lines, page_count):
        for page_number in range(page_count):
            header_locations = []
            for weekday in _PRECEPTING_WEEKDAYS:
                weekday_locations = [
                    location for location in locations[weekday] if location["page"] == page_number
                ]
                self.assertEqual(
                    len(weekday_locations),
                    1,
                    f"Expected one {weekday} heading on page {page_number + 1}",
                )
                header_locations.extend(weekday_locations)

            header_cells = [_grid_cell_at(grid_lines, location) for location in header_locations]
            self.assertEqual(
                {(cell["bottom"], cell["top"]) for cell in header_cells},
                {(header_cells[0]["bottom"], header_cells[0]["top"])},
                f"Weekday headings do not share a row on page {page_number + 1}",
            )
            self.assertEqual(
                [(cell["left"], cell["right"]) for cell in header_cells],
                sorted((cell["left"], cell["right"]) for cell in header_cells),
                f"Weekday headings are not ordered Monday through Friday on page {page_number + 1}",
            )

    def test_exports_scoped_current_calendar_with_edge_dates_and_positions_without_mutation(self):
        focused = self.physician("李雷")
        outside = self.physician("Outside")
        secondary = self.physician("Secondary outside scope")
        sessions = open_weekly_draft(self.edge_monday)
        am = next(
            session
            for session in sessions
            if session.date == self.edge_monday
            and session.period_of_day == PreceptorSession.PeriodOfDay.AM
        )
        pm = next(
            session
            for session in sessions
            if session.date == self.edge_monday
            and session.period_of_day == PreceptorSession.PeriodOfDay.PM
        )
        for session, physician, position in (
            (am, outside, 1),
            (am, focused, 2),
            (pm, focused, 1),
        ):
            assign_preceptor(
                session=session,
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=position,
                scheduler=self.scheduler,
            )
        assign_preceptor(
            session=am,
            physician=secondary,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )
        assign_late_doc(
            on_date=self.edge_monday,
            physician=outside,
            scheduler=self.scheduler,
            override=True,
        )
        client_session = self.client.session
        client_session[SESSION_KEY] = [focused.pk]
        client_session.save()

        screen = self.client.get(reverse("monthly-workspace"), {"month": "2025-01"})
        before = (
            SchedulePeriod.objects.count(),
            PreceptorSession.objects.count(),
            PreceptorAssignment.objects.count(),
        )
        response = self.client.get(
            reverse("precepting-calendar-pdf"),
            {"month": "2025-01", "physicians": [focused.pk]},
        )
        text = self.pdf_text(response)
        reader = PdfReader(BytesIO(response.content))

        self.assertContains(
            screen,
            f'href="/pdf/?month=2025-01&amp;physicians={focused.pk}">PDF</a>',
        )
        self.assertEqual(response["Content-Type"], "application/pdf")
        filename = _attachment_filename(response["Content-Disposition"])
        self.assertTrue(filename.lower().endswith(".pdf"))
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertIn("Precepting - January, 2025", text)
        self.assertIn("January, 2025", text)
        self.assertIn("Generated", text)
        self.assertIn("Dec 30", text)
        self.assertIn("Jan 3", text)
        self.assertIn("AM", text)
        self.assertIn("PM", text)
        self.assertIn("Occupied 李雷 Occupied", text)
        self.assertIn("Late Doc", text)
        for removed_label in (
            "Preceptor 1",
            "Preceptor 2",
            "Secondary",
            "Monday Late Doc",
            "Late Doc:",
            "Publication:",
            "Not published",
            "Week of",
        ):
            self.assertNotIn(removed_label, text)
        self.assertEqual(
            (
                SchedulePeriod.objects.count(),
                PreceptorSession.objects.count(),
                PreceptorAssignment.objects.count(),
            ),
            before,
        )
        self.assertTrue(reader.pages)
        self.assertTrue(
            all(page.mediabox.width == 792 and page.mediabox.height == 612 for page in reader.pages)
        )

    def test_shows_closed_slots_without_closure_prose_and_keeps_the_pdf_endpoint_login_protected(
        self,
    ):
        open_monthly_draft(date(2026, 12, 1))

        response = self.client.get(reverse("precepting-calendar-pdf"), {"month": "2026-12"})

        text = self.pdf_text(response)
        self.assertNotIn("Clinic closed:", text)
        self.assertNotIn("Christmas Day", text)
        self.assertIn("Closed", text)
        self.client.logout()
        unauthenticated = self.client.get(reverse("precepting-calendar-pdf"), {"month": "2026-12"})
        self.assertEqual(unauthenticated.status_code, 302)

    def test_month_grid_renders_closed_and_empty_assignment_slots_with_blank_weekday_late_docs(
        self,
    ):
        open_monthly_draft(date(2026, 12, 1))

        weeks = _precepting_calendar_pdf_weeks(date(2026, 12, 1), ())
        christmas = _calendar_day(weeks, date(2026, 12, 25))
        ordinary_day = _calendar_day(weeks, date(2026, 12, 23))
        monday = _calendar_day(weeks, date(2026, 12, 21))

        self.assertEqual(
            christmas["am"],
            (("Preceptor 1", "Closed"), ("Preceptor 2", "Closed"), ("Secondary", "Closed")),
        )
        self.assertEqual(
            christmas["pm"],
            (("Preceptor 1", "Closed"), ("Preceptor 2", "Closed"), ("Secondary", "Closed")),
        )
        # TEXT-CONTRACT: precepting-calendar-secondary-empty-marker
        # The calendar uses "-" for an actual empty optional Secondary, distinct
        # from a required Unfilled slot; scope: one hyphen, no surrounding whitespace.
        # This independent expected literal protects the rendered assignment-slot
        # contract owned by schedules/views/preceptors.py.
        self.assertEqual(
            ordinary_day["am"],
            (("Preceptor 1", "Unfilled"), ("Preceptor 2", "Unfilled"), ("Secondary", "-")),
        )
        self.assertEqual(
            ordinary_day["pm"],
            (("Preceptor 1", "Unfilled"), ("Preceptor 2", "Unfilled"), ("Secondary", "-")),
        )
        self.assertEqual(monday["late_doc"], (("Late Doc", "Unfilled"),))
        self.assertEqual(ordinary_day["late_doc"], ())
        self.assertTrue(christmas["has_holiday_closure"])
        self.assertFalse(ordinary_day["has_holiday_closure"])

        content = build_precepting_calendar_pdf(
            date(2026, 12, 1), weeks, captured_at=datetime(2026, 12, 1, 9, 30)
        )
        reader, locations, _, grid_lines = _pdf_artifact(
            content,
            {
                "Dec 21",
                "Dec 22",
                "Dec 23",
                "Dec 24",
                "Dec 25",
                "Dec 28",
                "Friday",
                "Closed",
                "Unfilled",
                "-",
                *_PRECEPTING_LANES,
            },
        )
        self.assertTrue(reader.pages)
        self._assert_date_is_under_weekday(content, "Dec 25", "Friday")
        christmas_cells = _day_lane_cells(locations, grid_lines, "Dec 25", "Dec 28")
        tuesday_cells = _day_lane_cells(locations, grid_lines, "Dec 22", "Dec 28")
        wednesday_cells = _day_lane_cells(locations, grid_lines, "Dec 23", "Dec 28")
        thursday_cells = _day_lane_cells(locations, grid_lines, "Dec 24", "Dec 28")
        friday_cells = _day_lane_cells(locations, grid_lines, "Dec 25", "Dec 28")
        monday_cells = _day_lane_cells(locations, grid_lines, "Dec 21", "Dec 28")

        for lane in ("AM", "PM"):
            self.assertCountEqual(
                _text_fragments_in_cell(locations, christmas_cells[lane]),
                ("Closed", "Closed", "Closed"),
            )
        for cells in (tuesday_cells, wednesday_cells):
            for lane in ("AM", "PM"):
                self.assertCountEqual(
                    _text_fragments_in_cell(locations, cells[lane]),
                    ("Unfilled", "Unfilled", "-"),
                )
        self.assertCountEqual(
            _text_fragments_in_cell(locations, thursday_cells["AM"]),
            ("Unfilled", "Unfilled", "-"),
        )
        self.assertCountEqual(
            _text_fragments_in_cell(locations, thursday_cells["PM"]),
            ("Closed", "Closed", "Closed"),
        )
        self.assertCountEqual(
            _text_fragments_in_cell(locations, monday_cells["Late Doc"]), ("Unfilled",)
        )
        for cells in (tuesday_cells, wednesday_cells, thursday_cells, friday_cells):
            self.assertEqual(_text_fragments_in_cell(locations, cells["Late Doc"]), [])

    def test_rejects_malformed_month_like_the_calendar(self):
        response = self.client.get(reverse("precepting-calendar-pdf"), {"month": "January"})

        self.assertEqual(response.status_code, 400)

    def test_rendered_month_grid_has_readable_equal_day_lanes_and_cohesive_weeks(self):
        weeks = _representative_calendar_weeks(date(2025, 7, 28), 6)
        content = build_precepting_calendar_pdf(
            date(2025, 8, 1), weeks, captured_at=datetime(2025, 8, 1, 9, 30)
        )
        reader = PdfReader(BytesIO(content))

        self.assertEqual(len(reader.pages), 1)
        self.assertEqual(reader.pages[0].mediabox.width, 792)
        self.assertEqual(reader.pages[0].mediabox.height, 612)
        _, locations, _, grid_lines = _pdf_artifact(
            content,
            {
                "Jul 28",
                "Jul 29",
                "Jul 30",
                "Jul 31",
                "Aug 1",
                "Aug 4",
                "Aug 5",
                "Aug 6",
                "Aug 7",
                "Aug 8",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Late Doc",
            },
        )
        weekday_columns = {
            weekday: _grid_cell_at(grid_lines, _single_location(locations, date_label))
            for weekday, date_label in zip(
                ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
                ("Jul 28", "Jul 29", "Jul 30", "Jul 31", "Aug 1"),
                strict=True,
            )
        }
        self._assert_complete_weekday_heading_row(locations, grid_lines, len(reader.pages))
        day_widths = [column["right"] - column["left"] for column in weekday_columns.values()]
        for width in day_widths[1:]:
            self.assertAlmostEqual(width, day_widths[0], places=3)
        for date_label, weekday in zip(
            ("Aug 4", "Aug 5", "Aug 6", "Aug 7", "Aug 8"),
            weekday_columns,
            strict=True,
        ):
            self._assert_date_is_under_weekday(content, date_label, weekday)
        late_doc_locations = locations["Late Doc"]
        self.assertEqual(len(late_doc_locations), 6)
        for location in late_doc_locations:
            lane_cell = _grid_cell_at(grid_lines, location)
            self.assertTrue(_horizontal_bounds_are_within(location["bounds"], lane_cell))
            self.assertTrue(_text_bounds_are_within_page(location["bounds"]))
        rendered_text = " ".join(page.extract_text() for page in reader.pages)
        self.assertIn("Avery Chen W1D1", rendered_text)
        self.assertIn("Morgan Patel W4D3", rendered_text)

    def test_uses_concise_month_title_and_subtitle_without_a_repeated_month(self):
        content = build_precepting_calendar_pdf(
            date(2026, 9, 1),
            _representative_calendar_weeks(date(2026, 8, 31), 5),
            captured_at=datetime(2026, 9, 1, 9, 30),
        )
        reader = PdfReader(BytesIO(content))
        text = " ".join(page.extract_text() for page in reader.pages)

        # TEXT-CONTRACT: precepting-calendar-month-title
        self.assertEqual(reader.metadata.title, "Precepting - September, 2026")
        self.assertIn("Precepting - September, 2026", text)
        self.assertEqual(text.count("September, 2026"), 1)
        self.assertIn("Current schedule state", text)
        self.assertIn("Generated 2026-09-01 09:30", text)

    def test_representative_four_five_and_six_week_month_grids_fit_one_landscape_page(self):
        for month, first_week, week_count, first_date, final_date in (
            (date(2021, 2, 1), date(2021, 2, 1), 4, "Feb 1", "Feb 26"),
            (date(2026, 9, 1), date(2026, 8, 31), 5, "Aug 31", "Oct 2"),
            (date(2025, 8, 1), date(2025, 7, 28), 6, "Jul 28", "Sep 5"),
        ):
            with self.subTest(month=month, week_count=week_count):
                content = build_precepting_calendar_pdf(
                    month,
                    _representative_calendar_weeks(first_week, week_count),
                    captured_at=datetime.combine(month, datetime.min.time()).replace(
                        hour=9, minute=30
                    ),
                    last_edited_at=datetime(2021, 2, 1, 12, 30, tzinfo=UTC),
                )
                reader = PdfReader(BytesIO(content))
                _, locations, _, grid_lines = _pdf_artifact(content, set(_PRECEPTING_WEEKDAYS))

                self.assertEqual(len(reader.pages), 1)
                self._assert_complete_weekday_heading_row(locations, grid_lines, len(reader.pages))
                text = " ".join(page.extract_text() for page in reader.pages)
                self.assertIn(first_date, text)
                self.assertIn(final_date, text)
                self.assertIn("Avery Chen W1D1", text)
                self.assertIn(f"Morgan Patel W{week_count}D5", text)
                self.assertEqual(reader.pages[0].mediabox.width, 792)
                self.assertEqual(reader.pages[0].mediabox.height, 612)

    def test_emitted_month_grid_preserves_adjacent_and_holiday_cell_shading(self):
        weeks = _shaded_calendar_weeks()
        content = build_precepting_calendar_pdf(
            date(2026, 12, 1), weeks, captured_at=datetime(2026, 12, 1, 9, 30)
        )
        _, locations, rectangles, grid_lines = _pdf_artifact(
            content,
            {
                "Nov 30",
                "Dec 1",
                "Dec 21",
                "Dec 25",
                "Dec 28",
                "Jan 1",
                "Jan 4",
                *_PRECEPTING_LANES,
            },
        )
        adjacent_holiday_cells = _painted_day_lane_cells(
            _day_lane_cells(locations, grid_lines, "Nov 30", "Dec 21"), rectangles
        )
        holiday_cells = _painted_day_lane_cells(
            _day_lane_cells(locations, grid_lines, "Dec 25", "Dec 28"), rectangles
        )
        ordinary_adjacent_cells = _painted_day_lane_cells(
            _day_lane_cells(locations, grid_lines, "Jan 1", "Jan 4"), rectangles
        )
        selected_date_cell = _painted_rectangle_for_cell(
            rectangles, _grid_cell_at(grid_lines, _single_location(locations, "Dec 1"))
        )
        adjacent_date_cell = _painted_rectangle_for_cell(
            rectangles, _grid_cell_at(grid_lines, _single_location(locations, "Nov 30"))
        )
        ordinary_adjacent_date_cell = _painted_rectangle_for_cell(
            rectangles, _grid_cell_at(grid_lines, _single_location(locations, "Jan 1"))
        )
        holiday_date_cell = _painted_rectangle_for_cell(
            rectangles, _grid_cell_at(grid_lines, _single_location(locations, "Dec 25"))
        )

        self.assertNotEqual(adjacent_date_cell["fill_color"], selected_date_cell["fill_color"])
        self.assertEqual(
            adjacent_date_cell["fill_color"], ordinary_adjacent_date_cell["fill_color"]
        )
        self.assertNotEqual(
            locations["Nov 30"][0]["text_color"], locations["Dec 1"][0]["text_color"]
        )
        self.assertEqual(holiday_date_cell["fill_color"], selected_date_cell["fill_color"])
        for lane in _PRECEPTING_LANES:
            self.assertEqual(holiday_cells[lane]["fill_color"], holiday_cells["AM"]["fill_color"])
            self.assertEqual(
                adjacent_holiday_cells[lane]["fill_color"], holiday_cells[lane]["fill_color"]
            )
            self.assertEqual(
                ordinary_adjacent_cells[lane]["fill_color"],
                ordinary_adjacent_date_cell["fill_color"],
            )
        self.assertNotEqual(
            adjacent_holiday_cells["AM"]["fill_color"], adjacent_date_cell["fill_color"]
        )

    def test_exceptional_content_repeats_headings_and_keeps_each_week_day_and_slot_together(self):
        weeks, records = _exceptional_calendar_weeks(date(2025, 7, 28), 6)
        content = build_precepting_calendar_pdf(
            date(2025, 8, 1), weeks, captured_at=datetime(2025, 8, 1, 9, 30)
        )
        reader, locations, _, grid_lines = _pdf_artifact(
            content,
            {
                *_PRECEPTING_WEEKDAYS,
                *_PRECEPTING_LANES,
                *(record["date_label"] for record in records),
            },
            fragments={record["marker"] for record in records},
        )

        self.assertGreater(len(reader.pages), 1)
        self._assert_complete_weekday_heading_row(locations, grid_lines, len(reader.pages))
        for page in reader.pages:
            self.assertEqual(page.mediabox.width, 792)
            self.assertEqual(page.mediabox.height, 612)
        for record in records:
            record["location"] = _single_location(locations, record["marker"])
        for week_index in range(1, 7):
            week_records = [record for record in records if record["week"] == week_index]
            self.assertEqual(
                {record["location"]["page"] for record in week_records},
                {locations[week_records[0]["date_label"]][0]["page"]},
            )
        for record in records:
            location = record["location"]
            date_cell = _grid_cell_at(grid_lines, _single_location(locations, record["date_label"]))
            content_cell = _grid_cell_at(grid_lines, location)
            self.assertEqual(location["page"], date_cell["page"])
            self.assertEqual(
                (content_cell["left"], content_cell["right"]),
                (date_cell["left"], date_cell["right"]),
            )
            self.assertTrue(_horizontal_bounds_are_within(location["bounds"], content_cell))
            self.assertTrue(_text_bounds_are_within_page(location["bounds"]))
            matching_lane = [
                lane
                for lane in _PRECEPTING_LANES
                if any(
                    _same_row(content_cell, _grid_cell_at(grid_lines, lane_location))
                    for lane_location in locations[lane]
                    if lane_location["page"] == location["page"]
                )
            ]
            self.assertEqual(matching_lane, [record["lane"]])


def _representative_calendar_weeks(first_week, count):
    return tuple(
        {
            "days": tuple(
                {
                    "date": first_week + timedelta(weeks=week_index, days=day_offset),
                    "am": (
                        ("Preceptor 1", f"Avery Chen W{week_index + 1}D{day_offset + 1}"),
                        ("Preceptor 2", f"Morgan Patel W{week_index + 1}D{day_offset + 1}"),
                    ),
                    "pm": (
                        ("Preceptor 1", f"Morgan Patel W{week_index + 1}D{day_offset + 1}"),
                        ("Preceptor 2", f"Avery Chen W{week_index + 1}D{day_offset + 1}"),
                    ),
                    "late_doc": (
                        (("Late Doc", f"Morgan Patel W{week_index + 1}D1"),)
                        if day_offset == 0
                        else ()
                    ),
                }
                for day_offset in range(5)
            )
        }
        for week_index in range(count)
    )


_PRECEPTING_LANES = ("AM", "PM", "Late Doc")
_PRECEPTING_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def _shaded_calendar_weeks():
    return tuple(
        {
            "days": tuple(
                {
                    "date": first_day + timedelta(days=day_offset),
                    "am": (("Preceptor 1", "Unfilled"),),
                    "pm": (("Preceptor 1", "Unfilled"),),
                    "late_doc": (("Late Doc", "Unfilled"),) if day_offset == 0 else (),
                    "has_holiday_closure": first_day + timedelta(days=day_offset)
                    in {date(2026, 11, 30), date(2026, 12, 25)},
                }
                for day_offset in range(5)
            )
        }
        for first_day in (
            date(2026, 11, 30),
            date(2026, 12, 21),
            date(2026, 12, 28),
            date(2027, 1, 4),
        )
    )


def _exceptional_calendar_weeks(first_week, count):
    """Build ASCII-only wrapped fixture content with distinct day and lane markers."""
    records = []
    weeks = []
    for week_index in range(1, count + 1):
        days = []
        for day_offset in range(5):
            on_date = first_week + timedelta(weeks=week_index - 1, days=day_offset)
            date_label = f"{on_date:%b} {on_date.day}"
            day = {"date": on_date}
            for lane, key in (("AM", "am"), ("PM", "pm"), ("Late Doc", "late_doc")):
                if lane == "Late Doc" and day_offset:
                    day[key] = ()
                    continue
                assignment_count = 3 + ((week_index + day_offset + len(lane)) % 2)
                assignments = []
                for assignment_index in range(1, assignment_count + 1):
                    marker = f"W{week_index}D{day_offset + 1}{lane[0]}{assignment_index}"
                    assignments.append(
                        (
                            "Fixture",
                            marker,
                        )
                    )
                    if assignment_index == 1:
                        records.append(
                            {
                                "week": week_index,
                                "date_label": date_label,
                                "lane": lane,
                                "marker": marker,
                            }
                        )
                day[key] = tuple(assignments)
            days.append(day)
        weeks.append({"days": tuple(days)})
    return tuple(weeks), records


def _calendar_day(weeks, on_date):
    return next(day for week in weeks for day in week["days"] if day["date"] == on_date)


def _attachment_filename(content_disposition):
    header = Message()
    header["Content-Disposition"] = content_disposition
    if header.get_content_disposition() != "attachment":
        raise AssertionError(f"Expected attachment disposition, got {content_disposition!r}")
    filename = header.get_filename()
    if not filename:
        raise AssertionError(f"Expected an attachment filename, got {content_disposition!r}")
    return filename


def _pdf_artifact(content, labels, *, fragments=()):
    """Read emitted ASCII text locations and painted rectangles, not ReportLab tables.

    The font-bounds adapter deliberately supports only this file's ASCII fixtures.
    Unicode names are covered by the endpoint extraction test above.
    """
    reader = PdfReader(BytesIO(content))
    locations = {label: [] for label in labels}
    locations.update({fragment: [] for fragment in fragments})
    locations["_all_text_fragments"] = []
    rectangles = []
    grid_lines = []
    for page_number, page in enumerate(reader.pages):
        emitted_text = []
        emitted_text_matrix = [(1, 0, 0, 1, 0, 0)]
        emitted_font_size = [None]
        emitted_leading = [0]
        fill_state = [None]
        fill_colors = []
        pending_rectangles = []
        path_start = [None]
        pending_lines = []
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)

        def transform(matrix, x, y):
            return (
                matrix[0] * x + matrix[2] * y + matrix[4],
                matrix[1] * x + matrix[3] * y + matrix[5],
            )

        def capture_operation(
            operator,
            operands,
            current_matrix,
            text_matrix,
            page_number=page_number,
            fill_state=fill_state,
            fill_colors=fill_colors,
            pending_rectangles=pending_rectangles,
            path_start=path_start,
            pending_lines=pending_lines,
            emitted_text=emitted_text,
            emitted_text_matrix=emitted_text_matrix,
            emitted_font_size=emitted_font_size,
            emitted_leading=emitted_leading,
        ):
            if operator == b"Tm" and len(operands) == 6:
                emitted_text_matrix[0] = tuple(float(part) for part in operands)
            elif operator == b"TL" and len(operands) == 1:
                emitted_leading[0] = float(operands[0])
            elif operator == b"Tf" and len(operands) == 2:
                emitted_font_size[0] = float(operands[1])
            elif operator == b"T*":
                a, b, c, d, e, f = emitted_text_matrix[0]
                emitted_text_matrix[0] = (
                    a,
                    b,
                    c,
                    d,
                    e - (c * emitted_leading[0]),
                    f - (d * emitted_leading[0]),
                )
            elif operator == b"Tj" and len(operands) == 1:
                try:
                    text = bytes(operands[0]).decode("ascii")
                except UnicodeDecodeError:
                    pass
                else:
                    normalized = " ".join(text.split())
                    if normalized:
                        emitted_text.append(
                            {
                                "text": normalized,
                                "matrix": emitted_text_matrix[0],
                                "font_size": emitted_font_size[0],
                            }
                        )
            elif operator == b"q":
                fill_colors.append(fill_state[0])
            elif operator == b"Q":
                fill_state[0] = fill_colors.pop()
            elif operator == b"rg":
                fill_state[0] = tuple(float(part) for part in operands)
            elif operator == b"g":
                fill_state[0] = (float(operands[0]),) * 3
            elif operator == b"re" and len(operands) == 4:
                x, y, width, height = (float(part) for part in operands)
                corners = tuple(
                    transform(current_matrix, corner_x, corner_y)
                    for corner_x, corner_y in (
                        (x, y),
                        (x + width, y),
                        (x, y + height),
                        (x + width, y + height),
                    )
                )
                pending_rectangles.append(
                    {
                        "page": page_number,
                        "left": min(point[0] for point in corners),
                        "right": max(point[0] for point in corners),
                        "bottom": min(point[1] for point in corners),
                        "top": max(point[1] for point in corners),
                    }
                )
            elif operator == b"m" and len(operands) == 2:
                path_start[0] = transform(current_matrix, float(operands[0]), float(operands[1]))
            elif operator == b"l" and len(operands) == 2 and path_start[0] is not None:
                line_end = transform(current_matrix, float(operands[0]), float(operands[1]))
                pending_lines.append(
                    {
                        "page": page_number,
                        "x1": path_start[0][0],
                        "y1": path_start[0][1],
                        "x2": line_end[0],
                        "y2": line_end[1],
                    }
                )
                path_start[0] = line_end
            elif operator in {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}:
                rectangles.extend(
                    {**rectangle, "fill_color": fill_state[0]} for rectangle in pending_rectangles
                )
                if operator in {b"B", b"B*", b"b", b"b*"}:
                    grid_lines.extend(pending_lines)
                pending_rectangles.clear()
                pending_lines.clear()
                path_start[0] = None
            elif operator in {b"S", b"s"}:
                grid_lines.extend(pending_lines)
                pending_rectangles.clear()
                pending_lines.clear()
                path_start[0] = None
            elif operator == b"n":
                pending_rectangles.clear()
                pending_lines.clear()
                path_start[0] = None

        def capture_text(
            text,
            current_matrix,
            text_matrix,
            font,
            font_size,
            page_number=page_number,
            page_width=page_width,
            page_height=page_height,
            fill_state=fill_state,
            emitted_text=emitted_text,
        ):
            normalized = " ".join(text.split())
            if not normalized or not normalized.isascii():
                return
            matching_emitted_text = next(
                (
                    (index, emitted)
                    for index, emitted in enumerate(emitted_text)
                    if emitted["text"] == normalized
                    and (emitted["font_size"] is None or float(font_size) == emitted["font_size"])
                ),
                None,
            )
            if matching_emitted_text is None:
                emitted_matrix = text_matrix
            else:
                index, emitted = matching_emitted_text
                emitted_matrix = emitted["matrix"]
                emitted_text.pop(index)
            matching_labels = {label for label in labels if normalized == label}
            matching_labels.update(fragment for fragment in fragments if fragment in normalized)
            x, y = _pdf_transform_point((0, 0), emitted_matrix, current_matrix)
            location = {
                "page": page_number,
                "x": x,
                "y": y,
                "bounds": _pdf_font_text_bounds(
                    normalized,
                    current_matrix,
                    emitted_matrix,
                    font,
                    font_size,
                    page_number,
                    page_width,
                    page_height,
                ),
                "text_color": fill_state[0],
                "text": normalized,
            }
            locations["_all_text_fragments"].append(location)
            for label in matching_labels:
                locations[label].append(location)

        page.extract_text(visitor_operand_before=capture_operation, visitor_text=capture_text)
    return reader, locations, rectangles, grid_lines


def _single_location(locations, label):
    if len(locations[label]) != 1:
        raise AssertionError(f"Expected one rendered {label!r}, found {len(locations[label])}")
    return locations[label][0]


def _grid_cell_at(grid_lines, location):
    vertical_boundaries = sorted(
        {
            line["x1"]
            for line in grid_lines
            if line["page"] == location["page"]
            and line["x1"] == line["x2"]
            and min(line["y1"], line["y2"]) < location["y"] < max(line["y1"], line["y2"])
        }
    )
    horizontal_boundaries = sorted(
        {
            line["y1"]
            for line in grid_lines
            if line["page"] == location["page"]
            and line["y1"] == line["y2"]
            and min(line["x1"], line["x2"]) < location["x"] < max(line["x1"], line["x2"])
        }
    )
    left = max(boundary for boundary in vertical_boundaries if boundary < location["x"])
    right = min(boundary for boundary in vertical_boundaries if boundary > location["x"])
    bottom = max(boundary for boundary in horizontal_boundaries if boundary < location["y"])
    top = min(boundary for boundary in horizontal_boundaries if boundary > location["y"])
    return {
        "page": location["page"],
        "left": left,
        "right": right,
        "bottom": bottom,
        "top": top,
    }


def _day_lane_cells(locations, grid_lines, date_label, next_week_date_label):
    date_location = _single_location(locations, date_label)
    next_week_location = _single_location(locations, next_week_date_label)
    lanes = {}
    for lane in _PRECEPTING_LANES:
        lane_locations = [
            location
            for location in locations[lane]
            if location["page"] == date_location["page"]
            and next_week_location["y"] < location["y"] < date_location["y"]
        ]
        if len(lane_locations) != 1:
            raise AssertionError(
                f"Expected one {lane} row beneath {date_label!r}, found {len(lane_locations)}"
            )
        lanes[lane] = _grid_cell_at(grid_lines, {**lane_locations[0], "x": date_location["x"]})
    return lanes


def _painted_day_lane_cells(cells, rectangles):
    return {lane: _painted_rectangle_for_cell(rectangles, cell) for lane, cell in cells.items()}


def _painted_rectangle_for_cell(rectangles, cell):
    candidates = [
        rectangle
        for rectangle in rectangles
        if rectangle["page"] == cell["page"]
        and rectangle["fill_color"]
        and rectangle["left"] <= cell["left"] + 0.01
        and cell["right"] <= rectangle["right"] + 0.01
        and rectangle["bottom"] <= cell["bottom"] + 0.01
        and cell["top"] <= rectangle["top"] + 0.01
    ]
    if not candidates:
        raise AssertionError(f"No painted region covers {cell!r}")
    return min(
        candidates,
        key=lambda rectangle: (
            (rectangle["right"] - rectangle["left"]) * (rectangle["top"] - rectangle["bottom"])
        ),
    )


def _text_fragments_in_cell(locations, cell):
    return [
        location["text"]
        for location in locations["_all_text_fragments"]
        if location["page"] == cell["page"]
        and cell["left"] < location["x"] < cell["right"]
        and cell["bottom"] < location["y"] < cell["top"]
    ]


def _pdf_font_text_bounds(
    text, current_matrix, text_matrix, font, font_size, page_number, page_width, page_height
):
    descriptor = font["/FontDescriptor"].get_object()
    font_bounds = descriptor["/FontBBox"]
    first_character = int(font["/FirstChar"])
    widths = font["/Widths"]
    text_width = sum(float(widths[ord(character) - first_character]) for character in text)
    scale = float(font_size) / 1000
    corners = (
        (0, float(font_bounds[1]) * scale),
        (text_width * scale, float(font_bounds[1]) * scale),
        (0, float(font_bounds[3]) * scale),
        (text_width * scale, float(font_bounds[3]) * scale),
    )
    page_corners = [_pdf_transform_point(point, text_matrix, current_matrix) for point in corners]
    xs, ys = zip(*page_corners, strict=True)
    return page_number, min(xs), min(ys), max(xs), max(ys), page_width, page_height


def _pdf_transform_point(point, text_matrix, current_matrix):
    x, y = point
    text_x = (text_matrix[0] * x) + (text_matrix[2] * y) + text_matrix[4]
    text_y = (text_matrix[1] * x) + (text_matrix[3] * y) + text_matrix[5]
    return (
        (current_matrix[0] * text_x) + (current_matrix[2] * text_y) + current_matrix[4],
        (current_matrix[1] * text_x) + (current_matrix[3] * text_y) + current_matrix[5],
    )


def _text_bounds_are_within_page(bounds):
    _, left, bottom, right, top, page_width, page_height = bounds
    return 0 <= left < right <= page_width and 0 <= bottom < top <= page_height


def _horizontal_bounds_are_within(bounds, rectangle):
    page, left, _, right, _, _, _ = bounds
    return page == rectangle["page"] and rectangle["left"] <= left < right <= rectangle["right"]


def _same_row(first, second):
    return first["bottom"] == second["bottom"] and first["top"] == second["top"]
