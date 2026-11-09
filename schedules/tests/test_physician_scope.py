from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    CasualRounderRule,
    CommunityPreceptorRule,
    InterviewShift,
    NormalRounder,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    Vacation,
)
from schedules.services.vacations import create_vacation
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


class PhysicianScopeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.first = Physician.objects.create(display_name="First physician")
        self.second = Physician.objects.create(display_name="Second physician")
        self.client.force_login(self.user)

    def test_selection_deduplicates_valid_posted_physicians_and_replaces_the_prior_focus(self):
        destination = f"{reverse('vacation-list')}?month=2026-08"
        Vacation.objects.create(
            physician=self.first, start_date="2026-08-03", end_date="2026-08-03"
        )
        Vacation.objects.create(
            physician=self.second, start_date="2026-08-04", end_date="2026-08-04"
        )
        response = self.client.post(
            reverse("physician-scope"),
            {
                "physicians": [str(self.second.pk), str(self.first.pk), str(self.second.pk), "bad"],
                "return": destination,
            },
        )
        self.assertRedirects(response, destination)
        workspace = self.client.get(destination)

        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.request["QUERY_STRING"], "month=2026-08")
        selected = workspace.context["physician_scope_selected"]
        self.assertEqual(len(selected), 2)
        self.assertEqual({physician.pk for physician in selected}, {self.first.pk, self.second.pk})
        self.assertEqual(
            set(workspace.context["vacation_list_rows"].values_list("physician_id", flat=True)),
            {self.first.pk, self.second.pk},
        )

        replacement = self._select(self.first, return_to=destination)
        selected = replacement.context["physician_scope_selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual({physician.pk for physician in selected}, {self.first.pk})
        self.assertEqual(
            list(replacement.context["vacation_list_rows"].values_list("physician_id", flat=True)),
            [self.first.pk],
        )

    def test_deleted_selected_physician_returns_the_supported_workspace_to_everyone(self):
        destination = f"{reverse('vacation-list')}?month=2026-08"
        Vacation.objects.create(
            physician=self.first, start_date="2026-08-03", end_date="2026-08-03"
        )
        Vacation.objects.create(
            physician=self.second, start_date="2026-08-04", end_date="2026-08-04"
        )
        temporary = Physician.objects.create(display_name="Temporary physician")
        self._select(temporary, return_to=destination)
        Physician.objects.filter(pk=temporary.pk).delete()

        workspace = self.client.get(destination)

        self.assertEqual(self._selected_physician_choice_ids(workspace), [])
        surface = self._surface_text(workspace, "vacation-list-section")
        self.assertIn("First physician", surface)
        self.assertIn("Second physician", surface)

    def test_reset_returns_to_everyone(self):
        destination = f"{reverse('vacation-list')}?month=2026-08"
        Vacation.objects.create(
            physician=self.first, start_date="2026-08-03", end_date="2026-08-03"
        )
        Vacation.objects.create(
            physician=self.second, start_date="2026-08-04", end_date="2026-08-04"
        )
        self._select(self.first, return_to=destination)

        response = self.client.post(
            reverse("physician-scope"), {"reset": "1", "return": destination}
        )
        self.assertRedirects(response, destination)
        workspace = self.client.get(destination)
        self.assertEqual(self._selected_physician_choice_ids(workspace), [])
        surface = self._surface_text(workspace, "vacation-list-section")
        self.assertIn("First physician", surface)
        self.assertIn("Second physician", surface)

    def test_unsafe_return_is_rejected_and_the_control_requires_authentication(self):
        response = self.client.post(
            reverse("physician-scope"),
            {"physicians": [str(self.first.pk)], "return": "https://example.test/"},
        )
        self.assertRedirects(response, reverse("monthly-workspace"))
        self.assertContains(
            self.client.get(reverse("monthly-workspace")), "Selected physicians: First physician"
        )

        anonymous = Client()
        response = anonymous.post(reverse("physician-scope"), {"physicians": [str(self.first.pk)]})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("sign-in"), response["Location"])

    def test_vacation_presentation_narrows_without_changing_the_retained_records(self):
        Vacation.objects.create(
            physician=self.first, start_date="2026-08-03", end_date="2026-08-03"
        )
        Vacation.objects.create(
            physician=self.second, start_date="2026-08-04", end_date="2026-08-04"
        )
        self._select(self.first, return_to=f"{reverse('vacation-list')}?month=2026-08")

        response = self.client.get(reverse("vacation-list"), {"month": "2026-08"})
        self.assertIn("First physician", self._surface_text(response, "vacation-list-section"))
        self.assertNotIn("Second physician", self._surface_text(response, "vacation-list-section"))
        self.assertEqual(
            list(response.context["vacation_list_rows"].values_list("physician_id", flat=True)),
            [self.first.pk],
        )
        self.assertEqual(Vacation.objects.count(), 2)

    def _select(self, *physicians, return_to=None):
        return_to = return_to or reverse("monthly-workspace")
        response = self.client.post(
            reverse("physician-scope"),
            {"physicians": [str(physician.pk) for physician in physicians], "return": return_to},
        )
        self.assertRedirects(response, return_to)
        return self.client.get(return_to)

    def _surface_text(self, response, surface_class):
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        surfaces = [
            element
            for element in parser.elements
            if surface_class in element.attributes.get("class", "").split()
        ]
        self.assertTrue(surfaces)
        return " ".join(" ".join(surface.text().split()) for surface in surfaces)

    def _selected_physician_choice_ids(self, response):
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        return [
            element.attributes["value"]
            for element in parser.elements
            if element.tag == "input"
            and element.attributes.get("name") == "physicians"
            and "checked" in element.attributes
        ]

    def test_focus_bar_is_compact_native_checkbox_control_below_global_navigation(self):
        self._select(self.first)

        response = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})
        content = response.content.decode()

        self.assertContains(response, "Physician focus:")
        self.assertContains(response, "Presentation only; schedule conclusions remain complete.")
        self.assertContains(response, '<details class="physician-scope-chooser">')
        self.assertContains(response, "Change physician focus")
        self.assertContains(response, 'type="checkbox" name="physicians"')
        self.assertContains(response, 'name="reset" value="1">Everyone</button>')
        self.assertContains(response, '<form class="physician-scope-reset" method="post"')
        chooser = content[
            content.index('<details class="physician-scope-chooser">') : content.index("</details>")
        ]
        self.assertNotIn('name="reset"', chooser)
        self.assertGreater(content.index("physician-scope-reset"), content.index("</details>"))
        self.assertNotContains(response, 'id="physician-scope-select"')
        self.assertLess(content.index("primary-navigation"), content.index("physician-scope"))

    def test_focus_preserves_preceptor_and_rounding_work_while_narrowing_names_and_casual_occurrences(
        self,
    ):
        self._select(self.first)
        self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})
        session = PreceptorSession.objects.get(date=date(2026, 8, 3), period_of_day="AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=self.second,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        NormalRounder.objects.create(week=date(2026, 8, 3), physician=self.second)
        NormalRounder.objects.create(week=date(2026, 8, 10), physician=self.first)
        CasualRounderRule.objects.create(physician=self.first, weekday=0, ordinals=[1])
        CasualRounderRule.objects.create(physician=self.second, weekday=0, ordinals=[1])

        monthly = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})
        rounding = self.client.get(reverse("rounding-list"), {"month": "2026-08"})

        self.assertEqual(monthly.context["assignment_count"], 1)
        self.assertContains(monthly, "Occupied")
        self.assertNotContains(monthly, "Assigned outside focus")
        self.assertEqual(
            len(rounding.context["rounding_list_rows"]),
            len(rounding.context["rounding_projection"].list_rows),
        )
        self.assertContains(rounding, "Assigned outside focus")
        self.assertContains(rounding, "First physician (Monday)")
        self.assertNotContains(rounding, "Second physician (Monday)")
        self.assertEqual(
            [
                occurrence.physician_name
                for item in rounding.context["rounding_list_rows"]
                for occurrence in item["casual_occurrences"]
            ],
            ["First physician"],
        )

    def test_focus_retains_incomplete_academic_and_interview_work_and_narrows_community(self):
        self._select(self.first)

        def event(name, required, participants):
            definition = AcademicEventDefinition.objects.create(
                base_name=name, start_academic_year=2026, expected_quarter="Q1"
            )
            occurrence = AcademicEvent.objects.create(
                definition=definition,
                academic_year=2026,
                base_name=name,
                expected_quarter="Q1",
                required_participant_count=required,
                resolution=AcademicEvent.Resolution.DATED,
                start_date=date(2026, 8, 3),
                end_date=date(2026, 8, 3),
            )
            occurrence.participants.set(participants)
            return occurrence

        underfilled = event("Underfilled", 2, (self.second,))
        unassigned = event("Unassigned", 1, ())
        event("Filled outside", 1, (self.second,))
        focused = event("Focused", 1, (self.first,))
        InterviewShift.objects.create(date=date(2026, 8, 3), physician=self.second)
        InterviewShift.objects.create(date=date(2026, 8, 4))
        CommunityPreceptorRule.objects.create(
            physician=self.first, weekday=0, ordinals=[1], periods=["AM"]
        )
        CommunityPreceptorRule.objects.create(
            physician=self.second, weekday=0, ordinals=[1], periods=["AM"]
        )

        academic = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        interviews = self.client.get(reverse("interview-list"), {"academic_year": "2026"})
        community = self.client.get(reverse("community-preceptor-rules"), {"month": "2026-08"})

        self.assertEqual(
            {event.pk for event in academic.context["academic_event_dated_events"]},
            {underfilled.pk, unassigned.pk, focused.pk},
        )
        academic_surface = self._surface_text(academic, "academic-event-list-section")
        self.assertIn("Underfilled", academic_surface)
        self.assertIn("Unassigned", academic_surface)
        self.assertNotIn("Filled outside", academic_surface)
        displayed_underfilled = next(
            event
            for event in academic.context["academic_event_dated_events"]
            if event.pk == underfilled.pk
        )
        self.assertEqual(displayed_underfilled.outside_focus_participant_count, 1)
        self.assertEqual(displayed_underfilled.assigned_count, 1)
        self.assertEqual(displayed_underfilled.required_participant_count, 2)
        self.assertEqual(len(interviews.context["interview_shifts"]), 2)
        self.assertIn(
            "Assigned outside focus", self._surface_text(interviews, "interview-list-section")
        )
        self.assertEqual(
            interviews.context["interview_shifts"][0].scope_assignment, "Assigned outside focus"
        )
        self.assertEqual(interviews.context["interview_summary"].active_count, 2)
        self.assertEqual(
            list(
                community.context["community_preceptor_rules"].values_list(
                    "physician_id", flat=True
                )
            ),
            [self.first.pk],
        )
        community_surface = self._surface_text(community, "community-preceptor-rules-section")
        self.assertIn("First physician", community_surface)
        self.assertNotIn("Second physician", community_surface)

    def test_focus_does_not_narrow_physician_pages_forms_or_history(self):
        self._select(self.first)
        create_vacation(
            physician=self.second,
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 3),
            scheduler=self.user,
        )

        directory = self.client.get(reverse("physicians-directory"))
        detail = self.client.get(reverse("physician-detail", args=(self.second.pk,)))
        vacation_form = self.client.get(reverse("vacation-create"))
        vacation_history = self.client.get(reverse("vacation-history"))

        self.assertContains(directory, "First physician")
        self.assertContains(directory, "Second physician")
        self.assertContains(detail, "Second physician")
        self.assertTrue(
            vacation_form.context["form"]
            .fields["physician"]
            .queryset.filter(pk=self.second.pk)
            .exists()
        )
        self.assertContains(vacation_history, "Second physician")
