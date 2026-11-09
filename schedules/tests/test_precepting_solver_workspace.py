import re
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

import schedules.services.precepting_solver as precepting_solver
from schedules.forms.solver import PreceptingSolveForm
from schedules.models import (
    ClinicHoliday,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    LateDocAssignment,
    LateDocAssignmentChange,
    NormalRounder,
    PreceptingSolverProposal,
    PreceptingSolverProposalComparison,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.models.physicians import Physician
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.precepting_solver import (
    SolveDateRange,
    balance_window_for_span,
)
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.services.rounding import record_normal_rounder
from schedules.tests.test_work_remaining import _RenderedSurfaceParser
from schedules.views import solver as solver_views


class PreceptingSolverWorkspaceTests(TestCase):
    week = date(2027, 2, 1)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.scheduler)
        self.enterContext(self.operational_date(date(2026, 1, 1)))

    @contextmanager
    def operational_date(self, today):
        # Keep solving, view defaults, and shared operational predicates on the
        # same date; solver-only patches leave holiday display at the host date.
        with (
            patch("schedules.services.precepting_solver.organization_today", return_value=today),
            patch("schedules.views.solver.organization_today", return_value=today),
            patch(
                "schedules.services.operational_attention.organization_today", return_value=today
            ),
        ):
            yield

    def faculty(self, name, availability="open"):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=date(2026, 1, 1),
            effective_end=None,
            slots={slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def ready_week(self, availability="open"):
        rounder = self.faculty("Rounder", availability)
        record_normal_rounder(week=self.week, physician=rounder, scheduler=self.scheduler)
        open_weekly_draft(self.week)
        return rounder

    def ready_range(self, start_date, end_date):
        rounder = self.faculty("Rounder")
        week = start_date - timedelta(days=start_date.weekday())
        while week <= end_date:
            record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
            open_weekly_draft(week)
            week += timedelta(days=7)
        return rounder

    def existing_normal_rounders(self, start_date, end_date):
        # These initialization tests start with source data, not user edits.
        # Recording a cross-month Rounding edit legitimately retains both months'
        # edit times before the solver runs, even without any persisted sessions.
        rounder = self.faculty("Rounder")
        week = start_date - timedelta(days=start_date.weekday())
        while week <= end_date:
            NormalRounder.objects.create(week=week, physician=rounder)
            week += timedelta(days=7)
        return rounder

    def solve(self, **values):
        data = {
            "mode": "fill",
            "range_kind": "custom",
            "start_date": self.week.isoformat(),
            "end_date": (self.week.replace(day=5)).isoformat(),
            "balance_span": "6-months",
        }
        data.update(values)
        return self.client.post(reverse("precepting-solver-workspace"), data)

    def proposal(self):
        self.ready_week()
        self.faculty("Second")
        response = self.solve()
        self.assertEqual(response.status_code, 302)
        return PreceptingSolverProposal.objects.get()

    def draft_state(self):
        return {
            "late_doc_assignments": list(
                LateDocAssignment.objects.order_by("pk").values_list(
                    "pk", "date", "physician_id", "created_at"
                )
            ),
            "late_doc_history": list(
                LateDocAssignmentChange.objects.order_by("pk").values_list(
                    "pk",
                    "assignment_id",
                    "assignment_id_at_recording",
                    "date",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "note",
                    "changed_by_id",
                    "changed_at",
                )
            ),
            "preceptor_assignments": list(
                PreceptorAssignment.objects.order_by("pk").values_list(
                    "pk",
                    "session_id",
                    "physician_id",
                    "responsibility",
                    "primary_position",
                    "is_locked",
                )
            ),
            "preceptor_history": list(
                PreceptorAssignmentChange.objects.order_by("pk").values_list(
                    "pk",
                    "session_id",
                    "assignment_id",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "reason",
                    "changed_by_id",
                    "changed_at",
                )
            ),
        }

    def retained_listing_proposal(
        self,
        *,
        created_at,
        applied=False,
        scheduler=None,
        solve_start_date=None,
        solve_end_date=None,
    ):
        solve_start_date = solve_start_date or self.week
        solve_end_date = solve_end_date or date(2027, 2, 5)
        proposal = PreceptingSolverProposal.objects.create(
            mode="fill",
            solve_start_date=solve_start_date,
            solve_end_date=solve_end_date,
            balance_start_date=date(2026, 8, 6),
            balance_end_date=date(2027, 2, 5),
            policy_version="test",
            solve_settings={},
            input_fingerprint="test",
            input_snapshot={"sessions": [], "late_doc_positions": []},
            proposed_assignments={"precepting": [], "late_doc": []},
            before_findings=[],
            after_findings=[],
            workload_effects={},
            compromises={},
            created_by=scheduler or self.scheduler,
            applied_at=created_at if applied else None,
        )
        PreceptingSolverProposal.objects.filter(pk=proposal.pk).update(created_at=created_at)
        return proposal

    def test_custom_range_selection_creates_a_reviewable_proposal(self):
        self.ready_week()
        self.faculty("Second")

        response = self.solve()

        self.assertRedirects(
            response,
            reverse("precepting-solver-review", args=(PreceptingSolverProposal.objects.get().pk,)),
        )
        proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual(
            (proposal.solve_start_date, proposal.solve_end_date), (self.week, date(2027, 2, 5))
        )
        review = self.client.get(response.url)
        content = review.content.decode()
        self.assertEqual(review.context["calendar_view"], "proposed")
        self.assertContains(review, "Proposed Precepting schedule")
        self.assertLess(content.index("Proposed schedule"), content.index("Week list"))
        self.assertLess(content.index("Week list"), content.index("Current schedule"))
        self.assertContains(
            self.client.get(response.url + "?view=changes"), "Proposed assignment changes"
        )

    def test_bounded_solver_failure_is_reviewable_feedback_without_a_proposal(self):
        self.ready_week()
        self.faculty("Second")
        message = (
            "Schedule Bot stopped at its 8.000s synchronous limit during Faculty fairness. "
            "Measured phases: model construction 0.125s; Faculty fairness 7.875s. "
            "No proposal was created."
        )

        with patch(
            "schedules.views.solver.produce_fill_proposal",
            side_effect=precepting_solver.PreceptingSolverTimeout(message),
        ):
            response = self.solve()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8.000s synchronous limit during Faculty fairness")
        self.assertContains(response, "No proposal was created.")
        self.assertFalse(PreceptingSolverProposal.objects.exists())

    def test_overlapping_month_clips_to_today_without_historical_setup_or_rounder_blockers(self):
        today = date(2026, 8, 26)
        first_actionable_week = date(2026, 8, 24)
        following_week = date(2026, 8, 31)
        with self.operational_date(today):
            setup = self.client.get(reverse("precepting-solver-workspace"))

        self.assertContains(setup, 'data-solve-today="2026-08-26"', html=False)
        self.assertContains(
            setup,
            "<output data-range-controls-effective-range>"
            "Aug 26, 2026 through Aug 31, 2026</output>",
            html=True,
        )
        rounder = self.faculty("Rounder")
        historical = self.faculty("Historical context")
        self.faculty("Second")
        record_normal_rounder(
            week=first_actionable_week, physician=rounder, scheduler=self.scheduler
        )
        record_normal_rounder(week=following_week, physician=rounder, scheduler=self.scheduler)
        open_weekly_draft(first_actionable_week)
        assign_preceptor(
            session=PreceptorSession.objects.get(date=first_actionable_week, period_of_day="AM"),
            physician=historical,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        with self.operational_date(today):
            response = self.client.post(
                reverse("precepting-solver-workspace"),
                {
                    "mode": "fill",
                    "range_kind": "month",
                    "month": "2026-08",
                    "balance_span": "6-months",
                },
            )

        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual(
            (proposal.solve_start_date, proposal.solve_end_date), (today, date(2026, 8, 31))
        )
        self.assertEqual(
            (proposal.balance_start_date, proposal.balance_end_date),
            (date(2026, 3, 1), date(2026, 8, 31)),
        )
        self.assertEqual(
            set(PreceptorSession.objects.filter(date__gte=today).values_list("date", flat=True)),
            {date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31)},
        )
        self.assertEqual(
            {session["date"] for session in proposal.input_snapshot["sessions"]},
            {"2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"},
        )

        for suffix in ("", "?view=current"):
            overview = self.client.get(response.url + suffix)
            calendar = overview.context["calendar"]

            self.assertEqual(
                [[day.iso_date for day in week.days] for week in calendar.weeks],
                [
                    ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
                    ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
                ],
            )
            context = next(
                item.content
                for week in calendar.weeks
                for lane in week.lanes
                if lane.label == "AM"
                for item in lane.items
                if item.content["date"] == "2026-08-24"
            )
            self.assertEqual(context["positions"][0]["value"], "Historical context")
            self.assertEqual(context["positions"][0]["state"], "optional-empty")
            self.assertFalse(context["positions"][0]["changed"])
            self.assertEqual(context["findings"], ())

        self.assertEqual(
            [week.action.url for week in self.client.get(response.url).context["calendar"].weeks],
            [
                reverse("precepting-solver-week", args=(proposal.pk, "2026-08-24")),
                reverse("precepting-solver-week", args=(proposal.pk, "2026-08-31")),
            ],
        )

        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, first_actionable_week.isoformat()))
        )

        self.assertEqual(
            [day.iso_date for day in proposal_week.context["calendar"].weeks[0].days],
            ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
        )
        self.assertNotContains(proposal_week, "/2026-08-24/AM/primary-1", html=False)
        self.assertNotContains(proposal_week, "/2026-08-25/AM/primary-1", html=False)
        historical_slot = (
            proposal_week.context["calendar"].weeks[0].lanes[0].items[0].content.slots[0]
        )
        self.assertEqual(
            (historical_slot.display, historical_slot.state, historical_slot.url),
            (
                "Historical context",
                "optional-empty",
                "",
            ),
        )

    def test_one_day_solve_restores_the_full_read_only_week_context(self):
        today = date(2026, 8, 26)
        rounder = self.faculty("Rounder")
        self.faculty("Second")
        record_normal_rounder(week=date(2026, 8, 24), physician=rounder, scheduler=self.scheduler)

        with self.operational_date(today):
            response = self.solve(start_date=today.isoformat(), end_date=today.isoformat())

        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual((proposal.solve_start_date, proposal.solve_end_date), (today, today))
        for suffix in ("", "?view=current"):
            calendar = self.client.get(response.url + suffix).context["calendar"]
            self.assertEqual(
                [day.iso_date for day in calendar.weeks[0].days],
                ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
            )
            self.assertFalse(calendar.weeks[0].days[0].in_selected_month)
            self.assertTrue(calendar.weeks[0].days[2].in_selected_month)

        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, "2026-08-24"))
        )
        self.assertEqual(
            [day.iso_date for day in proposal_week.context["calendar"].weeks[0].days],
            ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
        )

    def test_historical_selection_returns_no_action_without_initializing_or_retaining(self):
        with self.operational_date(date(2026, 8, 26)):
            response = self.client.post(
                reverse("precepting-solver-workspace"),
                {
                    "mode": "fill",
                    "range_kind": "custom",
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-25",
                    "balance_span": "6-months",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No current or future dates are selected.")
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptingSolverProposal.objects.exists())

    def test_oversized_solve_is_rejected_before_initialization_or_preflight(self):
        with (
            patch("schedules.views.solver.initialize_preceptor_session_range") as initialize,
            patch(
                "schedules.views.solver.preflight_precepting_solve",
                side_effect=AssertionError("oversized input must not preflight"),
            ),
        ):
            for values, message in (
                (
                    {"start_date": "2027-02-01", "end_date": "2027-05-01"},
                    "Schedule Bot accepts solve ranges of at most 3 calendar months.",
                ),
                (
                    {"balance_span": "custom", "balance_custom_days": 731},
                    "Schedule Bot accepts balance windows of at most 24 calendar months.",
                ),
            ):
                with self.subTest(values=values):
                    response = self.solve(**values)

                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, message)

        initialize.assert_not_called()
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())
        self.assertFalse(PreceptingSolverProposal.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())
        self.assertFalse(LateDocAssignmentChange.objects.exists())

    def test_repair_retains_current_unlocked_assignments_and_labels_them_for_review(self):
        self.ready_week()
        retained = self.faculty("Retained")
        self.faculty("Second")
        session = PreceptorSession.objects.get(date=self.week, period_of_day="AM")
        assignment = assign_preceptor(
            session=session,
            physician=retained,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        solver_input = precepting_solver.preflight_precepting_solve(
            SolveDateRange(self.week, date(2027, 2, 5))
        ).solver_input
        adapter = precepting_solver._CpSatFillAdapter(solver_input, repair=True)
        first_requirement = next(
            item
            for item in adapter.requirements
            if item.date == self.week
            and item.period_of_day == "AM"
            and item.position == "primary-1"
        )
        self.assertIn(
            retained.pk, [candidate.physician_id for candidate in first_requirement.candidates]
        )

        response = self.solve(mode="repair")

        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual(proposal.mode, "repair")
        self.assertEqual(
            [
                item
                for item in proposal.proposed_assignments["precepting"]
                if item["date"] == self.week.isoformat()
                and item["period_of_day"] == "AM"
                and item["position"] == "primary-1"
            ],
            [],
        )
        review = self.client.get(response.url)
        self.assertContains(review, "Review Repair findings proposal")
        self.assertContains(review, "Retained")
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician_id, retained.pk)

    def test_review_rows_label_required_gaps_without_changing_change_kinds(self):
        filled = self.retained_listing_proposal(created_at=timezone.now())
        filled.input_snapshot = {
            "sessions": [
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "positions": [{"key": "primary-1", "assignment": None}],
                }
            ],
            "late_doc_positions": [],
        }
        filled.proposed_assignments = {
            "precepting": [
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "position": "primary-1",
                    "physician_id": 1,
                    "physician_name": "Filled",
                    "explanation": {},
                }
            ],
            "late_doc": [],
        }
        filled.save(update_fields=("input_snapshot", "proposed_assignments"))

        fill_row = solver_views._review_groups(filled)[0][1][0]

        self.assertEqual(
            (fill_row["kind"], fill_row["current_physician_name"]), ("Addition", "Unfilled")
        )

        repair = self.retained_listing_proposal(created_at=timezone.now())
        repair.mode = PreceptingSolverProposal.Mode.REPAIR
        repair.input_snapshot = {
            "sessions": [
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "positions": [
                        {"key": "primary-1", "assignment": None},
                        {
                            "key": "primary-2",
                            "assignment": {"physician_id": 2, "physician_name": "Removed"},
                        },
                        {
                            "key": "secondary",
                            "assignment": {"physician_id": 3, "physician_name": "Optional"},
                        },
                    ],
                }
            ],
            "late_doc_positions": [{"date": self.week.isoformat(), "assignment": None}],
        }
        repair.proposed_assignments = {
            "precepting": [
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "position": "primary-1",
                    "physician_id": 1,
                    "physician_name": "Filled",
                    "explanation": {},
                },
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "position": "primary-2",
                    "physician_id": None,
                    "explanation": {},
                },
                {
                    "date": self.week.isoformat(),
                    "period_of_day": "AM",
                    "position": "secondary",
                    "physician_id": None,
                    "explanation": {},
                },
            ],
            "late_doc": [
                {
                    "date": self.week.isoformat(),
                    "position": "late-doc",
                    "physician_id": 4,
                    "physician_name": "Late Doc",
                    "explanation": {},
                }
            ],
        }
        repair.save(update_fields=("mode", "input_snapshot", "proposed_assignments"))

        repair_rows = {
            row["position"]: row for _, rows in solver_views._review_groups(repair) for row in rows
        }

        self.assertEqual(
            (repair_rows["primary-1"]["kind"], repair_rows["primary-1"]["current_physician_name"]),
            ("Added", "Unfilled"),
        )
        self.assertEqual(
            (repair_rows["primary-2"]["kind"], repair_rows["primary-2"]["physician_name"]),
            ("Removed", "Unfilled"),
        )
        self.assertEqual(repair_rows["secondary"]["physician_name"], "-")
        self.assertEqual(
            (repair_rows["Late Doc"]["kind"], repair_rows["Late Doc"]["current_physician_name"]),
            ("Added", "Unfilled Late Doc"),
        )

    def test_proposal_retains_all_setup_choices_and_reuse_prefills_without_preflight(self):
        self.ready_week()
        self.faculty("Second")
        excluded = self.faculty("Excluded")

        self.solve(
            balance_span="custom",
            balance_custom_days=12,
            excluded_physician_ids=[excluded.pk],
        )

        proposal = PreceptingSolverProposal.objects.get()
        solve_settings = dict(proposal.solve_settings)
        solve_seed = solve_settings.pop("solve_seed")
        self.assertEqual(
            solve_settings,
            {
                "mode": "fill",
                "range_kind": "custom",
                "month": None,
                "academic_year": None,
                "quarter": None,
                "start_date": "2027-02-01",
                "end_date": "2027-02-05",
                "balance_span": "custom",
                "balance_custom_days": 12,
                "excluded_physician_ids": [excluded.pk],
            },
        )
        self.assertRegex(solve_seed, r"^[0-9a-f]{32}$")
        with patch(
            "schedules.views.solver.preflight_precepting_solve",
            side_effect=AssertionError("reuse must not preflight"),
        ):
            reuse = self.client.get(f"{reverse('precepting-solver-workspace')}?reuse={proposal.pk}")

        self.assertEqual(reuse.status_code, 200)
        self.assertEqual(reuse.context["form"]["range_kind"].value(), "custom")
        self.assertEqual(reuse.context["form"]["start_date"].value(), date(2027, 2, 1))
        self.assertEqual(reuse.context["form"]["end_date"].value(), date(2027, 2, 5))
        self.assertEqual(reuse.context["form"]["balance_span"].value(), "custom")
        self.assertEqual(reuse.context["form"]["balance_custom_days"].value(), 12)
        self.assertIn(excluded.pk, reuse.context["form"]["excluded_physician_ids"].value())
        self.assertRegex(
            reuse.content.decode(),
            rf'type="checkbox" name="excluded_physician_ids" value="{excluded.pk}"[^>]* checked',
        )
        self.assertEqual(
            (reuse.context["solve_range"].start_date, reuse.context["solve_range"].end_date),
            (date(2027, 2, 1), date(2027, 2, 5)),
        )
        self.assertContains(
            self.client.get(reverse("precepting-solver-review", args=(proposal.pk,))),
            f"?reuse={proposal.pk}",
        )

    def test_exclusion_picker_renders_initial_and_invalid_submitted_selections(self):
        excluded = self.faculty("Excluded")
        initial_form = PreceptingSolveForm(initial={"excluded_physician_ids": [excluded.pk]})

        self.assertIsInstance(
            initial_form.fields["excluded_physician_ids"].widget, forms.CheckboxSelectMultiple
        )
        self.assertIn(excluded.pk, initial_form["excluded_physician_ids"].value())

        response = self.client.post(
            reverse("precepting-solver-workspace"),
            {
                "mode": "fill",
                "range_kind": "custom",
                "end_date": self.week.replace(day=5).isoformat(),
                "balance_span": "6-months",
                "excluded_physician_ids": [excluded.pk],
            },
        )

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a start date.")
        self.assertContains(response, "1 selected physician.")
        self.assertContains(response, "Choose the physicians to exclude from this proposal.")
        self.assertContains(response, "Filter physicians")
        self.assertContains(response, "Selected physicians")
        self.assertContains(response, "Click on a name to remove it.")
        self.assertContains(response, 'src="/static/schedules/physicians-picker.js"')
        self.assertRegex(
            content,
            rf'type="checkbox" name="excluded_physician_ids" value="{excluded.pk}"[^>]* checked',
        )
        self.assertNotIn('<select name="excluded_physician_ids"', content)

    def test_landing_shows_five_shared_unapplied_proposals_without_solver_work(self):
        now = timezone.now()
        other_scheduler = get_user_model().objects.create_user(
            username="other-scheduler", password="test-password"
        )
        proposals = [
            self.retained_listing_proposal(
                created_at=now - timedelta(minutes=index),
                scheduler=other_scheduler if index == 1 else None,
                solve_start_date=self.week + timedelta(days=index * 7),
                solve_end_date=self.week + timedelta(days=index * 7 + 4),
            )
            for index in range(6)
        ]
        applied = self.retained_listing_proposal(
            created_at=now + timedelta(minutes=1), applied=True
        )

        with (
            patch(
                "schedules.views.solver.preflight_precepting_solve",
                side_effect=AssertionError("landing must not preflight"),
            ),
            patch(
                "schedules.views.solver.produce_fill_proposal",
                side_effect=AssertionError("landing must not solve"),
            ),
        ):
            response = self.client.get(reverse("precepting-solver-workspace"))

        listed = list(response.context["recent_proposals"])
        self.assertEqual(
            [proposal.pk for proposal in listed], [proposal.pk for proposal in proposals[:5]]
        )
        self.assertNotIn(applied.pk, [proposal.pk for proposal in listed])
        self.assertContains(response, "Recent unapplied proposals")
        self.assertContains(response, 'id="recent-proposals"', html=False)
        self.assertContains(
            response, 'class="table-scroll-frame data-table-scroll-frame"', html=False
        )
        content = response.content.decode()
        self.assertEqual(content.count('class="workspace-surface-stack"'), 1)
        self.assertNotIn("<h1>Schedule Bot</h1>", content)
        self.assertLess(content.index('id="recent-proposals"'), content.index("Make new proposal"))
        self.assertNotIn('class="recent-proposal', content)
        parser = _RenderedSurfaceParser()
        parser.feed(content)
        recent_proposals_table = next(
            element
            for element in parser.elements
            if element.tag == "table" and element.attributes.get("id") == "recent-proposals-table"
        )
        header_row = next(
            row
            for row in recent_proposals_table.descendants()
            if row.tag == "tr"
            and any(
                cell.tag == "th" and cell.attributes.get("scope") == "col"
                for cell in row.children
                if not isinstance(cell, str)
            )
        )
        headers = [
            cell.text()
            for cell in header_row.children
            if not isinstance(cell, str) and cell.tag == "th"
        ]
        # Recent proposals identify each row first by its plain-text solve range.
        self.assertEqual(headers[0], "Solve range")
        self.assertTrue({"Status", "Actions"}.issubset(headers))
        rows = [
            row
            for row in recent_proposals_table.descendants()
            if row.tag == "tr" and row is not header_row
        ]
        for proposal in proposals[:5]:
            solve_range = (
                f"{proposal.solve_start_date:%b} {proposal.solve_start_date.day}, "
                f"{proposal.solve_start_date:%Y} through "
                f"{proposal.solve_end_date:%b} {proposal.solve_end_date.day}, "
                f"{proposal.solve_end_date:%Y}"
            )
            row = next(
                row
                for row in rows
                if any(
                    cell.tag == "th"
                    and cell.attributes.get("scope") == "row"
                    and cell.text() == solve_range
                    for cell in row.children
                    if not isinstance(cell, str)
                )
            )
            row_cells = [
                cell
                for cell in row.children
                if not isinstance(cell, str) and cell.tag in {"th", "td"}
            ]
            row_by_header = dict(zip(headers, row_cells, strict=True))
            row_links = [
                element for element in row_by_header["Actions"].descendants() if element.tag == "a"
            ]
            self.assertEqual(row_by_header["Solve range"].text(), solve_range)
            self.assertFalse(
                any(element.tag == "a" for element in row_by_header["Solve range"].descendants()),
                "the solve range is row data, not its review action",
            )
            self.assertIn("Current", row_by_header["Status"].text())
            self.assertIn("Unapplied", row_by_header["Status"].text())
            self.assertIn(
                ("Continue review", reverse("precepting-solver-review", args=(proposal.pk,))),
                [(link.text(), link.attributes.get("href")) for link in row_links],
            )
            self.assertIn(
                ("Reuse settings", f"{reverse('precepting-solver-workspace')}?reuse={proposal.pk}"),
                [(link.text(), link.attributes.get("href")) for link in row_links],
            )
        other_scheduler_range = (
            f"{proposals[1].solve_start_date:%b} {proposals[1].solve_start_date.day}, "
            f"{proposals[1].solve_start_date:%Y} through "
            f"{proposals[1].solve_end_date:%b} {proposals[1].solve_end_date.day}, "
            f"{proposals[1].solve_end_date:%Y}"
        )
        other_scheduler_row = next(
            row
            for row in rows
            if any(
                cell.tag == "th"
                and cell.attributes.get("scope") == "row"
                and cell.text() == other_scheduler_range
                for cell in row.children
                if not isinstance(cell, str)
            )
        )
        other_scheduler_cells = [
            cell
            for cell in other_scheduler_row.children
            if not isinstance(cell, str) and cell.tag in {"th", "td"}
        ]
        other_scheduler_by_header = dict(zip(headers, other_scheduler_cells, strict=True))
        self.assertIn("other-scheduler", other_scheduler_by_header["Created"].text())
        self.assertIn(
            reverse("precepting-solver-review", args=(proposals[1].pk,)),
            other_scheduler_by_header["Actions"].action_targets(),
        )

    def test_month_and_quarter_range_selections_create_reviewable_proposals(self):
        self.ready_range(date(2027, 1, 1), date(2027, 3, 31))
        self.ready_range(date(2027, 7, 1), date(2027, 9, 30))
        self.faculty("Second")

        month = self.client.post(
            reverse("precepting-solver-workspace"),
            {
                "mode": "fill",
                "range_kind": "month",
                "month": "2027-02",
                "balance_span": "6-months",
            },
        )
        self.assertEqual(month.status_code, 302)
        month_proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual(
            (month_proposal.solve_start_date, month_proposal.solve_end_date),
            (date(2027, 2, 1), date(2027, 2, 28)),
        )
        self.assertEqual(
            (month_proposal.balance_start_date, month_proposal.balance_end_date),
            (date(2026, 8, 29), date(2027, 2, 28)),
        )
        self.assertEqual(month_proposal.mode, PreceptingSolverProposal.Mode.FILL)
        self.assertEqual(
            {
                key: month_proposal.solve_settings[key]
                for key in ("mode", "range_kind", "month", "balance_span", "excluded_physician_ids")
            },
            {
                "mode": "fill",
                "range_kind": "month",
                "month": "2027-02-01",
                "balance_span": "6-months",
                "excluded_physician_ids": [],
            },
        )
        month_seed = month_proposal.solve_settings["solve_seed"]
        self.assertEqual(self.client.get(month.url).status_code, 200)
        month_proposal.refresh_from_db()
        self.assertEqual(month_proposal.solve_settings["solve_seed"], month_seed)

        quarter = self.client.post(
            reverse("precepting-solver-workspace"),
            {
                "mode": "fill",
                "range_kind": "quarter",
                "academic_year": 2027,
                "quarter": 1,
                "balance_span": "6-months",
            },
        )
        self.assertEqual(quarter.status_code, 302)
        quarter_proposal = PreceptingSolverProposal.objects.latest("pk")
        self.assertEqual(
            (quarter_proposal.solve_start_date, quarter_proposal.solve_end_date),
            (date(2027, 7, 1), date(2027, 9, 30)),
        )
        self.assertEqual(
            (quarter_proposal.balance_start_date, quarter_proposal.balance_end_date),
            (date(2027, 3, 31), date(2027, 9, 30)),
        )
        self.assertEqual(quarter_proposal.mode, PreceptingSolverProposal.Mode.FILL)
        self.assertEqual(
            {
                key: quarter_proposal.solve_settings[key]
                for key in (
                    "mode",
                    "range_kind",
                    "academic_year",
                    "quarter",
                    "balance_span",
                    "excluded_physician_ids",
                )
            },
            {
                "mode": "fill",
                "range_kind": "quarter",
                "academic_year": 2027,
                "quarter": 1,
                "balance_span": "6-months",
                "excluded_physician_ids": [],
            },
        )
        quarter_seed = quarter_proposal.solve_settings["solve_seed"]
        self.assertEqual(self.client.get(quarter.url).status_code, 200)
        reuse = self.client.get(
            f"{reverse('precepting-solver-workspace')}?reuse={quarter_proposal.pk}"
        )
        self.assertEqual(reuse.context["form"]["academic_year"].value(), 2027)
        self.assertEqual(reuse.context["form"]["quarter"].value(), 1)
        quarter_proposal.refresh_from_db()
        self.assertEqual(quarter_proposal.solve_settings["solve_seed"], quarter_seed)

    def test_default_quarter_uses_the_clinic_current_academic_year(self):
        with self.operational_date(date(2027, 1, 1)):
            response = self.client.get(reverse("precepting-solver-workspace"))

        self.assertEqual(response.context["form"]["month"].value(), date(2027, 1, 1))
        self.assertEqual(response.context["form"]["academic_year"].value(), 2026)
        self.assertEqual(response.context["form"]["quarter"].value(), 3)
        self.assertContains(response, 'name="academic_year"', html=False)
        self.assertContains(response, "2026-2027")
        self.assertNotContains(response, "quarter_year")

    def test_month_range_link_uses_fresh_defaults_and_keeps_the_requested_month(self):
        with self.operational_date(date(2027, 2, 15)):
            response = self.client.get(
                f"{reverse('precepting-solver-workspace')}?range_kind=month&month=2027-02"
            )

        form = response.context["form"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(form["range_kind"].value(), "month")
        self.assertEqual(form["month"].value(), date(2027, 2, 1))
        self.assertEqual(form["mode"].value(), "fill")
        self.assertEqual(form["balance_span"].value(), "6-months")
        self.assertIsNone(form["balance_custom_days"].value())
        self.assertIsNone(form["excluded_physician_ids"].value())
        self.assertEqual(
            (response.context["solve_range"].start_date, response.context["solve_range"].end_date),
            (date(2027, 2, 15), date(2027, 2, 28)),
        )
        self.assertFalse(PreceptingSolverProposal.objects.exists())

    def test_direct_schedule_bot_visit_keeps_its_ordinary_default_month(self):
        with self.operational_date(date(2028, 9, 18)):
            response = self.client.get(reverse("precepting-solver-workspace"))

        self.assertEqual(response.context["form"]["range_kind"].value(), "month")
        self.assertEqual(response.context["form"]["month"].value(), date(2028, 9, 1))

    def test_reused_month_takes_precedence_over_the_clinic_current_month(self):
        proposal = self.retained_listing_proposal(created_at=timezone.now())
        proposal.solve_settings = {
            "mode": "fill",
            "range_kind": "month",
            "month": "2027-02-01",
            "academic_year": None,
            "quarter": None,
            "start_date": None,
            "end_date": None,
            "balance_span": "6-months",
            "balance_custom_days": None,
            "excluded_physician_ids": [],
        }
        proposal.save(update_fields=("solve_settings",))

        with self.operational_date(date(2028, 9, 18)):
            response = self.client.get(
                f"{reverse('precepting-solver-workspace')}?reuse={proposal.pk}"
            )

        self.assertEqual(response.context["form"]["month"].value(), date(2027, 2, 1))

    def test_future_month_solve_initializes_normal_draft_structure_without_schedule_mutations(self):
        month = date(2027, 3, 1)
        self.existing_normal_rounders(month, date(2027, 3, 31))
        self.faculty("First")
        self.faculty("Second")
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())

        response = self.client.post(
            reverse("precepting-solver-workspace"),
            {
                "mode": "fill",
                "range_kind": "month",
                "month": "2027-03",
                "balance_span": "6-months",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(SchedulePeriod.objects.values_list("month", flat=True)), [month])
        self.assertEqual(PreceptorSession.objects.filter(period__month=month).count(), 46)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())
        self.assertFalse(LateDocAssignmentChange.objects.exists())
        self.assertIsNone(SchedulePeriod.objects.get(month=month).last_edited_at)

    def test_custom_solve_initializes_each_intersecting_month_and_repeated_solves_are_idempotent(
        self,
    ):
        start_date = date(2027, 1, 29)
        end_date = date(2027, 3, 2)
        self.existing_normal_rounders(start_date, end_date)
        self.faculty("First")
        self.faculty("Second")
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())
        data = {
            "mode": "fill",
            "range_kind": "custom",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "balance_span": "6-months",
        }

        first = self.client.post(reverse("precepting-solver-workspace"), data)
        session_count = PreceptorSession.objects.count()
        repeated = self.client.post(reverse("precepting-solver-workspace"), data)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)),
            [date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)],
        )
        self.assertEqual(PreceptorSession.objects.count(), session_count)
        self.assertFalse(SchedulePeriod.objects.filter(last_edited_at__isnull=False).exists())

    def test_invalid_or_anonymous_solve_does_not_initialize_sessions(self):
        invalid = self.client.post(
            reverse("precepting-solver-workspace"),
            {"mode": "fill", "range_kind": "month", "balance_span": "6-months"},
        )

        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(SchedulePeriod.objects.exists())
        self.client.logout()
        anonymous = self.client.post(
            reverse("precepting-solver-workspace"),
            {
                "mode": "fill",
                "range_kind": "month",
                "month": "2027-03",
                "balance_span": "6-months",
            },
        )

        self.assertEqual(anonymous.status_code, 302)
        self.assertFalse(SchedulePeriod.objects.exists())

    def test_blocked_or_invalid_solve_shows_named_feedback_before_setup_controls(self):
        self.retained_listing_proposal(created_at=timezone.now())
        blocked = self.solve()

        self.assertEqual(blocked.status_code, 200)
        content = blocked.content.decode()
        self.assertContains(blocked, 'id="solve-feedback"', html=False)
        self.assertContains(blocked, 'role="alert"', html=False)
        self.assertContains(blocked, "No normal rounder is recorded for a solve week.")
        self.assertLess(content.index('id="solve-feedback"'), content.index("solve-mode-heading"))
        self.assertLess(
            content.index('id="recent-proposals"'), content.index('id="solve-feedback"')
        )
        self.assertLess(content.index("Make new proposal"), content.index('id="solve-feedback"'))
        self.assertLess(
            content.index('id="recent-proposals"'), content.index("Choose the work to fill.")
        )
        self.assertLess(content.index('id="solve-feedback"'), content.index("Run Schedule Bot"))

        invalid = self.client.post(
            reverse("precepting-solver-workspace"),
            {"mode": "fill", "range_kind": "month", "balance_span": "6-months"},
        )

        self.assertContains(
            invalid, "Correct the highlighted setup fields before running Schedule Bot."
        )
        self.assertContains(invalid, "Month:")

    def test_setup_configures_compact_period_and_balance_controls(self):
        response = self.client.get(reverse("precepting-solver-workspace"))

        self.assertNotContains(response, "Recent unapplied proposals")
        self.assertNotContains(response, "No unapplied proposals are retained.")
        self.assertEqual(
            response.context["form"]["month"].field.widget.attrs["class"], "field-width--compact"
        )
        self.assertEqual(
            response.context["form"]["balance_span"].field.widget.attrs["class"],
            "field-width--compact",
        )
        self.assertContains(response, 'data-solve-step-month="-1"', html=False)
        self.assertContains(response, 'data-solve-step-academic-year="1"', html=False)
        self.assertContains(
            response,
            'class="toolbar period-navigation period-navigation-cluster solver-period-navigation '
            'period-navigation--academic-year" aria-label="Academic year navigation">',
            html=False,
        )
        self.assertContains(response, 'data-solve-step-quarter="1"', html=False)
        self.assertContains(response, 'aria-label="Academic year navigation"', html=False)
        self.assertContains(response, 'aria-label="Quarter navigation"', html=False)
        self.assertContains(response, "Academic year")
        self.assertContains(response, "Quarter")

    def test_no_javascript_range_selections_render_the_selected_effective_range(self):
        selections = (
            (
                "month",
                {"range_kind": "month", "month": "2027-02"},
                (date(2027, 2, 1), date(2027, 2, 28)),
            ),
            (
                "quarter",
                {"range_kind": "quarter", "academic_year": 2027, "quarter": 1},
                (date(2027, 7, 1), date(2027, 9, 30)),
            ),
            (
                "custom",
                {
                    "range_kind": "custom",
                    "start_date": "2027-02-01",
                    "end_date": "2027-02-05",
                },
                (date(2027, 2, 1), date(2027, 2, 5)),
            ),
        )

        for range_kind, selection, (start_date, end_date) in selections:
            with self.subTest(range_kind=range_kind):
                response = self.client.post(
                    reverse("precepting-solver-workspace"),
                    {"mode": "fill", "balance_span": "6-months", **selection},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response,
                    "<output data-range-controls-effective-range>"
                    f"{date_format(start_date, 'M j, Y')} through "
                    f"{date_format(end_date, 'M j, Y')}</output>",
                    html=True,
                )

    def test_setup_uses_the_default_six_month_balance_span(self):
        with self.operational_date(date(2027, 2, 15)):
            response = self.client.get(reverse("precepting-solver-workspace"))

        self.assertEqual(response.context["form"]["balance_span"].value(), "6-months")
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        balance_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and element.attributes.get("aria-labelledby") == "solve-balance-heading"
        ]
        self.assertEqual(len(balance_sections), 1)
        balance_outputs = [
            element
            for element in balance_sections[0].descendants()
            if element.tag == "output" and "data-solve-balance-range" in element.attributes
        ]
        self.assertEqual(len(balance_outputs), 1)
        self.assertEqual(
            re.findall(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", balance_outputs[0].text()),
            ["Aug 29, 2026", "Feb 28, 2027"],
        )

    def test_each_named_balance_span_derives_an_inclusive_window_on_the_server(self):
        solve_range = (date(2027, 2, 1), date(2027, 2, 5))
        self.ready_range(*solve_range)
        self.faculty("Second")
        expected_starts = {
            "1-day": date(2027, 2, 5),
            "1-week": date(2027, 1, 30),
            "2-weeks": date(2027, 1, 23),
            "4-weeks": date(2027, 1, 9),
            "1-month": date(2027, 1, 6),
            "3-months": date(2026, 11, 6),
            "6-months": date(2026, 8, 6),
            "1-year": date(2026, 2, 6),
        }

        for span, expected_start in expected_starts.items():
            with self.subTest(span=span):
                response = self.solve(balance_span=span)
                self.assertEqual(response.status_code, 302)
                proposal = PreceptingSolverProposal.objects.latest("pk")
                self.assertEqual(
                    (proposal.balance_start_date, proposal.balance_end_date),
                    (expected_start, solve_range[1]),
                )

    def test_calendar_balance_spans_use_inclusive_calendar_boundaries(self):
        self.assertEqual(
            balance_window_for_span(
                SolveDateRange(date(2028, 2, 1), date(2028, 2, 29)), "1-month"
            ).start_date,
            date(2028, 1, 30),
        )
        self.assertEqual(
            balance_window_for_span(
                SolveDateRange(date(2028, 2, 1), date(2028, 2, 29)), "1-year"
            ).start_date,
            date(2027, 3, 1),
        )

    def test_custom_balance_span_is_derived_and_retained_with_the_proposal(self):
        self.ready_week()
        self.faculty("Second")

        response = self.solve(
            **{
                "balance_span": "custom",
                "balance_custom_days": 12,
            }
        )

        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        self.assertEqual(
            (proposal.balance_start_date, proposal.balance_end_date),
            (date(2027, 1, 25), date(2027, 2, 5)),
        )

    def test_incomplete_infeasible_proposal_remains_reviewable(self):
        self.ready_week(availability="off")

        response = self.solve()

        self.assertEqual(response.status_code, 302)
        review = self.client.get(response.url)
        self.assertContains(review, "Unresolved work")
        self.assertContains(review, "Preceptor 1 is unfilled.")
        self.assertContains(review, "Preceptor 2 is unfilled.")

    def test_fill_reduces_required_position_findings_one_assignment_at_a_time(self):
        rounder = self.ready_week()
        self.faculty("Eligible")

        response = self.solve(excluded_physician_ids=[rounder.pk])

        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        before = [
            finding
            for finding in proposal.before_findings
            if finding["code"] == "missing_primary_position"
        ]
        after = [
            finding
            for finding in proposal.after_findings
            if finding["code"] == "missing_primary_position"
        ]
        self.assertEqual(len(before), 20)
        self.assertEqual(len(after), 10)
        self.assertTrue(
            all(
                finding["explanation"]["position"] in {"primary-1", "primary-2"}
                for finding in after
            )
        )

    def test_review_shows_unresolved_counts_and_disclosed_before_and_after_findings(self):
        proposal = self.proposal()
        proposal.before_findings = [
            {
                "date": "2027-02-01",
                "code": "missing_primary_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "explanation": {},
            },
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            },
        ]
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "not_available_assignment",
                "severity": "hard",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "PM",
                "explanation": {"position": "secondary"},
            },
            {
                "date": "2027-02-02",
                "code": "missing_normal_rounder",
                "severity": "hard",
                "period_of_day": None,
                "explanation": {},
            },
        ]
        proposal.save(update_fields=("before_findings", "after_findings"))

        review = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))
        content = review.content.decode()

        self.assertContains(review, "<h2>Unresolved work</h2>", html=False)
        self.assertIn(
            '<span class="finding finding-hard">Hard count: 2</span>',
            content,
        )
        self.assertIn(
            '<span class="finding finding-preference">Preference count: 1</span>',
            content,
        )
        self.assertIn(
            '<details class="solver-review-subsection"><summary>Before findings '
            "(1 hard, 1 preference)</summary>",
            content,
        )
        self.assertIn(
            '<details class="solver-review-subsection" open><summary>After findings '
            "(2 hard, 1 preference)</summary>",
            content,
        )
        self.assertLess(content.index("Before findings"), content.index("After findings"))
        self.assertContains(review, "One or both required Preceptor positions are unfilled.")
        self.assertContains(review, "Assigned physician is not available for this session.")
        self.assertNotContains(review, "not_available_assignment")

    def test_review_tones_unresolved_work_from_after_finding_severity(self):
        proposal = self.proposal()
        review_url = reverse("precepting-solver-review", args=(proposal.pk,))
        proposal.after_findings = []
        proposal.save(update_fields=("after_findings",))

        clear = self.client.get(review_url)
        self.assertContains(
            clear,
            'class="workspace-surface surface-frame solver-review-unresolved"',
            html=False,
        )

        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            }
        ]
        proposal.save(update_fields=("after_findings",))
        preference = self.client.get(review_url)
        self.assertContains(
            preference,
            'class="workspace-surface surface-frame surface-tone--preference '
            'solver-review-unresolved"',
            html=False,
        )

        proposal.after_findings.append(
            {
                "date": "2027-02-01",
                "code": "not_available_assignment",
                "severity": "hard",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            }
        )
        proposal.save(update_fields=("after_findings",))
        hard = self.client.get(review_url)
        self.assertContains(
            hard,
            'class="workspace-surface surface-frame surface-tone--hard solver-review-unresolved"',
            html=False,
        )

    def test_after_finding_provenance_uses_position_and_physician_scope(self):
        proposal = self.proposal()
        proposal.before_findings = [
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [101],
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-01",
                "code": "missing_primary_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [],
                "explanation": {"missing_primary_positions": [1]},
            },
        ]
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [101],
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "AM",
                "physician_ids": [101],
                "explanation": {"position": "primary-2"},
            },
            {
                "date": "2027-02-01",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "AM",
                "physician_ids": [102],
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-01",
                "code": "missing_primary_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [],
                "explanation": {"missing_primary_positions": [2]},
            },
        ]
        proposal.save(update_fields=("before_findings", "after_findings"))

        response = self.client.get(
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?view=changes"
        )

        self.assertEqual(
            [finding["provenance"] for finding in response.context["after_findings"]],
            [
                "Existing",
                "Proposed",
                "Proposed",
                "Proposed",
            ],
        )
        self.assertContains(
            response,
            'title="Existed before this proposal." aria-label="Existing: Existed before this proposal.">Existing</span>',
            count=2,
        )
        self.assertContains(
            response,
            'title="Introduced by this proposal." aria-label="Proposed: Introduced by this proposal.">Proposed</span>',
            count=6,
        )
        content = response.content.decode()
        self.assertIn(
            'aria-label="Existing: Existed before this proposal.">Existing</span>'
            '<span class="finding finding-hard">',
            content,
        )
        self.assertIn(
            'aria-label="Existing: Existed before this proposal.">Existing</span>'
            "<strong>Hard:</strong>",
            content,
        )

    def test_after_missing_primary_coverage_is_existing_when_a_baseline_gap_remains(self):
        proposal = self.proposal()
        proposal.before_findings = [
            {
                "date": "2027-02-01",
                "code": "missing_primary_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [],
                "explanation": {"missing_primary_positions": [1, 2]},
            },
        ]
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "missing_primary_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [],
                "explanation": {"missing_primary_positions": [2]},
            },
        ]
        proposal.save(update_fields=("before_findings", "after_findings"))

        response = self.client.get(
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?view=changes"
        )

        self.assertEqual(response.context["after_findings"][0]["provenance"], "Existing")

    def test_after_insufficient_faculty_coverage_is_existing_when_fill_adds_one_faculty(self):
        proposal = self.proposal()
        proposal.before_findings = [
            {
                "date": "2027-02-01",
                "code": "insufficient_faculty_equivalent_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [],
                "explanation": {
                    "required_count": 2,
                    "assigned_count": 0,
                    "faculty_equivalent_count": 0,
                },
            },
        ]
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "insufficient_faculty_equivalent_coverage",
                "severity": "hard",
                "period_of_day": "AM",
                "physician_ids": [101],
                "explanation": {
                    "required_count": 2,
                    "assigned_count": 1,
                    "faculty_equivalent_count": 1,
                },
            },
        ]
        proposal.save(update_fields=("before_findings", "after_findings"))

        response = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        self.assertEqual(response.context["after_findings"][0]["provenance"], "Existing")

    def test_after_findings_target_the_matching_proposed_calendar_location_and_keep_delta_visible(
        self,
    ):
        proposal = self.proposal()
        proposed_assignment = proposal.proposed_assignments["precepting"][0]
        on_date = proposed_assignment["date"]
        period_of_day = proposed_assignment["period_of_day"]
        position = proposed_assignment["position"]
        proposal.after_findings = [
            {
                "date": on_date,
                "code": "not_available_assignment",
                "severity": "hard",
                "period_of_day": period_of_day,
                "explanation": {"position": position},
            },
            {
                "date": "2027-02-01",
                "code": "insufficient_faculty_equivalent_coverage",
                "severity": "hard",
                "period_of_day": "PM",
                "explanation": {},
            },
            {
                "date": "2027-02-01",
                "code": "missing_normal_rounder",
                "severity": "hard",
                "period_of_day": None,
                "explanation": {},
            },
        ]
        proposal.save(update_fields=("after_findings",))

        review = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))
        proposed = self.client.get(
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?view=proposed"
        )
        content = proposed.content.decode()
        primary = next(
            slot
            for week in proposed.context["calendar"].weeks
            for lane in week.lanes
            if lane.label == period_of_day
            for item in lane.items
            if item.content.get("date") == on_date
            for slot in item.content["positions"]
            if slot["label"]
            == f"Preceptor {proposed_assignment['position'].removeprefix('primary-')}"
        )

        self.assertEqual(primary["state"], "conflict")
        self.assertTrue(primary["changed"])
        self.assertIn(f'id="proposal-position-{on_date}-{period_of_day}-{position}"', content)
        self.assertIn('id="proposal-session-2027-02-01-PM"', content)
        self.assertIn('id="proposal-week-2027-02-01"', content)
        self.assertIn("solver-calendar-position--changed", content)
        self.assertIn("solver-calendar-delta", content)
        target_item = re.search(
            rf'<li id="proposal-position-{on_date}-{period_of_day}-{position}".*?</li>', content
        )
        self.assertIsNotNone(target_item)
        self.assertIn("Hard conflict", target_item[0])
        self.assertNotIn("Conflict:", target_item[0])
        self.assertNotIn("finding", target_item[0])
        self.assertContains(
            review, f"?view=proposed#proposal-position-{on_date}-{period_of_day}-{position}"
        )
        self.assertContains(review, "?view=proposed#proposal-session-2027-02-01-PM")
        self.assertContains(review, "?view=proposed#proposal-week-2027-02-01")

    def test_current_and_proposed_calendars_use_before_and_after_slot_findings(self):
        self.ready_week()
        current_physician = self.faculty("Current physician")
        self.faculty("Second")
        session = PreceptorSession.objects.get(date=self.week, period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=current_physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        finding = {
            "date": self.week.isoformat(),
            "period_of_day": "AM",
            "explanation": {"position": "primary-1"},
        }
        proposal.before_findings = [
            {**finding, "code": "backup_only_assignment", "severity": "preference"}
        ]
        proposal.after_findings = [
            {**finding, "code": "not_available_assignment", "severity": "hard"}
        ]
        proposal.save(update_fields=("before_findings", "after_findings"))

        current = self.client.get(response.url + "?view=current")
        proposed = self.client.get(response.url + "?view=proposed")

        def slot(response):
            return next(
                position
                for week in response.context["calendar"].weeks
                for lane in week.lanes
                if lane.label == "AM"
                for item in lane.items
                if item.content.get("date") == self.week.isoformat()
                for position in item.content["positions"]
                if position["label"] == "Preceptor 1"
            )

        self.assertEqual(
            (slot(current)["state"], slot(current)["display"]), ("preference", "Current physician")
        )
        self.assertEqual(
            (slot(proposed)["state"], slot(proposed)["display"]), ("conflict", "Current physician")
        )
        self.assertEqual(slot(current)["finding_tone"], "preference")
        self.assertEqual(slot(proposed)["finding_tone"], "hard")
        self.assertNotContains(current, "Check: Current physician")
        self.assertNotContains(proposed, "Conflict: Current physician")
        self.assertContains(current, "Non-blocking finding: Current physician")
        self.assertContains(proposed, "Hard conflict: Current physician")
        self.assertContains(current, "month-slot-preference month-slot--preference")
        self.assertContains(proposed, "month-slot-conflict month-slot--hard")

    def test_changes_by_week_includes_after_findings_on_unchanged_positions_and_gaps(self):
        proposal = self.proposal()
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "missing_primary_position",
                "severity": "hard",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-02",
                "code": "backup_only_assignment",
                "severity": "preference",
                "period_of_day": "PM",
                "explanation": {"position": "secondary"},
            },
        ]
        proposal.save(update_fields=("after_findings",))

        response = self.client.get(
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?view=changes"
        )
        finding_rows = [
            row["finding"]
            for _, rows in response.context["review_groups"]
            for row in rows
            if row["finding"]
        ]

        self.assertEqual(len(finding_rows), 2)
        self.assertContains(response, "Preceptor 1 is unfilled.")
        self.assertContains(
            response, "Assigned physician is available as backup only for this session."
        )
        self.assertContains(response, "Show in proposed schedule", count=4)

    def test_review_uses_grouped_disclosures_and_bulleted_explanations(self):
        end_date = date(2027, 2, 12)
        self.ready_range(self.week, end_date)
        self.faculty("First")
        self.faculty("Second")
        third = self.faculty("Third")
        self.faculty("Backup", availability="backup_only")
        unavailable = self.faculty("Unavailable", availability="off")

        response = self.solve(end_date=end_date.isoformat())
        proposal = PreceptingSolverProposal.objects.get()
        proposal.proposed_assignments["precepting"][0]["explanation"][
            "other_normally_available_alternatives"
        ] = [
            {
                "physician_id": third.pk,
                "physician_name": third.display_name,
                "reason": "Normally available; the whole proposal may have different policy tradeoffs.",
            }
        ]
        proposal.save(update_fields=("proposed_assignments",))
        review = self.client.get(response.url + "?view=changes")
        content = review.content.decode()

        self.assertEqual(len(review.context["review_groups"]), 2)
        self.assertContains(
            review, '<details class="solver-review-week" open><summary>Week of ', html=False
        )
        self.assertContains(
            review, '<details class="solver-review-week"><summary>Week of ', html=False
        )
        self.assertContains(
            review,
            '<section class="workspace-surface surface-frame solver-review-unresolved">',
            html=False,
        )
        self.assertContains(
            review,
            '<details class="solver-review-section"><summary>Workload effects</summary>',
            html=False,
        )
        self.assertIn(
            '<details class="solver-review-compact-disclosure"><summary>Why this?</summary><h3>Hard exclusions</h3><ul>',
            content,
        )
        self.assertIn(
            'class="solver-review-compact-disclosure"><summary>Other eligible people (', content
        )
        self.assertIn('<optgroup label="Other normally available">', content)
        self.assertIn('<optgroup label="Backup only">', content)
        self.assertNotIn(
            unavailable.pk,
            {
                alternative["physician_id"]
                for _, rows in review.context["review_groups"]
                for row in rows
                for category in (
                    "equally_good_alternatives",
                    "other_normally_available_alternatives",
                    "backup_only_alternatives",
                )
                for alternative in row["explanation"].get(category, ())
            },
        )

    def test_late_doc_replacement_is_the_effective_proposed_end_state(self):
        self.ready_week()
        self.faculty("Second")
        self.faculty("Replacement 1")
        self.faculty("Replacement 2")
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        review = proposal.late_doc_reviews.get()
        review_url = reverse(
            "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
        )
        replacement = next(
            physician
            for physician in self.client.get(review_url)
            .context["form"]
            .fields["replacement_physician"]
            .queryset
            if physician.pk != review.proposed_physician_id
        )

        response = self.client.post(
            review_url,
            {
                "decision": "replaced",
                "replacement_physician": replacement.pk,
                "revision": proposal.revision,
            },
        )

        self.assertRedirects(
            response,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "#late-doc-review",
        )
        review.refresh_from_db()
        self.assertEqual(
            (review.date, review.decision, review.replacement_physician_id),
            (self.week, "replaced", replacement.pk),
        )
        review_page = self.client.get(response.url)
        late_doc_row = next(
            row
            for _, rows in review_page.context["review_groups"]
            for row in rows
            if row["date"] == review.date.isoformat() and row["position"] == "Late Doc"
        )
        self.assertEqual(late_doc_row["physician_name"], replacement.display_name)
        proposed_page = self.client.get(response.url + "?view=proposed")
        late_doc_item = next(
            item
            for week in proposed_page.context["calendar"].weeks
            for lane in week.lanes
            if lane.label == "Late Doc"
            for item in lane.items
            if item.content["target"] == f"proposal-position-{review.date}-Late Doc-late-doc"
        )
        self.assertEqual(late_doc_item.content["value"], replacement.display_name)

    def test_pending_late_doc_review_offers_inline_confirm_and_replace(self):
        proposal = self.proposal()
        review = proposal.late_doc_reviews.get()
        review_url = reverse(
            "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
        )

        page = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        parser = _RenderedSurfaceParser()
        parser.feed(page.content.decode())
        review_table = next(
            element
            for element in parser.elements
            if element.tag == "table"
            and "late-doc-review-table" in element.attributes.get("class", "").split()
            and any(
                ancestor.attributes.get("id") == "late-doc-review"
                for ancestor in element.ancestors()
            )
        )
        header_row = next(
            row
            for row in review_table.descendants()
            if row.tag == "tr"
            and {"Date", "Proposed physician", "Decision", "Actions"}.issubset(
                {
                    cell.text()
                    for cell in row.children
                    if not isinstance(cell, str) and cell.tag == "th"
                }
            )
        )
        headers = [
            cell.text()
            for cell in header_row.children
            if not isinstance(cell, str) and cell.tag == "th"
        ]
        pending_row = next(
            row
            for row in review_table.descendants()
            if row.tag == "tr"
            and any(
                cell.tag == "time" and cell.attributes.get("datetime") == review.date.isoformat()
                for cell in row.descendants()
            )
        )
        pending_row_cells = [
            cell
            for cell in pending_row.children
            if not isinstance(cell, str) and cell.tag in {"th", "td"}
        ]
        pending_by_header = dict(zip(headers, pending_row_cells, strict=True))
        self.assertEqual(
            pending_by_header["Proposed physician"].text(), review.proposed_physician.display_name
        )
        self.assertEqual(pending_by_header["Decision"].text(), "Pending")
        confirm_forms = [
            element
            for element in pending_by_header["Actions"].descendants()
            if element.tag == "form" and element.attributes.get("action") == review_url
        ]
        self.assertEqual(len(confirm_forms), 1)
        confirm_form = confirm_forms[0]
        self.assertEqual(confirm_form.attributes.get("method"), "post")
        self.assertEqual(
            {
                control.attributes.get("name"): control.attributes.get("value")
                for control in confirm_form.descendants()
                if control.tag == "input"
                and control.attributes.get("name") in {"decision", "revision"}
            },
            {"decision": "confirmed", "revision": str(proposal.revision)},
        )
        confirm_button = next(
            control
            for control in confirm_form.descendants()
            if control.tag == "button" and control.text() == "Confirm"
        )
        self.assertEqual(confirm_button.text(), "Confirm")
        self.assertIn(
            ("Replace", review_url),
            [
                (link.text(), link.attributes.get("href"))
                for link in pending_by_header["Actions"].descendants()
                if link.tag == "a"
            ],
        )
        response = self.client.post(
            review_url,
            {"decision": "confirmed", "revision": proposal.revision},
        )
        self.assertRedirects(
            response,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "#late-doc-review",
        )
        review.refresh_from_db()
        self.assertEqual(review.decision, "confirmed")
        self.assertEqual(review.reviewed_by, self.scheduler)
        updated_page = self.client.get(response.url)
        parser = _RenderedSurfaceParser()
        parser.feed(updated_page.content.decode())
        updated_row = next(
            row
            for row in parser.elements
            if row.tag == "tr"
            and any(
                cell.tag == "time" and cell.attributes.get("datetime") == review.date.isoformat()
                for cell in row.descendants()
            )
            and any(
                ancestor.attributes.get("id") == "late-doc-review" for ancestor in row.ancestors()
            )
        )
        updated_row_cells = [
            cell
            for cell in updated_row.children
            if not isinstance(cell, str) and cell.tag in {"th", "td"}
        ]
        updated_by_header = dict(zip(headers, updated_row_cells, strict=True))
        self.assertEqual(updated_by_header["Decision"].text(), "Confirmed")
        self.assertFalse(
            any(
                control.tag == "button" and control.text() == "Confirm"
                for control in updated_by_header["Actions"].descendants()
            )
        )
        self.assertIn(
            ("View decision", review_url),
            [
                (link.text(), link.attributes.get("href"))
                for link in updated_by_header["Actions"].descendants()
                if link.tag == "a"
            ],
        )

    def test_late_doc_review_empty_state_uses_a_full_width_table_row(self):
        proposal = self.retained_listing_proposal(created_at=timezone.now())

        page = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        parser = _RenderedSurfaceParser()
        parser.feed(page.content.decode())
        review_table = next(
            element
            for element in parser.elements
            if element.tag == "table"
            and "late-doc-review-table" in element.attributes.get("class", "").split()
            and any(
                ancestor.attributes.get("id") == "late-doc-review"
                for ancestor in element.ancestors()
            )
        )
        header_row = next(
            row
            for row in review_table.descendants()
            if row.tag == "tr"
            and any(cell.tag == "th" for cell in row.children if not isinstance(cell, str))
        )
        empty_row = next(
            row
            for row in review_table.descendants()
            if row.tag == "tr"
            and any(
                cell.tag == "td" and "empty" in cell.attributes.get("class", "").split()
                for cell in row.children
                if not isinstance(cell, str)
            )
        )
        empty_cell = next(
            cell
            for cell in empty_row.children
            if not isinstance(cell, str)
            and cell.tag == "td"
            and "empty" in cell.attributes.get("class", "").split()
        )
        self.assertIn("No Late Doc assignment", empty_cell.text())
        self.assertEqual(
            int(empty_cell.attributes["colspan"]),
            sum(cell.tag == "th" for cell in header_row.children if not isinstance(cell, str)),
        )

    def test_late_doc_review_candidates_keep_retained_seeded_order_after_renaming(self):
        self.ready_week()
        eligible_candidates = [self.faculty(name) for name in ("Alpha", "Beta", "Gamma")]
        response = self.solve()
        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        proposal.solve_settings["solve_seed"] = "late-doc-review-order"
        proposal.save(update_fields=("solve_settings",))
        review = proposal.late_doc_reviews.get()
        review_url = reverse(
            "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
        )
        candidate_ids = list(
            self.client.get(review_url)
            .context["form"]
            .fields["replacement_physician"]
            .queryset.values_list("pk", flat=True)
        )
        self.assertTrue({candidate.pk for candidate in eligible_candidates}.issubset(candidate_ids))
        for index, physician_id in enumerate(reversed(candidate_ids)):
            Physician.objects.filter(pk=physician_id).update(
                display_name=f"Renamed {index}",
            )

        response = self.client.post(
            review_url,
            {
                "decision": "replaced",
                "replacement_physician": candidate_ids[0],
                "revision": proposal.revision,
            },
        )
        self.assertRedirects(
            response,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "#late-doc-review",
        )
        response = self.client.get(review_url)

        self.assertEqual(
            list(
                response.context["form"]
                .fields["replacement_physician"]
                .queryset.values_list("pk", flat=True)
            ),
            candidate_ids,
        )

    def test_proposal_month_opens_shared_editable_week_without_draft_mutation(self):
        self.ready_week()
        self.faculty("First")
        replacements = [self.faculty(f"Replacement {index}") for index in range(4)]
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        assignment = proposal.proposed_assignments["precepting"][0]
        replacement = next(
            physician
            for physician in replacements
            if physician.pk
            not in {
                item["physician_id"]
                for item in proposal.proposed_assignments["precepting"]
                if item["date"] == assignment["date"]
                and item["period_of_day"] == assignment["period_of_day"]
            }
        )
        edit_url = reverse(
            "precepting-solver-assignment-edit",
            args=(
                proposal.pk,
                "precepting",
                assignment["date"],
                assignment["period_of_day"],
                assignment["position"],
            ),
        )
        proposal_week_url = reverse(
            "precepting-solver-week", args=(proposal.pk, self.week.isoformat())
        )

        changes = self.client.get(response.url + "?view=changes")
        proposed = self.client.get(response.url + "?view=proposed")
        current = self.client.get(response.url + "?view=current")
        proposal_week = self.client.get(proposal_week_url)
        edit = self.client.get(f"{edit_url}?week={self.week.isoformat()}")
        saved = self.client.post(
            f"{edit_url}?week={self.week.isoformat()}",
            {
                "revision": proposal.revision,
                "physician": replacement.pk,
                "note": "Manual coverage choice.",
            },
        )

        self.assertContains(changes, edit_url)
        self.assertContains(proposed, proposal_week_url)
        self.assertContains(
            proposed,
            'data-calendar-week-action="proposal-week-open-2027-02-01"',
        )
        self.assertContains(proposed, 'src="/static/schedules/calendar-week-actions.js"')
        self.assertNotContains(current, "data-calendar-week-action")
        self.assertNotContains(proposed, edit_url)
        self.assertNotContains(proposed, "Edit proposed assignment")
        self.assertContains(proposal_week, edit_url)
        self.assertContains(proposal_week, "calendar-surface--assignment-week")
        self.assertContains(proposal_week, 'aria-label="Preceptor 1:', html=False)
        self.assertContains(edit, "Edit proposed assignment")
        self.assertRedirects(saved, proposal_week_url)
        self.assertEqual(proposal.edits.count(), 1)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_proposal_week_uses_the_month_assignment_item_partial(self):
        proposal = self.proposal()
        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, self.week.isoformat()))
        )
        ordinary_week = self.client.get(reverse("monthly-workspace") + f"?month={self.week:%Y-%m}")

        proposal_item = proposal_week.context["calendar"].weeks[0].lanes[0].items[0]
        ordinary_item = ordinary_week.context["calendar"].weeks[0].lanes[0].items[0]

        self.assertEqual(proposal_item.partial, "schedules/calendar_assignment_item.html")
        self.assertEqual(proposal_item.partial, ordinary_item.partial)

    def test_schedule_bot_precepting_calendars_opt_into_compact_late_doc_lanes(self):
        proposal = self.proposal()
        review_url = reverse("precepting-solver-review", args=(proposal.pk,))
        responses = (
            self.client.get(review_url + "?view=current"),
            self.client.get(review_url + "?view=proposed"),
            self.client.get(
                reverse("precepting-solver-week", args=(proposal.pk, self.week.isoformat()))
            ),
        )

        for response in responses:
            lanes = response.context["calendar"].weeks[0].lanes
            self.assertEqual(
                {lane.label: lane.variant for lane in lanes},
                {"AM": "", "PM": "", "Late Doc": "compact"},
            )
            self.assertContains(
                response,
                'class="calendar-lattice-lane calendar-lattice-lane--compact" '
                'role="row" aria-label="Late Doc"',
            )

    def test_schedule_bot_status_and_reconciliation_summaries_use_surface_content_stack(self):
        proposal = self.proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        current = self.faculty("Current")
        assign_preceptor(
            session=PreceptorSession.objects.get(pk=proposed["session_id"]),
            physician=current,
            responsibility="secondary" if proposed["position"] == "secondary" else "primary",
            primary_position=(
                None
                if proposed["position"] == "secondary"
                else int(proposed["position"].removeprefix("primary-"))
            ),
            scheduler=self.scheduler,
        )
        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, self.week.isoformat()))
        )
        reconciliation = self.client.get(
            reverse("precepting-solver-reconcile", args=(proposal.pk,))
        )
        review = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))
        reconciliation_content = reconciliation.content.decode()

        self.assertRegex(
            proposal_week.content.decode(),
            r'<h1>Proposed week</h1>.*?<div class="surface-body surface-content-stack">',
        )
        self.assertRegex(
            reconciliation_content,
            r'<h1>Reconcile proposal</h1>.*?<div class="surface-body surface-content-stack">',
        )
        self.assertRegex(
            review.content.decode(),
            r'(?s)<h1>Review .*? proposal</h1>.*?<div class="surface-body surface-content-stack">',
        )
        self.assertContains(
            review,
            '<h2>Manual edit history</h2></header><div class="surface-body surface-content-stack">',
            html=False,
        )
        self.assertContains(
            review,
            '<h2>Unresolved work</h2></header><div class="surface-body surface-content-stack">',
            html=False,
        )
        self.assertContains(
            review,
            '<section class="workspace-surface surface-frame workspace-actions"><div class="surface-body surface-content-stack">',
            html=False,
        )
        form_start = reconciliation_content.index(
            '<form method="post" class="workspace-surface surface-frame">'
        )
        self.assertIn('<div class="surface-body">', reconciliation_content[form_start:])
        self.assertNotIn(
            '<div class="surface-body surface-content-stack">', reconciliation_content[form_start:]
        )

    def test_proposal_week_marks_holiday_closed_coverage_non_editable(self):
        holiday = date(2027, 12, 24)
        self.ready_range(holiday, holiday)
        self.faculty("Second")
        self.assertEqual(
            self.solve(start_date=holiday.isoformat(), end_date=holiday.isoformat()).status_code,
            302,
        )
        proposal = PreceptingSolverProposal.objects.get()

        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, holiday.isoformat()))
        )
        self.assertEqual(proposal_week.status_code, 200)
        pm_item = next(
            item
            for lane in proposal_week.context["calendar"].weeks[0].lanes
            if lane.label == "PM"
            for item in lane.items
            if item.content.period == "PM" and item.content.date_label.startswith("Friday, Dec 24")
        )

        self.assertEqual(
            [(slot.display, slot.state, slot.url) for slot in pm_item.content.slots],
            [("Closed", "holiday-closed", "")],
        )
        self.assertNotContains(proposal_week, "Edit Preceptor 1 for PM on December 24, 2027")

    def test_proposal_week_marks_christmas_eve_late_doc_as_pm_holiday_coverage(self):
        holiday = date(2029, 12, 24)
        self.ready_range(holiday, holiday)
        self.faculty("Second")
        self.assertEqual(
            self.solve(start_date=holiday.isoformat(), end_date=holiday.isoformat()).status_code,
            302,
        )
        proposal = PreceptingSolverProposal.objects.get()

        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, holiday.isoformat()))
        )
        late_doc = proposal_week.context["calendar"].weeks[0].calendar_lanes[2]

        self.assertEqual(late_doc.period, "PM")
        self.assertIn(1, late_doc.holiday_columns)
        self.assertEqual(
            late_doc.items[0].content["accessible_label"],
            "Late Doc Monday 5-6 PM: Closed for Christmas Eve, PM only.",
        )

    def test_proposal_review_overview_marks_holiday_closed_coverage_non_gap(self):
        holiday = date(2027, 12, 24)
        self.ready_range(holiday, holiday)
        self.faculty("Second")
        response = self.solve(start_date=holiday.isoformat(), end_date=holiday.isoformat())

        for view in ("current", "proposed"):
            calendar = self.client.get(response.url + f"?view={view}").context["calendar"]
            pm_item = next(
                item.content
                for week in calendar.weeks
                for lane in week.lanes
                if lane.label == "PM"
                for item in lane.items
                if item.content["date"] == holiday.isoformat()
            )
            self.assertEqual(
                [(position["value"], position["state"]) for position in pm_item["positions"]],
                [],
            )
            self.assertEqual((pm_item["display"], pm_item["state"]), ("Closed", "holiday-closed"))

    def test_proposal_review_overview_marks_closed_monday_late_doc(self):
        holiday = date(2027, 5, 31)
        self.ready_range(holiday, holiday)
        self.faculty("Second")
        response = self.solve(start_date=holiday.isoformat(), end_date=holiday.isoformat())

        for view in ("current", "proposed"):
            calendar = self.client.get(response.url + f"?view={view}").context["calendar"]
            late_doc_item = next(
                item.content
                for week in calendar.weeks
                for lane in week.lanes
                if lane.label == "Late Doc"
                for item in lane.items
                if item.content["target"] == f"proposal-position-{holiday}-Late Doc-late-doc"
            )
            self.assertEqual(
                (late_doc_item["value"], late_doc_item["state"]), ("Closed", "optional-empty")
            )

    def test_proposal_review_overview_marks_unfilled_late_doc_in_both_views(self):
        proposal = self.proposal()
        proposal.proposed_assignments["late_doc"] = []
        proposal.after_findings = proposal.before_findings
        proposal.save(update_fields=("proposed_assignments", "after_findings"))

        for view in ("current", "proposed"):
            with self.subTest(view=view):
                calendar = self.client.get(
                    reverse("precepting-solver-review", args=(proposal.pk,)) + f"?view={view}"
                ).context["calendar"]
                late_doc_item = next(
                    item.content
                    for week in calendar.weeks
                    for lane in week.lanes
                    if lane.label == "Late Doc"
                    for item in lane.items
                    if item.content["target"] == f"proposal-position-{self.week}-Late Doc-late-doc"
                )

                self.assertEqual(late_doc_item["value"], "Unfilled")

    def test_stale_proposed_holiday_coverage_renders_closed_before_reconciliation(self):
        self.ready_week()
        baseline = self.faculty("Baseline")
        assign_preceptor(
            session=PreceptorSession.objects.get(date=self.week, period_of_day="AM"),
            physician=baseline,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        self.faculty("Proposed")
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        stale_precepting_positions = {
            assignment["position"]
            for assignment in proposal.proposed_assignments["precepting"]
            if assignment["date"] == self.week.isoformat() and assignment["period_of_day"] == "AM"
        }
        self.assertTrue(stale_precepting_positions)
        self.assertTrue(
            any(
                assignment["date"] == self.week.isoformat()
                for assignment in proposal.proposed_assignments["late_doc"]
            )
        )
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.INDEPENDENCE_DAY),
            observed_date=self.week,
            scheduler=self.scheduler,
        )

        overview = self.client.get(response.url + "?view=proposed").context["calendar"]
        overview_am = next(
            item.content
            for week in overview.weeks
            for lane in week.lanes
            if lane.label == "AM"
            for item in lane.items
            if item.content["date"] == self.week.isoformat()
        )
        overview_late_doc = next(
            item.content
            for week in overview.weeks
            for lane in week.lanes
            if lane.label == "Late Doc"
            for item in lane.items
            if item.content["target"] == f"proposal-position-{self.week}-Late Doc-late-doc"
        )
        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, self.week.isoformat()))
        )
        week_am = next(
            item.content
            for lane in proposal_week.context["calendar"].weeks[0].lanes
            if lane.label == "AM"
            for item in lane.items
            if item.content.period == "AM" and item.content.date_label.startswith("Monday, Feb 1")
        )
        week_late_doc = next(
            item.content
            for lane in proposal_week.context["calendar"].weeks[0].lanes
            if lane.label == "Late Doc"
            for item in lane.items
            if item.content
            and item.content["target"] == f"proposal-position-{self.week}-Late Doc-late-doc"
        )

        overview_positions = dict(
            zip(("primary-1", "primary-2", "secondary"), overview_am["positions"], strict=True)
        )
        self.assertEqual(overview_positions["primary-1"]["value"], "Baseline")
        for position in stale_precepting_positions:
            self.assertEqual(
                (overview_positions[position]["value"], overview_positions[position]["changed"]),
                ("Closed", False),
            )
        self.assertEqual(
            (overview_late_doc["value"], overview_late_doc["state"], overview_late_doc["changed"]),
            ("Closed", "optional-empty", False),
        )
        self.assertEqual(week_am.slots[0].display, "Conflict: Baseline")
        week_positions = dict(
            zip(("primary-1", "primary-2", "secondary"), week_am.slots, strict=True)
        )
        for position in stale_precepting_positions:
            self.assertEqual(
                (week_positions[position].display, week_positions[position].changed),
                ("Closed", False),
            )
        self.assertEqual(
            (week_late_doc["display"], week_late_doc["state"], week_late_doc["changed"]),
            ("Closed", "optional-empty", False),
        )

    def test_schedule_bot_projects_retained_holiday_findings_in_current_proposed_and_week(self):
        self.ready_week()
        retained = self.faculty("Retained")
        assign_preceptor(
            session=PreceptorSession.objects.get(date=self.week, period_of_day="AM"),
            physician=retained,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        LateDocAssignment.objects.create(date=self.week, physician=retained)
        self.faculty("Second")
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.INDEPENDENCE_DAY),
            observed_date=self.week,
            scheduler=self.scheduler,
        )

        for view in ("current", "proposed"):
            with self.subTest(view=view):
                rendered = self.client.get(response.url + f"?view={view}")
                calendar = rendered.context["calendar"]
                am_item = next(
                    item.content
                    for calendar_week in calendar.weeks
                    for lane in calendar_week.lanes
                    if lane.label == "AM"
                    for item in lane.items
                    if item.content["date"] == self.week.isoformat()
                )
                late_doc_item = next(
                    item.content
                    for calendar_week in calendar.weeks
                    for lane in calendar_week.lanes
                    if lane.label == "Late Doc"
                    for item in lane.items
                    if item.content["target"] == f"proposal-position-{self.week}-Late Doc-late-doc"
                )

                self.assertEqual(
                    (am_item["positions"][0]["state"], am_item["positions"][0]["value"]),
                    ("conflict", "Retained"),
                )
                self.assertEqual(
                    (late_doc_item["state"], late_doc_item["value"]),
                    ("conflict", "Retained"),
                )
                self.assertEqual(
                    am_item["positions"][0]["target"],
                    f"proposal-position-{self.week}-AM-primary-1",
                )
                self.assertIn(
                    "Clinic holiday closes this Precepting session while an assignment remains.",
                    [finding["message"] for finding in am_item["findings"]],
                )
                self.assertIn(
                    "Clinic holiday closes Monday Late Doc while an assignment remains.",
                    [finding["message"] for finding in late_doc_item["findings"]],
                )

        proposal_week = self.client.get(
            reverse("precepting-solver-week", args=(proposal.pk, self.week.isoformat()))
        )
        week_am = next(
            item.content
            for lane in proposal_week.context["calendar"].weeks[0].lanes
            if lane.label == "AM"
            for item in lane.items
            if item.content.period == "AM" and item.content.date_label.startswith("Monday, Feb 1")
        )
        week_late_doc = next(
            item.content
            for lane in proposal_week.context["calendar"].weeks[0].lanes
            if lane.label == "Late Doc"
            for item in lane.items
            if item.content
            and item.content["target"] == f"proposal-position-{self.week}-Late Doc-late-doc"
        )

        self.assertEqual(week_am.slots[0].state, "conflict")
        self.assertEqual(week_late_doc["state"], "conflict")
        self.assertEqual(
            week_am.slots[0].target,
            f"proposal-position-{self.week}-AM-primary-1",
        )
        self.assertContains(
            proposal_week,
            "Clinic holiday closes this Precepting session while an assignment remains.",
        )
        self.assertContains(
            proposal_week,
            "Clinic holiday closes Monday Late Doc while an assignment remains.",
        )

    def test_assignment_edit_excludes_retained_physicians_from_form_and_crafted_post(self):
        self.ready_week()
        self.faculty("First")
        excluded = self.faculty("Excluded")
        response = self.solve(excluded_physician_ids=[excluded.pk])
        proposal = PreceptingSolverProposal.objects.get()
        assignment = proposal.proposed_assignments["precepting"][0]
        edit_url = reverse(
            "precepting-solver-assignment-edit",
            args=(
                proposal.pk,
                "precepting",
                assignment["date"],
                assignment["period_of_day"],
                assignment["position"],
            ),
        )

        edit_page = self.client.get(edit_url)
        crafted = self.client.post(
            edit_url,
            {"revision": proposal.revision, "physician": excluded.pk, "note": "Ignore exclusion."},
        )

        self.assertRedirects(response, reverse("precepting-solver-review", args=(proposal.pk,)))
        field = edit_page.context["form"].fields["physician"]
        self.assertEqual(field.__class__.__name__, "AssignmentPhysicianChoiceField")
        self.assertNotIn(
            excluded,
            [alternative.physician for alternative in edit_page.context["form"].alternatives],
        )
        self.assertEqual(crafted.status_code, 200)
        self.assertContains(crafted, "Select a valid choice")
        self.assertFalse(proposal.edits.exists())

    def test_stale_assignment_edit_redirects_to_refreshed_review_without_mutation(self):
        self.ready_week()
        self.faculty("First")
        for index in range(5):
            self.faculty(f"Replacement {index}")
        response = self.solve()
        proposal = PreceptingSolverProposal.objects.get()
        assignment = proposal.proposed_assignments["precepting"][0]
        edit_url = reverse(
            "precepting-solver-assignment-edit",
            args=(
                proposal.pk,
                "precepting",
                assignment["date"],
                assignment["period_of_day"],
                assignment["position"],
            ),
        )
        choices = [
            alternative.physician
            for alternative in self.client.get(edit_url).context["form"].alternatives
            if alternative.physician.pk != assignment["physician_id"]
        ]
        stale_revision = proposal.revision
        saved = self.client.post(
            edit_url, {"revision": stale_revision, "physician": choices[0].pk, "note": "First."}
        )
        stale = self.client.post(
            edit_url, {"revision": stale_revision, "physician": choices[1].pk, "note": "Second."}
        )

        self.assertRedirects(response, reverse("precepting-solver-review", args=(proposal.pk,)))
        self.assertRedirects(saved, reverse("precepting-solver-review", args=(proposal.pk,)))
        self.assertRedirects(
            stale,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?conflict=revision",
        )
        proposal.refresh_from_db()
        self.assertEqual(proposal.edits.count(), 1)
        self.assertEqual(proposal.edits.get().resulting_value["physician_id"], choices[0].pk)
        self.assertContains(
            self.client.get(stale.url),
            "This proposal changed while you were editing it. Your work was not saved",
        )

    def test_stale_proposal_offers_reconcile_with_conflict_choices_and_no_other_proposal_input(
        self,
    ):
        proposal = self.proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        current = self.faculty("Current")
        assign_preceptor(
            session=PreceptorSession.objects.get(pk=proposed["session_id"]),
            physician=current,
            responsibility="secondary" if proposed["position"] == "secondary" else "primary",
            primary_position=(
                None
                if proposed["position"] == "secondary"
                else int(proposed["position"].removeprefix("primary-"))
            ),
            scheduler=self.scheduler,
        )

        review = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))
        reconcile_url = reverse("precepting-solver-reconcile", args=(proposal.pk,))
        reconciliation = self.client.get(reconcile_url)
        conflicts = reconciliation.context["conflicts"]
        response = self.client.post(
            reconcile_url,
            {
                "revision": proposal.revision,
                "current_input_fingerprint": reconciliation.context[
                    "preview"
                ].current_input_fingerprint,
                **{conflict["choice_key"]: "proposal" for conflict in conflicts},
            },
        )

        self.assertContains(review, "distinct shift")
        self.assertContains(review, "Reconcile")
        self.assertContains(reconciliation, "Reconcile proposal")
        self.assertNotContains(reconciliation, "Choose another proposal")
        for rendered in (review, reconciliation):
            self.assertContains(rendered, "0 already match")
            self.assertNotContains(rendered, "matchs")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PreceptingSolverProposal.objects.count(), 2)

    def test_reconciliation_summaries_use_singular_already_matches(self):
        proposal = self.proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        assign_preceptor(
            session=PreceptorSession.objects.get(pk=proposed["session_id"]),
            physician=Physician.objects.get(pk=proposed["physician_id"]),
            responsibility="secondary" if proposed["position"] == "secondary" else "primary",
            primary_position=(
                None
                if proposed["position"] == "secondary"
                else int(proposed["position"].removeprefix("primary-"))
            ),
            scheduler=self.scheduler,
        )

        review = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))
        reconciliation = self.client.get(
            reverse("precepting-solver-reconcile", args=(proposal.pk,))
        )

        for rendered in (review, reconciliation):
            self.assertContains(rendered, "1 already matches")
            self.assertNotContains(rendered, "matchs")

    def test_stale_late_doc_review_redirects_to_refreshed_review_without_mutation(self):
        proposal = self.proposal()
        review = proposal.late_doc_reviews.get()
        review_url = reverse(
            "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
        )
        stale_revision = proposal.revision
        saved = self.client.post(review_url, {"decision": "confirmed", "revision": stale_revision})
        proposal.refresh_from_db()
        review.refresh_from_db()
        expected_state = {
            "proposal_revision": proposal.revision,
            "proposal_updated_at": proposal.updated_at,
            "proposal_last_edited_by_id": proposal.last_edited_by_id,
            "review_decision": review.decision,
            "review_replacement_physician_id": review.replacement_physician_id,
            "review_reviewed_by_id": review.reviewed_by_id,
            "review_reviewed_at": review.reviewed_at,
        }
        stale = self.client.post(review_url, {"decision": "confirmed", "revision": stale_revision})

        self.assertRedirects(
            saved,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "#late-doc-review",
        )
        self.assertRedirects(
            stale,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?conflict=revision",
        )
        proposal.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(
            {
                "proposal_revision": proposal.revision,
                "proposal_updated_at": proposal.updated_at,
                "proposal_last_edited_by_id": proposal.last_edited_by_id,
                "review_decision": review.decision,
                "review_replacement_physician_id": review.replacement_physician_id,
                "review_reviewed_by_id": review.reviewed_by_id,
                "review_reviewed_at": review.reviewed_at,
            },
            expected_state,
        )
        self.assertContains(
            self.client.get(stale.url),
            "This proposal changed while you were editing it. Your work was not saved",
        )

    def test_calendar_keeps_each_precepting_position_in_its_own_row(self):
        rounder = self.ready_week()
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=rounder,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        self.faculty("Second")
        response = self.solve()

        proposed = self.client.get(response.url + "?view=proposed").context["calendar"]
        current = self.client.get(response.url + "?view=current").context["calendar"]
        proposed_am = next(
            item.content
            for week in proposed.weeks
            for lane in week.lanes
            if lane.label == "AM"
            for item in lane.items
            if item.content["date"] == self.week.isoformat()
        )
        current_am = next(
            item.content
            for week in current.weeks
            for lane in week.lanes
            if lane.label == "AM"
            for item in lane.items
            if item.content["date"] == self.week.isoformat()
        )

        self.assertEqual(
            [position["label"] for position in proposed_am["positions"]],
            ["Preceptor 1", "Preceptor 2", "Secondary"],
        )
        self.assertEqual(proposed_am["positions"][0]["value"], "Rounder")
        self.assertFalse(proposed_am["positions"][0]["changed"])
        self.assertTrue(any(position["changed"] for position in proposed_am["positions"][1:]))
        self.assertTrue(all(not position["changed"] for position in current_am["positions"]))
        proposed_rendered = self.client.get(response.url + "?view=proposed")
        current_rendered = self.client.get(response.url + "?view=current")
        for rendered in (proposed_rendered, current_rendered):
            self.assertNotContains(rendered, ">Preceptor 1:")
            self.assertNotContains(rendered, ">Preceptor 2:")
            self.assertNotContains(rendered, ">Secondary:")
            self.assertNotContains(rendered, ">Late Doc:")
            self.assertContains(rendered, 'aria-label="Preceptor 1:')
            self.assertContains(rendered, 'aria-label="Preceptor 2:')
            self.assertContains(rendered, 'aria-label="Secondary:')
            self.assertContains(rendered, 'aria-label="Late Doc:')

    def test_proposed_calendar_delta_label_matches_proposal_mode(self):
        proposal = self.proposal()
        review_url = reverse("precepting-solver-review", args=(proposal.pk,))

        fill = self.client.get(review_url)
        proposal.mode = PreceptingSolverProposal.Mode.REPAIR
        proposal.save(update_fields=("mode",))
        repair = self.client.get(review_url)

        self.assertContains(fill, '<span class="solver-calendar-delta">Filled</span>', html=False)
        self.assertContains(fill, 'aria-label="Preceptor 1: Filled,', html=False)
        self.assertContains(
            repair, '<span class="solver-calendar-delta">Changed</span>', html=False
        )
        self.assertContains(repair, 'aria-label="Preceptor 1: Changed,', html=False)

    def test_comparison_displays_feasibility_moves_workload_and_compromises(self):
        proposal = self.proposal()
        PreceptingSolverProposalComparison.objects.create(
            proposal=proposal,
            created_by=self.scheduler,
            result={
                "feasible": True,
                "forced_or_excluded": "Forced the selected alternative for primary-1.",
                "additional_changes": ["2027-02-02 AM primary-1 changes."],
                "workload_effect": {
                    "faculty_workloads": [
                        {
                            "physician_name": "Rounder",
                            "current_total": 2,
                            "proposed_total": 3,
                            "target_total": 2.5,
                        }
                    ]
                },
                "compromises": ["One Community commitment remains uncovered."],
            },
        )

        response = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        self.assertContains(response, "Feasible.")
        self.assertContains(response, "Extra moves")
        self.assertContains(response, "Faculty workload effect")
        self.assertContains(response, 'id="comparison-workload-effects"')
        self.assertContains(response, "Rounder")
        self.assertContains(response, "New compromises")
        self.assertContains(response, "One Community commitment remains uncovered.")
        self.assertContains(response, "Forced the selected alternative for Preceptor 1.")
        self.assertContains(response, "2027-02-02 AM Preceptor 1 changes.")

    def test_rendered_late_doc_compare_form_accepts_an_equal_alternative(self):
        self.ready_week()
        self.faculty("Second")
        alternative = self.faculty("Z Late Doc alternative")
        response = self.solve()
        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        late_doc_assignment = proposal.proposed_assignments["late_doc"][0]
        proposal.proposed_assignments["late_doc"][0]["explanation"]["equally_good_alternatives"] = [
            {
                "physician_id": alternative.pk,
                "physician_name": alternative.display_name,
                "reason": "Produces the same complete named policy outcome.",
            }
        ]
        proposal.save(update_fields=("proposed_assignments",))

        review_page = self.client.get(
            reverse("precepting-solver-review", args=(proposal.pk,)) + "?view=changes"
        )
        late_doc_row = next(
            row
            for _, rows in review_page.context["review_groups"]
            for row in rows
            if row["position"] == "Late Doc" and row["date"] == late_doc_assignment["date"]
        )

        # The rendered form uses the retained internal identity, not the Late Doc display label.
        self.assertContains(
            review_page,
            'name="period_of_day" value=""',
        )
        response = self.client.post(
            reverse("precepting-solver-compare", args=(proposal.pk,)),
            {
                "assignment_type": "late_doc",
                "date": late_doc_row["date"],
                "period_of_day": "",
                "position": "late-doc",
                "alternative_physician_id": alternative.pk,
            },
        )

        self.assertRedirects(response, reverse("precepting-solver-review", args=(proposal.pk,)))
        comparison_page = self.client.get(response.url)
        self.assertContains(comparison_page, "Latest comparison")
        self.assertContains(comparison_page, "Forced the selected alternative for Late Doc.")

    def test_late_doc_review_enables_successful_apply_to_the_draft(self):
        proposal = self.proposal()
        review = proposal.late_doc_reviews.get()

        response = self.client.post(
            reverse(
                "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
            ),
            {"decision": "confirmed", "revision": proposal.revision},
        )

        self.assertRedirects(
            response,
            reverse("precepting-solver-review", args=(proposal.pk,)) + "#late-doc-review",
        )
        confirmation = self.client.post(reverse("precepting-solver-apply", args=(proposal.pk,)))
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Confirm hard findings")
        self.assertContains(confirmation, "will remain in the draft.")
        self.assertContains(confirmation, "Applying is a checkpoint")
        self.assertContains(confirmation, "Acknowledge as an exception")
        self.assertContains(confirmation, 'name="acknowledge_finding"', html=False)
        self.assertNotRegex(
            confirmation.content.decode(),
            r'<input type="checkbox" name="acknowledge_finding" value="[^"]+" checked',
        )
        self.assertContains(confirmation, 'name="reason"', html=False)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())

        proposal.refresh_from_db()
        applied = self.client.post(
            reverse("precepting-solver-apply", args=(proposal.pk,)),
            {
                "confirm_hard_findings": "1",
                "revision": proposal.revision,
                "reason": "Keep this checkpoint while staffing is arranged.",
            },
        )
        self.assertEqual(applied.status_code, 302)
        self.assertTrue(PreceptorAssignment.objects.exists())
        self.assertTrue(LateDocAssignment.objects.filter(date=self.week).exists())
        proposal.refresh_from_db()
        self.assertEqual(
            proposal.applied_reason, "Keep this checkpoint while staffing is arranged."
        )

    def test_clean_apply_surface_names_current_schedule_update(self):
        proposal = self.proposal()
        proposal.after_findings = []
        proposal.save(update_fields=("after_findings",))

        response = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        self.assertContains(response, "Apply to draft")
        self.assertContains(response, "Apply updates the ordinary draft.")
        self.assertNotContains(response, "Publishing remains a separate operation.")
        self.assertNotContains(response, "Review 1 hard finding before applying")

    def test_hard_finding_apply_surface_explains_checkpoint_before_confirmation(self):
        proposal = self.proposal()
        proposal.after_findings = [
            {
                "date": "2027-02-01",
                "code": "not_available_assignment",
                "severity": "hard",
                "period_of_day": "AM",
                "explanation": {"position": "primary-1"},
            },
            {
                "date": "2027-02-02",
                "code": "missing_normal_rounder",
                "severity": "hard",
                "period_of_day": None,
                "explanation": {},
            },
        ]
        proposal.save(update_fields=("after_findings",))

        response = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        self.assertContains(response, "2 hard findings will remain after Apply.")
        self.assertContains(response, "Apply records a useful checkpoint; it does not resolve")
        self.assertContains(response, "select individual findings as acknowledged exceptions")
        self.assertContains(response, "Unselected findings remain required work")
        self.assertContains(response, "Review 2 hard findings before applying")
        self.assertContains(
            response,
            "<button disabled>Review 2 hard findings before applying</button>",
            html=False,
        )
        self.assertNotContains(response, ">Apply to draft</button>", html=False)

    def test_applied_proposal_does_not_offer_another_apply_or_confirmation(self):
        proposal = self.proposal()
        proposal.applied_at = timezone.now()
        proposal.applied_by = self.scheduler
        proposal.save(update_fields=("applied_at", "applied_by"))

        response = self.client.get(reverse("precepting-solver-review", args=(proposal.pk,)))

        self.assertContains(response, "<button disabled>Applied to draft</button>", html=False)
        self.assertContains(response, "This proposal has already updated the ordinary draft.")
        self.assertNotContains(response, "Review 1 hard finding before applying")
        self.assertNotContains(response, "select individual findings as acknowledged exceptions")

    def test_stale_hard_finding_confirmation_refuses_without_draft_mutation(self):
        proposal = self.proposal()
        review = proposal.late_doc_reviews.get()
        review_url = reverse(
            "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
        )
        self.client.post(review_url, {"decision": "confirmed", "revision": proposal.revision})
        proposal.refresh_from_db()
        warning = self.client.post(reverse("precepting-solver-apply", args=(proposal.pk,)))
        self.assertEqual(warning.status_code, 200)
        self.assertContains(warning, "Confirm hard findings")
        confirmation_revision = proposal.revision

        # A later scheduler action refreshes the proposal before this browser confirms.
        self.client.post(
            review_url,
            {"decision": "confirmed", "revision": proposal.revision},
        )
        stale = self.client.post(
            reverse("precepting-solver-apply", args=(proposal.pk,)),
            {"confirm_hard_findings": "1", "revision": confirmation_revision},
        )

        self.assertEqual(stale.status_code, 200)
        self.assertContains(stale, "changed while you were editing")
        self.assertContains(stale, "will remain after Apply")
        self.assertContains(stale, "select individual findings as acknowledged exceptions")
        self.assertNotContains(stale, "Confirm hard findings")
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_stale_proposal_cannot_be_applied(self):
        proposal = self.proposal()
        review = proposal.late_doc_reviews.get()
        self.client.post(
            reverse(
                "precepting-solver-late-doc-review", args=(proposal.pk, review.date.isoformat())
            ),
            {"decision": "confirmed", "revision": proposal.revision},
        )
        proposal.refresh_from_db()
        warning = self.client.post(reverse("precepting-solver-apply", args=(proposal.pk,)))
        self.assertContains(warning, "Confirm hard findings")
        confirmation_revision = proposal.revision
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=self.faculty("Changed"),
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        before_apply = self.draft_state()

        response = self.client.post(
            reverse("precepting-solver-apply", args=(proposal.pk,)),
            {"confirm_hard_findings": "1", "revision": confirmation_revision},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "stale")
        self.assertEqual(self.draft_state(), before_apply)

    def test_authenticated_scheduler_can_open_workspace_and_anonymous_user_is_redirected(self):
        self.assertEqual(self.client.get(reverse("precepting-solver-workspace")).status_code, 200)
        self.client.logout()
        response = self.client.get(reverse("precepting-solver-workspace"))
        self.assertEqual(response.status_code, 302)
