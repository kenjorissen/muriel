from datetime import UTC, date, datetime
from io import BytesIO
from itertools import pairwise
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from schedules.models import (
    AcademicEvent,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    PreceptorAssignment,
)
from schedules.physician_scope import SESSION_KEY
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_occurrence,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.late_docs import assign_late_doc
from schedules.services.physicians import (
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.vacations import create_vacation


class MasterScheduleWorkspaceTests(TestCase):
    monday = date(2025, 2, 3)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def faculty(self, name, **availability):
        physician = create_physician(display_name=name, scheduler=self.user)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={
                slot: availability.get(slot, "open")
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.user,
        )
        return physician

    def response(self, selected_date=None):
        query = {"date": selected_date.isoformat()} if selected_date else {}
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
        ):
            return self.client.get(reverse("master-schedule"), query)

    @staticmethod
    def pdf_artifact_locations(pdf, labels):
        """Read rendered PDF text and painted rectangles without table internals."""
        reader = PdfReader(BytesIO(pdf.content))
        locations = {label: [] for label in labels}
        rectangles = []
        for page_number, page in enumerate(reader.pages):
            fill_state = [None]
            fill_colors = []
            pending_rectangles = []

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
            ):
                if operator == b"q":
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
                elif operator in {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}:
                    rectangles.extend(
                        {**rectangle, "fill_color": fill_state[0]}
                        for rectangle in pending_rectangles
                    )
                    pending_rectangles.clear()
                elif operator in {b"S", b"s", b"n"}:
                    pending_rectangles.clear()

            def capture_text(text, current_matrix, text_matrix, *args, page_number=page_number):
                label = " ".join(text.split())
                if label in locations:
                    # PDF text coordinates compose its text matrix with the current
                    # graphics transform; adding translation offsets loses rotation.
                    matrix = (
                        text_matrix[0] * current_matrix[0] + text_matrix[1] * current_matrix[2],
                        text_matrix[0] * current_matrix[1] + text_matrix[1] * current_matrix[3],
                        text_matrix[2] * current_matrix[0] + text_matrix[3] * current_matrix[2],
                        text_matrix[2] * current_matrix[1] + text_matrix[3] * current_matrix[3],
                        text_matrix[4] * current_matrix[0]
                        + text_matrix[5] * current_matrix[2]
                        + current_matrix[4],
                        text_matrix[4] * current_matrix[1]
                        + text_matrix[5] * current_matrix[3]
                        + current_matrix[5],
                    )
                    locations[label].append(
                        {
                            "page": page_number,
                            "x": matrix[4],
                            "y": matrix[5],
                            "direction": matrix[:2],
                        }
                    )

            page.extract_text(visitor_operand_before=capture_operation, visitor_text=capture_text)
        return reader, locations, rectangles

    @staticmethod
    def painted_rectangle_at(rectangles, location):
        """Find the visible painted cell containing an emitted text location."""
        candidates = [
            rectangle
            for rectangle in rectangles
            if rectangle["page"] == location["page"]
            and rectangle["fill_color"]
            and rectangle["left"] < location["x"] < rectangle["right"]
            and rectangle["bottom"] < location["y"] < rectangle["top"]
        ]
        return min(
            candidates,
            key=lambda rectangle: (
                (rectangle["right"] - rectangle["left"]) * (rectangle["top"] - rectangle["bottom"])
            ),
        )

    def assert_pdf_closures(
        self,
        pdf,
        *,
        dates,
        expected,
        morning_row_labels=("Preceptor 1", "Preceptor 2", "Secondary"),
    ):
        """Prove each compact closure paints its stated date column and period span."""
        _, locations, rectangles = self.pdf_artifact_locations(
            pdf, {"Closed", *morning_row_labels, "Late Doc", *dates}
        )
        self.assertTrue(all(locations[label] for label in dates))
        headers = [max(locations[label], key=lambda location: location["y"]) for label in dates]
        self.assertEqual(len({header["page"] for header in headers}), 1)
        header_page = headers[0]["page"]
        header_xs = [header["x"] for header in headers]
        self.assertEqual(header_xs, sorted(header_xs))
        boundaries = [
            header_xs[0] - (header_xs[1] - header_xs[0]) / 2,
            *(left + (right - left) / 2 for left, right in pairwise(header_xs)),
            header_xs[-1] + (header_xs[-1] - header_xs[-2]) / 2,
        ]
        columns = {
            label: (boundaries[index], boundaries[index + 1]) for index, label in enumerate(dates)
        }
        morning_rows = [
            max(locations[label], key=lambda marker: marker["y"]) for label in morning_row_labels
        ]
        afternoon_rows = [
            min(locations["Preceptor 1"], key=lambda marker: marker["y"]),
            min(locations["Preceptor 2"], key=lambda marker: marker["y"]),
            min(locations["Secondary"], key=lambda marker: marker["y"]),
            locations["Late Doc"][0],
        ]
        lane_boundary = (
            min(marker["y"] for marker in morning_rows)
            + max(marker["y"] for marker in afternoon_rows)
        ) / 2
        self.assertEqual(len(locations["Closed"]), len(expected))

        closures = {}
        for closure in locations["Closed"]:
            self.assertEqual(closure["page"], header_page)
            closure_date = next(
                (label for label, (left, right) in columns.items() if left < closure["x"] < right),
                None,
            )
            self.assertIsNotNone(closure_date)
            closure_period = "Morning" if closure["y"] > lane_boundary else "Afternoon"
            self.assertNotIn((closure_date, closure_period), closures)
            closures[closure_date, closure_period] = closure

        self.assertEqual(set(closures), set(expected))
        for expected_date, expected_period in expected:
            closure = closures[expected_date, expected_period]
            left, right = columns[expected_date]
            expected_rows, other_rows = (
                (morning_rows, afternoon_rows)
                if expected_period == "Morning"
                else (afternoon_rows, morning_rows)
            )
            matching_rectangles = [
                rectangle
                for rectangle in rectangles
                if rectangle["page"] == closure["page"]
                and rectangle["fill_color"]
                and any(channel < 0.99 for channel in rectangle["fill_color"])
                and rectangle["left"] < closure["x"] < rectangle["right"]
                and rectangle["bottom"] < closure["y"] < rectangle["top"]
                and left < (rectangle["left"] + rectangle["right"]) / 2 < right
                and rectangle["right"] - rectangle["left"] >= (right - left) * 0.75
                and all(
                    rectangle["bottom"] < marker["y"] < rectangle["top"] for marker in expected_rows
                )
                and all(
                    not rectangle["bottom"] < marker["y"] < rectangle["top"]
                    for marker in other_rows
                )
            ]
            self.assertTrue(
                matching_rectangles,
                f"{expected_period} closure for {expected_date} lacks a painted day-column span.",
            )

    def test_defaults_to_current_week_with_shared_week_navigation_and_two_date_spans(self):
        response = self.response()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["projection"].week_start, self.monday)
        self.assertTemplateUsed(response, "schedules/schedule_workspace_week_navigation.html")
        self.assertContains(response, 'aria-label="Week navigation"')
        self.assertContains(response, 'href="/schedule/?date=2025-01-27"')
        self.assertContains(response, 'href="/schedule/?date=2025-02-10"')
        self.assertContains(
            response,
            '<section class="workspace-surface surface-frame master-schedule-title-surface" '
            'aria-labelledby="master-schedule-heading">',
        )
        self.assertContains(
            response,
            '<header class="surface-header master-schedule-title-row"><h1 '
            'id="master-schedule-heading">Master Schedule</h1>',
        )
        clinic = response.context["clinic_grid"]
        supporting = response.context["supporting_grid"]
        self.assertEqual(
            [day.iso_date for day in clinic.days],
            ["2025-02-03", "2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07"],
        )
        self.assertEqual(
            [day.label for day in clinic.days],
            ["Mon 2/3", "Tue 2/4", "Wed 2/5", "Thu 2/6", "Fri 2/7"],
        )
        self.assertEqual(
            [day.iso_date for day in supporting.days],
            [
                "2025-02-02",
                "2025-02-03",
                "2025-02-04",
                "2025-02-05",
                "2025-02-06",
                "2025-02-07",
                "2025-02-08",
            ],
        )
        self.assertEqual(
            [day.label for day in supporting.days],
            ["Sun 2/2", "Mon 2/3", "Tue 2/4", "Wed 2/5", "Thu 2/6", "Fri 2/7", "Sat 2/8"],
        )
        self.assertContains(response, "Supporting schedules")
        self.assertNotContains(response, '<h2 id="clinic-schedule-heading">')
        self.assertContains(
            response,
            '<header class="surface-header"><h2 id="supporting-schedule-heading">Supporting schedules</h2></header>',
            html=True,
        )

    def test_orders_groups_and_keeps_ordinary_empty_cells_blank(self):
        response = self.response(self.monday)

        self.assertEqual(
            [group.label for group in response.context["clinic_grid"].groups],
            ["Morning", "Afternoon", "Faculty availability"],
        )
        self.assertEqual(
            [row.label for row in response.context["clinic_grid"].groups[0].rows],
            ["Preceptor 1", "Preceptor 2", "Secondary"],
        )
        self.assertEqual(
            [row.label for row in response.context["clinic_grid"].groups[1].rows],
            ["Preceptor 1", "Preceptor 2", "Secondary", "Late Doc"],
        )
        self.assertEqual(
            [group.label for group in response.context["supporting_grid"].groups],
            ["Rounding", "Community Precepting", "Vacation", "Academic Events"],
        )
        self.assertEqual(
            [row.label for row in response.context["supporting_grid"].groups[0].rows],
            ["Normal Rounding", "Casual Rounding"],
        )
        empty_cell = response.context["clinic_grid"].groups[0].rows[0].cells[0]
        self.assertEqual((empty_cell.facts, empty_cell.findings), ((), ()))
        self.assertNotContains(response, "Not scheduled")
        self.assertNotContains(response, "No assignment")
        self.assertNotContains(response, "Assigned to Precepting")

    def test_uses_once_only_semantic_row_group_headers_for_clinic_periods(self):
        response = self.response(self.monday)

        content = response.content.decode()
        self.assertEqual(
            content.count(
                'class="master-schedule-grid-rowgroup surface-tone--schedule-group" scope="rowgroup" '
                'rowspan="3"><span class="master-schedule-grid-rowgroup-label">Morning</span></th>'
            ),
            1,
        )
        self.assertEqual(
            content.count(
                'class="master-schedule-grid-rowgroup surface-tone--schedule-group" scope="rowgroup" '
                'rowspan="4"><span class="master-schedule-grid-rowgroup-label">Afternoon</span></th>'
            ),
            1,
        )
        self.assertNotContains(response, 'scope="colgroup">Morning</th>')
        self.assertNotContains(response, 'scope="colgroup">Afternoon</th>')
        self.assertContains(
            response,
            '<th class="surface-tone--calendar" colspan="2" scope="col">Clinic</th>',
            html=True,
        )
        self.assertNotContains(response, '<th scope="col">Period</th>', html=True)

    def test_displays_compact_non_navigational_assignments_and_supporting_facts(self):
        first = self.faculty("First")
        second = self.faculty("Second")
        sessions = open_weekly_draft(self.monday)
        am = next(session for session in sessions if session.period_of_day == "AM")
        pm = next(session for session in sessions if session.period_of_day == "PM")
        assign_preceptor(
            session=am,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=pm,
            physician=second,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.user,
        )
        late_doc = assign_late_doc(on_date=self.monday, physician=first, scheduler=self.user)
        shift = create_interview_shift(academic_year=2024, date=self.monday, scheduler=self.user)
        assign_interview_shift(shift=shift, physician=second, scheduler=self.user)
        vacation = create_vacation(
            physician=first,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="PM",
            scheduler=self.user,
        )
        definition = create_academic_event_definition(
            base_name="Teaching",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=1,
            scheduler=self.user,
        )
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(second,),
            scheduler=self.user,
        )

        response = self.response(self.monday)

        self.assertContains(response, "Preceptor 1")
        self.assertContains(response, "Secondary")
        self.assertContains(response, "Late Doc")
        self.assertNotContains(response, f'href="/week/?date={self.monday.isoformat()}"')
        self.assertNotContains(
            response, f'href="{reverse("late-doc-edit", args=(late_doc.date.isoformat(),))}"'
        )
        self.assertNotContains(response, f'href="{reverse("interview-correct", args=(shift.pk,))}"')
        self.assertNotContains(
            response, f'href="{reverse("vacation-correct", args=(vacation.pk,))}"'
        )
        self.assertNotContains(
            response, f'href="{reverse("academic-event-correct", args=(event.pk,))}"'
        )
        supporting_rows = response.context["supporting_grid"].groups
        vacation = next(group for group in supporting_rows if group.key == "vacation").rows[0]
        self.assertEqual([fact.label for fact in vacation.cells[1].facts], ["First"])
        self.assertEqual([fact.detail for fact in vacation.cells[1].facts], ["PM"])
        event_row = next(group for group in supporting_rows if group.key == "academic-events").rows[
            0
        ]
        self.assertEqual([fact.label for fact in event_row.cells[1].facts], ["Teaching"])
        self.assertEqual([fact.detail for fact in event_row.cells[1].facts], ["Second"])
        self.assertNotContains(response, "All day")

    def test_labels_calendar_only_academic_events_in_supporting_facts(self):
        definition = create_academic_event_definition(
            base_name="Calendar entry",
            start_academic_year=2024,
            expected_quarter="Q4",
            scheduler=self.user,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day=AcademicEvent.PeriodOfDay.NONE,
            precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            scheduler=self.user,
        )

        response = self.response(self.monday)

        groups = {group.key: group for group in response.context["supporting_grid"].groups}
        event_fact = groups["academic-events"].rows[0].cells[1].facts[0]
        self.assertEqual(
            (event_fact.label, event_fact.detail),
            ("Calendar entry", "Calendar-only Academic Event."),
        )
        self.assertEqual(event_fact.attention, "")

    def test_displays_interview_cells_during_the_interview_season(self):
        shift = create_interview_shift(
            academic_year=2024, date=date(2024, 10, 7), scheduler=self.user
        )

        response = self.response(shift.date)

        interviews = next(
            row
            for row in response.context["clinic_grid"].groups[0].rows
            if row.label == "Interviews"
        )
        self.assertEqual([fact.label for fact in interviews.cells[0].facts], ["Interview shift"])

    def test_compacts_supporting_schedule_facts_without_hiding_attention(self):
        normal = self.faculty("Normal")
        casual = create_physician(display_name="Casual", scheduler=self.user)
        community = create_physician(display_name="Community", scheduler=self.user)
        event_one = self.faculty("Event one")
        event_two = self.faculty("Event two")
        full_day = create_physician(display_name="Full day vacation", scheduler=self.user)
        partial_day = create_physician(display_name="Partial day vacation", scheduler=self.user)
        record_normal_rounder(week=self.monday, physician=normal, scheduler=self.user)
        create_casual_rounder_rule(physician=casual, weekday=0, ordinals=[1], scheduler=self.user)
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM", "PM"],
            scheduler=self.user,
        )
        definition = create_academic_event_definition(
            base_name="Teaching",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=3,
            scheduler=self.user,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(event_one, event_two),
            scheduler=self.user,
        )
        create_vacation(
            physician=full_day,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="whole_day",
            scheduler=self.user,
        )
        create_vacation(
            physician=partial_day,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="AM",
            scheduler=self.user,
        )

        response = self.response(self.monday)

        groups = {group.key: group for group in response.context["supporting_grid"].groups}
        monday = 1
        normal_fact = groups["rounding"].rows[0].cells[monday].facts[0]
        casual_fact = groups["rounding"].rows[1].cells[monday].facts[0]
        community_facts = groups["community-precepting"].rows[0].cells[monday].facts
        vacation_facts = groups["vacation"].rows[0].cells[monday].facts
        event_fact = groups["academic-events"].rows[0].cells[monday].facts[0]
        self.assertEqual((normal_fact.label, normal_fact.detail), ("Normal", ""))
        self.assertEqual((casual_fact.label, casual_fact.detail), ("Casual", ""))
        self.assertEqual(
            [(fact.label, fact.detail) for fact in community_facts], [("Community", "")]
        )
        self.assertEqual(
            [(fact.label, fact.detail) for fact in vacation_facts],
            [("Full day vacation", ""), ("Partial day vacation", "AM")],
        )
        self.assertEqual(
            (event_fact.label, event_fact.detail), ("Teaching", "Event one, Event two")
        )
        self.assertEqual(event_fact.attention, "incomplete")

    def test_preserves_distinct_same_named_physicians_in_compact_supporting_facts(self):
        first = create_physician(display_name="Same name", scheduler=self.user)
        second = create_physician(display_name="Same name", scheduler=self.user)
        create_community_preceptor_rule(
            physician=first,
            weekday=0,
            ordinals=[1],
            periods=["AM", "PM"],
            scheduler=self.user,
        )
        create_community_preceptor_rule(
            physician=second,
            weekday=0,
            ordinals=[1],
            periods=["AM", "PM"],
            scheduler=self.user,
        )
        definition = create_academic_event_definition(
            base_name="Teaching",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=2,
            scheduler=self.user,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(first, second),
            scheduler=self.user,
        )

        response = self.response(self.monday)

        groups = {group.key: group for group in response.context["supporting_grid"].groups}
        monday = 1
        community_facts = groups["community-precepting"].rows[0].cells[monday].facts
        event_facts = groups["academic-events"].rows[0].cells[monday].facts
        self.assertEqual([fact.label for fact in community_facts], ["Same name", "Same name"])
        self.assertEqual(
            [(fact.label, fact.detail) for fact in event_facts],
            [("Teaching", "Same name, Same name")],
        )

    def test_displays_holiday_closures_without_source_navigation(self):
        response = self.response(date(2025, 7, 4))

        self.assertNotContains(
            response,
            f'href="{reverse("holiday-workspace")}?calendar_year=2025"',
        )
        self.assertContains(response, "Independence Day")
        self.assertContains(
            response,
            'aria-label="Holiday: Independence Day, AM and PM"',
        )
        christmas_eve = self.response(date(2025, 12, 24))
        self.assertContains(christmas_eve, "Christmas Eve")
        self.assertContains(christmas_eve, "<span>Christmas Eve, PM</span>", html=True)

    def test_presents_holiday_headers_and_compact_clinic_closures(self):
        labor_day = self.response(date(2025, 9, 1))

        clinic = labor_day.context["clinic_grid"]
        self.assertEqual([holiday.name for holiday in clinic.days[0].holidays], ["Labor Day"])
        self.assertEqual([group.closed_day_indexes for group in clinic.groups[:2]], [(0,), (0,)])
        self.assertContains(
            labor_day,
            '<th class="surface-tone--calendar" scope="col">'
            '<time class="calendar-date-header-label" datetime="2025-09-01">Mon 9/1</time>'
            '<span class="calendar-holiday-header" role="note" '
            'aria-label="Holiday: Labor Day, AM and PM"><span>Labor Day</span></span></th>',
            html=True,
        )
        self.assertEqual(labor_day.content.count(b"master-schedule-grid-cell--holiday"), 5)
        self.assertNotContains(labor_day, 'scope="colgroup">Holidays</th>')
        self.assertEqual(labor_day.content.count(b"master-schedule-grid-closed"), 2)
        self.assertEqual(labor_day.content.count(b">Closed</span>"), 2)
        self.assertContains(labor_day, 'class="master-schedule-grid-closed" rowspan="3"')
        self.assertContains(labor_day, 'class="master-schedule-grid-closed" rowspan="4"')

        christmas_eve = self.response(date(2025, 12, 24))

        clinic = christmas_eve.context["clinic_grid"]
        self.assertEqual([holiday.name for holiday in clinic.days[2].holidays], ["Christmas Eve"])
        self.assertEqual([group.closed_day_indexes for group in clinic.groups[:2]], [(3,), (2, 3)])
        self.assertContains(
            christmas_eve,
            'aria-label="Holiday: Christmas Eve, PM only"><span>Christmas Eve, PM</span>',
        )
        self.assertEqual(christmas_eve.content.count(b"master-schedule-grid-closed"), 3)

    def test_pdf_presents_holiday_headers_and_compact_clinic_closures(self):
        for on_date, holiday_name, dates, expected, morning_row_labels in (
            (
                date(2025, 9, 1),
                "Labor Day",
                ("Mon 9/1", "Tue 9/2", "Wed 9/3", "Thu 9/4", "Fri 9/5"),
                (("Mon 9/1", "Morning"), ("Mon 9/1", "Afternoon")),
                ("Preceptor 1", "Preceptor 2", "Secondary"),
            ),
            (
                date(2025, 12, 24),
                "Christmas Eve, PM",
                ("Mon 12/22", "Tue 12/23", "Wed 12/24", "Thu 12/25", "Fri 12/26"),
                (
                    ("Thu 12/25", "Morning"),
                    ("Wed 12/24", "Afternoon"),
                    ("Thu 12/25", "Afternoon"),
                ),
                # October-January retains an Interviews row, even with no scheduled shift.
                # See docs/design.md, Master Schedule.
                ("Preceptor 1", "Preceptor 2", "Secondary", "Interviews"),
            ),
        ):
            pdf = self.client.get(reverse("master-schedule-pdf"), {"date": on_date.isoformat()})
            reader = PdfReader(BytesIO(pdf.content))
            text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)

            self.assertIn(holiday_name, text)
            self.assert_pdf_closures(
                pdf,
                dates=dates,
                expected=expected,
                morning_row_labels=morning_row_labels,
            )

            if holiday_name == "Labor Day":
                _, locations, rectangles = self.pdf_artifact_locations(
                    pdf,
                    {
                        "Clinic",
                        *dates,
                        "Normal Rounding",
                        "Casual Rounding",
                        "Community Precepting",
                        "Vacation",
                        "Academic Events",
                    },
                )
                supporting_header = min(locations[dates[0]], key=lambda location: location["y"])
                ordinary_header = min(locations[dates[1]], key=lambda location: location["y"])
                row_locations = [
                    min(locations[label], key=lambda location: location["y"])
                    for label in (
                        "Normal Rounding",
                        "Casual Rounding",
                        "Community Precepting",
                        "Vacation",
                        "Academic Events",
                    )
                ]
                holiday_cells = [
                    self.painted_rectangle_at(
                        rectangles,
                        {**row, "x": supporting_header["x"]},
                    )["fill_color"]
                    for row in row_locations
                ]
                ordinary_cell = self.painted_rectangle_at(
                    rectangles,
                    {**row_locations[0], "x": ordinary_header["x"]},
                )["fill_color"]
                calendar_header = self.painted_rectangle_at(rectangles, locations["Clinic"][0])[
                    "fill_color"
                ]
                self.assertEqual(len(set(holiday_cells)), 1)
                self.assertNotEqual(holiday_cells[0], ordinary_cell)
                self.assertEqual(
                    self.painted_rectangle_at(rectangles, supporting_header)["fill_color"],
                    calendar_header,
                )

    def test_pdf_places_distinct_assignments_in_their_date_period_and_position_cells(self):
        morning_primary = self.faculty("Monday morning primary")
        afternoon_second = self.faculty("Tuesday afternoon second")
        sessions = {
            (session.date, session.period_of_day): session
            for session in open_weekly_draft(self.monday)
        }
        assign_preceptor(
            session=sessions[(self.monday, "AM")],
            physician=morning_primary,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=sessions[(date(2025, 2, 4), "PM")],
            physician=afternoon_second,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.user,
        )
        dates = ("Mon 2/3", "Tue 2/4", "Wed 2/5", "Thu 2/6", "Fri 2/7")
        pdf = self.client.get(reverse("master-schedule-pdf"), {"date": self.monday.isoformat()})
        _, locations, _ = self.pdf_artifact_locations(
            pdf,
            {
                *dates,
                "Monday morning primary",
                "Tuesday afternoon second",
                "Preceptor 1",
                "Preceptor 2",
                "Secondary",
                "Late Doc",
            },
        )
        headers = [max(locations[label], key=lambda location: location["y"]) for label in dates]
        header_xs = [header["x"] for header in headers]
        boundaries = [
            header_xs[0] - (header_xs[1] - header_xs[0]) / 2,
            *(left + (right - left) / 2 for left, right in pairwise(header_xs)),
            header_xs[-1] + (header_xs[-1] - header_xs[-2]) / 2,
        ]
        columns = {
            label: (boundaries[index], boundaries[index + 1]) for index, label in enumerate(dates)
        }
        row_markers = [
            *locations["Preceptor 1"],
            *locations["Preceptor 2"],
            *locations["Secondary"],
            *locations["Late Doc"],
        ]
        morning_rows = [
            max(locations["Preceptor 1"], key=lambda marker: marker["y"]),
            max(locations["Preceptor 2"], key=lambda marker: marker["y"]),
            max(locations["Secondary"], key=lambda marker: marker["y"]),
        ]
        afternoon_rows = [
            min(locations["Preceptor 1"], key=lambda marker: marker["y"]),
            min(locations["Preceptor 2"], key=lambda marker: marker["y"]),
            min(locations["Secondary"], key=lambda marker: marker["y"]),
            locations["Late Doc"][0],
        ]
        morning_boundary = (
            min(marker["y"] for marker in morning_rows)
            + max(marker["y"] for marker in afternoon_rows)
        ) / 2
        for name, expected_date, expected_position, expected_period in (
            ("Monday morning primary", "Mon 2/3", "Preceptor 1", "Morning"),
            ("Tuesday afternoon second", "Tue 2/4", "Preceptor 2", "Afternoon"),
        ):
            left, right = columns[expected_date]
            assignments = [
                location
                for location in locations[name]
                if location["page"] == headers[0]["page"]
                and left < location["x"] < right
                and (location["y"] > morning_boundary) == (expected_period == "Morning")
            ]
            self.assertEqual(len(assignments), 1)
            assignment = assignments[0]
            nearest_row = min(row_markers, key=lambda marker: abs(marker["y"] - assignment["y"]))
            self.assertEqual(nearest_row["page"], assignment["page"])
            self.assertEqual(
                "Morning" if assignment["y"] > morning_boundary else "Afternoon", expected_period
            )
            self.assertIn(
                nearest_row,
                locations[expected_position],
                f"{name} is not rendered beside its {expected_position} row label.",
            )

    def test_pdf_retains_counterclockwise_labels_and_distinct_surface_roles(self):
        pdf = self.client.get(reverse("master-schedule-pdf"), {"date": self.monday.isoformat()})
        _, locations, rectangles = self.pdf_artifact_locations(
            pdf,
            {
                "Clinic",
                "Morning",
                "Afternoon",
                "Preceptor 1",
                "Rounding",
                "Community Precepting",
                "Vacation",
                "Academic Events",
            },
        )

        for label in ("Morning", "Afternoon"):
            self.assertEqual(len(locations[label]), 1)
            horizontal, vertical = locations[label][0]["direction"]
            self.assertGreater(vertical, 0)
            self.assertGreater(abs(vertical), abs(horizontal))

        def painted_color(label):
            location = max(locations[label], key=lambda item: item["y"])
            return self.painted_rectangle_at(rectangles, location)["fill_color"]

        coordinate_color = painted_color("Clinic")
        subtle_color = painted_color("Preceptor 1")
        schedule_group_colors = [
            painted_color(label)
            for label in (
                "Morning",
                "Afternoon",
                "Rounding",
                "Community Precepting",
                "Vacation",
                "Academic Events",
            )
        ]
        self.assertEqual(len(set(schedule_group_colors)), 1)
        self.assertNotEqual(coordinate_color, subtle_color)
        self.assertNotEqual(coordinate_color, schedule_group_colors[0])
        self.assertNotEqual(subtle_color, schedule_group_colors[0])

    def test_summarizes_unoccupied_authoritative_faculty_availability_by_day(self):
        full_day = self.faculty("Full day")
        slots = {slot: "off" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        slots["monday_am"] = "open"
        self.faculty("Partial day", **slots)
        am = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=am,
            physician=full_day,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )

        response = self.response(self.monday)

        faculty = response.context["clinic_grid"].groups[-1]
        self.assertEqual(faculty.label, "Faculty availability")
        self.assertEqual([row.label for row in faculty.rows], ["Available faculty"])
        self.assertEqual(
            {fact.label for fact in faculty.rows[0].cells[0].facts},
            {"Full day (PM)", "Partial day (AM)"},
        )
        self.assertEqual([fact.label for fact in faculty.rows[0].cells[1].facts], ["Full day"])

    def test_physician_focus_filters_assignment_contents_and_keeps_faculty_rows(self):
        focused = self.faculty("Focused")
        outside = self.faculty("Outside")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=outside,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )
        session = self.client.session
        session[SESSION_KEY] = [focused.pk]
        session.save()

        response = self.response(self.monday)

        self.assertContains(response, "Preceptor 1")
        self.assertContains(response, "Focused")
        clinic_row = response.context["clinic_grid"].groups[0].rows[0]
        self.assertEqual(clinic_row.cells[0].facts, ())
        faculty_row = response.context["clinic_grid"].groups[-1].rows[0]
        self.assertEqual([fact.label for fact in faculty_row.cells[0].facts], ["Focused"])

    def test_pdf_exports_the_displayed_week_focus_grids_findings_capture_time_and_layout(self):
        focused = self.faculty("Focused")
        outside = self.faculty("Outside")
        backup = self.faculty("Backup", monday_am="backup_only")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=outside,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=session,
            physician=backup,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.user,
        )
        create_vacation(
            physician=focused,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="AM",
            scheduler=self.user,
        )
        definition = create_academic_event_definition(
            base_name="Teaching",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=1,
            scheduler=self.user,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(),
            scheduler=self.user,
        )
        client_session = self.client.session
        client_session[SESSION_KEY] = [focused.pk]
        client_session.save()

        screen = self.response(self.monday)
        with (
            patch(
                "schedules.views.master_schedule.timezone.now",
                return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
            ),
            patch(
                "schedules.services.operational_attention.timezone.now",
                return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
            ),
        ):
            pdf = self.client.get(
                reverse("master-schedule-pdf"),
                {"date": self.monday.isoformat(), "physicians": [focused.pk]},
            )

        reader = PdfReader(BytesIO(pdf.content))
        text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
        self.assertContains(
            screen,
            f'href="/schedule/pdf/?date=2025-02-03&amp;physicians={focused.pk}">PDF</a>',
        )
        self.assertContains(
            screen,
            '<header class="surface-header master-schedule-title-row"><h1 '
            'id="master-schedule-heading">Master Schedule</h1>',
        )
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertEqual(
            pdf["Content-Disposition"], 'attachment; filename="master-schedule-2025-02-03.pdf"'
        )
        self.assertTrue(pdf.content.startswith(b"%PDF-"))
        self.assertGreater(reader.pages[0].mediabox.width, reader.pages[0].mediabox.height)
        self.assertIn("Master Schedule \N{EM DASH} Feb 3\N{EN DASH}7, 2025", text)
        self.assertIn("Clinic", text)
        self.assertIn("Mon 2/3", text)
        self.assertIn("Sun 2/2", text)
        self.assertLess(text.index("Master Schedule"), text.index("Preceptor 1"))
        self.assertNotIn("PDF", text)
        self.assertIn("Morning", text)
        self.assertIn("Preceptor 1", text)
        self.assertIn("Faculty availability", text)
        self.assertIn("Supporting schedules", text)
        self.assertIn("Rounding", text)
        self.assertIn("Vacation", text)
        self.assertIn("Focused", text)
        self.assertNotIn("Outside", text)
        self.assertTrue(screen.context["projection"].findings)
        self.assertIn(screen.context["projection"].findings[0].message, text)
        attention = next(
            fact.attention
            for grid in (screen.context["clinic_grid"], screen.context["supporting_grid"])
            for group in grid.groups
            for row in group.rows
            for cell in row.cells
            for fact in cell.facts
            if fact.label == "Teaching"
        )
        self.assertEqual(attention, "incomplete")
        finding = next(
            finding
            for grid in (screen.context["clinic_grid"], screen.context["supporting_grid"])
            for group in grid.groups
            for row in group.rows
            for cell in row.cells
            for finding in cell.findings
            if finding.sources
        )
        self.assertEqual(finding.severity, "preference")
        self.assertEqual([source.label for source in finding.sources], ["Backup"])
        self.assertIn("Incomplete", text)
        self.assertIn("Preference finding:", text)
        self.assertIn("Sources: Backup", text)
        self.assertIn("Captured 2025-02-03 12:00 CST", text)
        self.assertNotIn("Change physician focus", text)

    def test_places_every_finding_at_its_target_with_message_and_source_label(self):
        physician = self.faculty("Backup", monday_am="backup_only")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )

        response = self.response(self.monday)

        projection = response.context["projection"]
        displayed_findings = [
            finding
            for grid in (response.context["clinic_grid"], response.context["supporting_grid"])
            for group in grid.groups
            for row in group.rows
            for cell in row.cells
            for finding in cell.findings
        ]
        self.assertEqual(
            len(displayed_findings), sum(len(finding.targets) for finding in projection.findings)
        )
        self.assertTrue(displayed_findings)
        backup_locations = [
            (group.key, row.key, day.iso_date, finding.severity)
            for group in response.context["clinic_grid"].groups
            for row in group.rows
            for day, cell in zip(response.context["clinic_grid"].days, row.cells, strict=True)
            for finding in cell.findings
            if any(source.label == "Backup" for source in finding.sources)
        ]
        self.assertEqual(
            backup_locations,
            [("clinic-morning", "precepting-primary-1", "2025-02-03", "preference")],
        )
        for finding in projection.findings:
            self.assertContains(response, finding.message)
        self.assertContains(response, "Sources:")
        self.assertNotContains(
            response, f'href="{reverse("physician-detail", args=(physician.pk,))}"'
        )
