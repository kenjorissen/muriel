from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from threading import Event, RLock, Thread, current_thread
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase

import schedules.services._mutation as _mutation
import schedules.services.precepting_solver as precepting_solver
from schedules.management.commands.time_precepting_solver import Command as TimingCommand
from schedules.models import (
    AcademicEvent,
    ClinicHoliday,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    LateDocAssignment,
    LateDocAssignmentChange,
    Physician,
    PreceptingSolverLateDocReview,
    PreceptingSolverProposalEdit,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.late_docs import assign_late_doc
from schedules.services.physicians import (
    add_role,
    correct_faculty_availability_template,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
    set_faculty_precepting_capacity_override,
)
from schedules.services.precepting_solver import (
    FillProposalUnresolvedRequirement,
    HardFindingConfirmationRequired,
    SolveDateRange,
    apply_precepting_solver_proposal,
    balance_window_for_span,
    clip_solve_range_at_today,
    compare_fill_proposal,
    custom_solve_range,
    default_balance_window,
    edit_proposed_assignment,
    effective_proposal_assignments,
    month_solve_range,
    preflight_precepting_solve,
    preview_proposal_reconciliation,
    produce_fill_proposal,
    produce_repair_proposal,
    proposal_assignment_context,
    quarter_solve_range,
    reconcile_precepting_solver_proposal,
    retain_fill_proposal,
    retain_proposal_comparison,
    review_proposed_late_doc,
    validate_precepting_solver_capacity,
    without_excluded_physicians,
)
from schedules.services.preceptors import (
    assign_preceptor,
    draft_findings_for_week,
    open_weekly_draft,
    set_preceptor_assignment_lock,
)
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.vacations import create_vacation


class PreceptingSolverTimingTests(SimpleTestCase):
    def test_three_month_target_includes_preflight_for_fill_and_repair(self):
        self.assertFalse(TimingCommand._three_month_target_met(5.9, 0.2, 0.05))
        self.assertFalse(TimingCommand._three_month_target_met(5.9, 0.05, 0.2))
        self.assertTrue(TimingCommand._three_month_target_met(5.8, 0.1, 0.2))

    def test_timing_command_applies_one_cp_sat_limit_to_each_independent_operation(self):
        command = TimingCommand()
        preflight = SimpleNamespace(blockers=(), solver_input=object())
        solve_range = SolveDateRange(date(2027, 8, 1), date(2027, 8, 31))

        with (
            patch(
                "schedules.management.commands.time_precepting_solver."
                "initialize_preceptor_session_range"
            ),
            patch(
                "schedules.management.commands.time_precepting_solver.preflight_precepting_solve",
                return_value=preflight,
            ) as preflight_solve,
            patch(
                "schedules.management.commands.time_precepting_solver.produce_fill_proposal"
            ) as fill,
            patch(
                "schedules.management.commands.time_precepting_solver.produce_repair_proposal"
            ) as repair,
        ):
            command._measure_range("prepared range", solve_range, 7)

        self.assertEqual(fill.call_args.kwargs["max_time_seconds"], 7)
        self.assertEqual(repair.call_args.kwargs["max_time_seconds"], 7)
        self.assertEqual(
            preflight_solve.call_args.kwargs["balance_window"],
            SolveDateRange(date(2026, 9, 1), date(2027, 8, 31)),
        )
        self.assertIsInstance(fill.call_args.kwargs["phase_timings"], dict)
        self.assertIsInstance(repair.call_args.kwargs["phase_timings"], dict)

    def test_timing_command_reports_a_native_cp_sat_timeout_without_skipping_repair(self):
        command = TimingCommand()
        preflight = SimpleNamespace(blockers=(), solver_input=object())

        with (
            patch(
                "schedules.management.commands.time_precepting_solver."
                "initialize_preceptor_session_range"
            ),
            patch(
                "schedules.management.commands.time_precepting_solver.preflight_precepting_solve",
                return_value=preflight,
            ),
            patch(
                "schedules.management.commands.time_precepting_solver.produce_fill_proposal",
                side_effect=precepting_solver.PreceptingSolverTimeout("limit reached"),
            ),
            patch(
                "schedules.management.commands.time_precepting_solver.produce_repair_proposal"
            ) as repair,
        ):
            result = command._measure_range(
                "prepared range",
                SolveDateRange(date(2027, 8, 1), date(2027, 8, 31)),
                7,
            )

        self.assertIsNone(result[1])
        self.assertIsNotNone(result[2])
        self.assertTrue(repair.called)

    def test_diagnostic_cp_sat_limit_rejects_a_nonoptimal_limited_solve(self):
        solver_input = precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(date(2027, 8, 1), date(2027, 8, 1)),
            balance_window=SolveDateRange(date(2027, 8, 1), date(2027, 8, 1)),
            sessions=(),
            late_doc_positions=(),
            community_commitments=(),
            faculty_capacities=(),
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="timing-test",
        )
        adapter = precepting_solver._CpSatFillAdapter(solver_input, max_time_seconds=3)
        adapter.solve_deadline = perf_counter() + 3
        limited_solver = adapter._solver()

        class InterruptedSolver:
            def Solve(self, model):
                return precepting_solver.cp_model.FEASIBLE

        self.assertGreater(limited_solver.parameters.max_time_in_seconds, 0)
        self.assertLessEqual(limited_solver.parameters.max_time_in_seconds, 3)
        with self.assertRaises(precepting_solver.PreceptingSolverTimeout):
            adapter._solve_model(InterruptedSolver())

    def test_bounded_failure_reports_the_active_phase_and_measurements(self):
        adapter = precepting_solver._CpSatFillAdapter(
            self._empty_solver_input(), max_time_seconds=8
        )
        adapter.current_phase = "Late Doc policy"
        adapter.phase_started = perf_counter()
        adapter.phase_timings["model construction"] = 0.125

        with self.assertRaises(precepting_solver.PreceptingSolverTimeout) as raised:
            adapter._raise_timeout()
        message = str(raised.exception)
        self.assertRegex(message, r"(?:Late Doc policy.*8(?:\.0+)?s|8(?:\.0+)?s.*Late Doc policy)")
        self.assertRegex(message, r"(?:model construction.*0\.125|0\.125.*model construction)")

    def test_component_enumeration_observes_the_global_solve_deadline(self):
        adapter = precepting_solver._CpSatFillAdapter(
            self._empty_solver_input(), max_time_seconds=1
        )
        candidates = tuple(
            replace(self._tie_break_candidate(), physician_id=physician_id)
            for physician_id in range(1, 51)
        )
        requirements = tuple(
            precepting_solver._FillRequirement(
                date(2027, 8, 1),
                "AM",
                1,
                f"primary-{position}",
                candidates,
                candidates,
            )
            for position in range(1, 4)
        )
        adapter.solve_deadline = 1
        adapter.current_phase = "independent policy prefix"
        adapter.phase_started = 0

        with (
            patch(
                "schedules.services.precepting_solver.perf_counter",
                side_effect=(0.5, 1, 1.1),
            ),
            self.assertRaises(precepting_solver.PreceptingSolverTimeout) as raised,
        ):
            adapter._best_component_choices(requirements)
        self.assertIn("independent policy prefix", str(raised.exception))

    def test_late_doc_balance_uses_its_independent_lane_and_balances_actual_assignments(self):
        candidates = tuple(
            replace(
                self._tie_break_candidate(),
                physician_id=physician_id,
                physician_name=f"Faculty {physician_id}",
            )
            for physician_id in range(1, 25)
        )
        first_monday = date(2027, 8, 2)
        solve_range = SolveDateRange(first_monday, first_monday + timedelta(days=77))
        solver_input = precepting_solver.PreceptingSolverInput(
            solve_range=solve_range,
            balance_window=solve_range,
            sessions=(),
            late_doc_positions=tuple(
                precepting_solver.SolverLateDocPosition(
                    first_monday + timedelta(days=7 * week), None, candidates, candidates
                )
                for week in range(12)
            ),
            community_commitments=(),
            faculty_capacities=tuple(
                precepting_solver.FacultyCapacity(
                    physician_id=candidate.physician_id,
                    physician_name=candidate.physician_name,
                    precepting_capacity=1,
                    precepting_share=1 / len(candidates),
                    late_doc_capacity=1,
                    late_doc_share=1 / len(candidates),
                )
                for candidate in candidates
            ),
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="interchangeable-late-docs",
        )
        independent_lane = precepting_solver._CpSatFillAdapter(
            solver_input,
            forced_choices=((first_monday, None, "late-doc", candidates[-1].physician_id),),
            solve_seed="flow",
        )
        independent_lane._add_variables_and_session_constraints()
        independent_lane._apply_comparison_constraints()
        independent_lane._fix_independent_policy_outcomes()

        with patch.object(
            independent_lane,
            "_solver",
            side_effect=AssertionError(
                "Late Doc balance must not solve the complete CP-SAT model."
            ),
        ):
            independent_lane._apply_late_doc_balance_and_overlap_policy()

        self.assertEqual(sum(independent_lane.late_doc_band_totals.values()), 12)
        self.assertEqual(
            sorted(independent_lane.late_doc_band_totals.values()),
            [0] * 12 + [1] * 12,
        )

        proposal = precepting_solver._CpSatFillAdapter(
            solver_input,
            forced_choices=((first_monday, None, "late-doc", candidates[-1].physician_id),),
            solve_seed="flow",
        ).solve()

        assigned_by_date = {
            assignment.date: assignment.physician_id for assignment in proposal.late_doc_assignments
        }
        self.assertEqual(assigned_by_date[first_monday], candidates[-1].physician_id)
        self.assertEqual(len(assigned_by_date), 12)
        self.assertLessEqual(
            max(
                sum(assigned == physician_id for assigned in assigned_by_date.values())
                for physician_id in set(assigned_by_date.values())
            ),
            2,
        )

    def test_late_doc_flow_keeps_fixed_backup_and_soft_policy_outcomes(self):
        """Balance bands must admit the independently fixed mixed-candidate solution."""
        normal = replace(self._tie_break_candidate(), physician_id=1, physician_name="Normal")
        backup = replace(
            normal,
            physician_id=2,
            physician_name="Backup",
            availability="backup_only",
            base_availability="backup_only",
        )
        soft = replace(normal, physician_id=3, physician_name="Soft", has_soft_academic_event=True)
        mondays = tuple(date(2027, 8, 2) + timedelta(days=7 * week) for week in range(5))
        solver_input = self._late_doc_only_solver_input(mondays, (normal, backup, soft))

        proposal = precepting_solver._CpSatFillAdapter(
            solver_input,
            forced_choices=((mondays[0], None, "late-doc", backup.physician_id),),
            solve_seed="mixed-late-doc-policy",
        ).solve()

        self.assertEqual(
            [(item.date, item.physician_id) for item in proposal.late_doc_assignments],
            [
                (mondays[0], backup.physician_id),
                *[(on_date, normal.physician_id) for on_date in mondays[1:]],
            ],
        )
        self.assertFalse(proposal.unresolved_requirements)

    def test_late_doc_flow_keeps_repair_baselines_before_deriving_bands(self):
        normal = replace(self._tie_break_candidate(), physician_id=1, physician_name="Normal")
        backup = replace(
            normal,
            physician_id=2,
            physician_name="Backup",
            availability="backup_only",
            base_availability="backup_only",
        )
        soft = replace(normal, physician_id=3, physician_name="Soft", has_soft_academic_event=True)
        mondays = tuple(date(2027, 8, 2) + timedelta(days=7 * week) for week in range(4))
        assignments = {
            on_date: precepting_solver.SolverAssignment(
                index + 1,
                normal.physician_id,
                normal.physician_name,
                "late-doc",
                None,
                None,
                True,
            )
            for index, on_date in enumerate(mondays)
        }
        solver_input = self._late_doc_only_solver_input(
            mondays, (normal, backup, soft), assignments=assignments
        )

        adapter = precepting_solver._CpSatFillAdapter(
            solver_input, repair=True, solve_seed="repair-late-doc-policy"
        )
        proposal = adapter.solve()

        self.assertEqual(proposal.late_doc_assignments, ())
        self.assertFalse(proposal.unresolved_requirements)
        self.assertEqual(
            adapter.late_doc_band_totals,
            {normal.physician_id: 4, backup.physician_id: 0, soft.physician_id: 0},
        )

    def test_late_doc_overlap_uses_the_per_monday_upper_bound_without_global_proof(self):
        """Multiple PM choices per Monday must not force a whole-model overlap proof."""
        candidates = tuple(
            replace(
                self._tie_break_candidate(),
                physician_id=physician_id,
                physician_name=f"Faculty {physician_id}",
            )
            for physician_id in range(1, 27)
        )
        mondays = tuple(date(2027, 8, 2) + timedelta(days=7 * week) for week in range(13))
        solver_input = self._late_doc_only_solver_input(
            mondays,
            candidates,
            sessions=tuple(
                precepting_solver.SolverSession(
                    on_date,
                    "PM",
                    index + 1,
                    (
                        precepting_solver.SolverPosition(
                            "primary-1", True, None, candidates[2 * index : 2 * index + 2]
                        ),
                    ),
                    candidates[2 * index : 2 * index + 2],
                )
                for index, on_date in enumerate(mondays)
            ),
        )
        phase_timings = {}
        adapter = precepting_solver._CpSatFillAdapter(
            solver_input,
            solve_seed="overlap-bound",
            max_time_seconds=precepting_solver.SYNCHRONOUS_SOLVER_TIME_SECONDS,
            phase_timings=phase_timings,
        )

        def reject_global_proof(variables, **_):
            raise AssertionError("Late Doc overlap must not prove the complete model.")

        with patch.object(adapter, "_maximize_then_fix", side_effect=reject_global_proof):
            proposal = adapter.solve()

        self.assertEqual(len(proposal.late_doc_assignments), len(mondays))
        pm_by_date = {
            assignment.date: assignment.physician_id for assignment in proposal.assignments
        }
        self.assertEqual(
            sum(
                assignment.physician_id == pm_by_date[assignment.date]
                for assignment in proposal.late_doc_assignments
            ),
            13,
        )
        self.assertIn("Late Doc policy", phase_timings)

    def test_late_doc_overlap_proves_the_reduced_optimum_when_the_ceiling_is_unattainable(self):
        """Fairness and balance can rule out an otherwise possible Monday overlap."""
        first = replace(self._tie_break_candidate(), physician_id=1, physician_name="First")
        second = replace(self._tie_break_candidate(), physician_id=2, physician_name="Second")
        candidates = (first, second)
        mondays = tuple(date(2027, 8, 2) + timedelta(days=7 * week) for week in range(4))
        solver_input = precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(mondays[0], mondays[-1]),
            balance_window=SolveDateRange(mondays[0], mondays[-1]),
            sessions=tuple(
                precepting_solver.SolverSession(
                    on_date,
                    "PM",
                    index + 1,
                    (precepting_solver.SolverPosition("primary-1", True, None, candidates),),
                    candidates,
                )
                for index, on_date in enumerate(mondays)
            ),
            late_doc_positions=tuple(
                precepting_solver.SolverLateDocPosition(on_date, None, candidates, candidates)
                for on_date in mondays
            ),
            community_commitments=(),
            faculty_capacities=(
                precepting_solver.FacultyCapacity(1, "First", 3, 3 / 4, 1, 1 / 4),
                precepting_solver.FacultyCapacity(2, "Second", 1, 1 / 4, 3, 3 / 4),
            ),
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="reduced-late-doc-overlap",
        )
        adapter = precepting_solver._CpSatFillAdapter(solver_input, solve_seed="reduced-overlap")

        with patch.object(
            adapter,
            "_maximize_then_fix",
            side_effect=AssertionError("Late Doc overlap must not reopen the whole Fill model."),
        ):
            proposal = adapter.solve()

        self.assertEqual(len(proposal.late_doc_assignments), len(mondays))
        # The one-assignment Late Doc band (docs/design.md section 15.2) permits
        # two assignments for Second while its PM capacity makes three Monday
        # overlaps attainable.
        self.assertEqual(
            sum(item.physician_id == second.physician_id for item in proposal.late_doc_assignments),
            2,
        )
        pm_by_date = {
            assignment.date: assignment.physician_id for assignment in proposal.assignments
        }
        self.assertEqual(
            sum(
                assignment.physician_id == pm_by_date[assignment.date]
                for assignment in proposal.late_doc_assignments
            ),
            3,
        )

    def test_final_seeded_tie_break_retains_optimal_or_feasible_result(self):
        for status in (precepting_solver.cp_model.OPTIMAL, precepting_solver.cp_model.FEASIBLE):
            with self.subTest(status=status):
                adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
                selected = adapter.model.NewBoolVar("selected")
                requirement = self._tie_break_requirement()
                candidate = self._tie_break_candidate()
                adapter.variables[requirement, candidate] = selected
                solver = self._solver_with_status(status)

                with patch.object(adapter, "_solver", return_value=solver):
                    result = adapter._resolve_seeded_ties()

                self.assertIs(result, solver)
                self.assertEqual(adapter._solve_selected(result), {requirement: candidate})

    def test_final_seeded_tie_break_warm_starts_from_last_policy_solution(self):
        adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
        unrelated = adapter.model.NewBoolVar("unrelated")
        auxiliary = adapter.model.NewIntVar(0, 2, "auxiliary")
        selected = adapter.model.NewBoolVar("selected")
        adapter.model.Add(auxiliary == selected + 1)
        adapter.variables[self._tie_break_requirement(), self._tie_break_candidate()] = selected
        policy_values = {unrelated.Index(): 0, selected.Index(): 1, auxiliary.Index(): 2}
        policy_solver = self._solver_with_status(
            precepting_solver.cp_model.FEASIBLE,
            value=lambda variable: policy_values[variable.Index()],
        )

        with (
            patch.object(adapter, "_solver", return_value=policy_solver),
        ):
            adapter._maximize_then_fix([selected])

        received_hint = []
        tie_break_solver = self._solver_with_status(
            precepting_solver.cp_model.FEASIBLE, received_hint=received_hint
        )
        with (
            patch.object(adapter, "_solver", return_value=tie_break_solver),
        ):
            adapter._resolve_seeded_ties()

        received_by_index = dict(received_hint)
        self.assertEqual(
            tuple(received_by_index[variable.Index()] for variable in (selected, auxiliary)),
            (1, 2),
        )

    def test_fairness_warm_starts_from_last_policy_solution(self):
        adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
        unrelated = adapter.model.NewBoolVar("unrelated")
        auxiliary = adapter.model.NewIntVar(0, 2, "auxiliary")
        selected = adapter.model.NewBoolVar("selected")
        adapter.model.Add(auxiliary == selected + 1)
        adapter.variables[self._tie_break_requirement(), self._tie_break_candidate()] = selected
        policy_values = {unrelated.Index(): 0, selected.Index(): 1, auxiliary.Index(): 2}
        policy_solver = self._solver_with_status(
            precepting_solver.cp_model.FEASIBLE,
            value=lambda variable: policy_values[variable.Index()],
        )

        with (
            patch.object(adapter, "_solver", return_value=policy_solver),
        ):
            adapter._maximize_then_fix([selected])

        received_hint = []
        fairness_solver = self._solver_with_status(
            precepting_solver.cp_model.FEASIBLE, received_hint=received_hint
        )
        with (
            patch.object(adapter, "_solver", return_value=fairness_solver),
        ):
            adapter._minimize_then_fix([selected], fairness_stage=True)

        received_by_index = dict(received_hint)
        self.assertEqual(
            tuple(received_by_index[variable.Index()] for variable in (selected, auxiliary)),
            (1, 2),
        )

    def test_solutionless_final_seeded_tie_break_fails_clearly(self):
        statuses = {
            precepting_solver.cp_model.UNKNOWN: "search limit",
            precepting_solver.cp_model.INFEASIBLE: "no final seeded tie-break outcome",
            precepting_solver.cp_model.MODEL_INVALID: "invalid model",
        }
        for status, detail in statuses.items():
            with self.subTest(status=status):
                adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
                adapter.variables[self._tie_break_requirement(), self._tie_break_candidate()] = (
                    adapter.model.NewBoolVar("selected")
                )

                with (
                    patch.object(adapter, "_solver", return_value=self._solver_with_status(status)),
                    self.assertRaises(ValidationError) as raised,
                ):
                    adapter._resolve_seeded_ties()
                self.assertIn("final seeded tie-break", str(raised.exception))
                self.assertIn(detail, str(raised.exception))

    def test_feasible_fairness_continues_through_late_doc_policy_to_a_proposal(self):
        monday = date(2027, 8, 2)
        first = replace(self._tie_break_candidate(), physician_id=1, physician_name="First")
        second = replace(self._tie_break_candidate(), physician_id=2, physician_name="Second")
        candidates = (first, second)
        solver_input = precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(monday, monday),
            balance_window=SolveDateRange(monday, monday),
            sessions=(
                precepting_solver.SolverSession(
                    monday,
                    "PM",
                    1,
                    (precepting_solver.SolverPosition("primary-1", True, None, candidates),),
                    candidates,
                ),
            ),
            late_doc_positions=(
                precepting_solver.SolverLateDocPosition(monday, None, candidates, candidates),
            ),
            community_commitments=(),
            faculty_capacities=(
                precepting_solver.FacultyCapacity(1, "First", 1, 1 / 2, 1, 1 / 2),
                precepting_solver.FacultyCapacity(2, "Second", 1, 1 / 2, 1, 1 / 2),
            ),
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="feasible-fairness-continuation",
        )
        adapter = precepting_solver._CpSatFillAdapter(solver_input, solve_seed="continuation")
        original_solver = adapter._solver
        observed_solves = []

        class FeasibleFairnessSolver:
            def __init__(self, solver):
                self.solver = solver
                self.parameters = solver.parameters
                self.status = None

            def Solve(self, model):
                observed_solves.append(
                    (
                        adapter.current_phase,
                        self.parameters.max_time_in_seconds,
                        self.parameters.num_search_workers,
                    )
                )
                actual_status = self.solver.Solve(model)
                self.status = (
                    precepting_solver.cp_model.FEASIBLE
                    if adapter.current_phase == "Faculty fairness"
                    and actual_status
                    in (precepting_solver.cp_model.OPTIMAL, precepting_solver.cp_model.FEASIBLE)
                    else actual_status
                )
                return self.status

            def Value(self, variable):
                return self.solver.Value(variable)

            def ResponseProto(self):
                return SimpleNamespace(status=self.status)

        def solver_for_stage(**kwargs):
            return FeasibleFairnessSolver(original_solver(**kwargs))

        with patch.object(adapter, "_solver", side_effect=solver_for_stage):
            proposal = adapter.solve()

        fairness_solves = [
            (time_limit, workers)
            for phase, time_limit, workers in observed_solves
            if phase == "Faculty fairness"
        ]
        late_doc_solves = [
            (time_limit, workers)
            for phase, time_limit, workers in observed_solves
            if phase == "Late Doc policy"
        ]
        tie_break_solves = [
            (time_limit, workers)
            for phase, time_limit, workers in observed_solves
            if phase == "seeded tie-break"
        ]

        self.assertTrue(
            any(time_limit == 3 and workers in (1, 2) for time_limit, workers in fairness_solves)
        )
        self.assertTrue(
            any(
                time_limit == float("inf") and workers in (1, 2)
                for time_limit, workers in late_doc_solves
            )
        )
        self.assertTrue(
            any(time_limit == 1 and workers == 1 for time_limit, workers in tie_break_solves)
        )
        self.assertEqual([item.date for item in proposal.late_doc_assignments], [monday])
        self.assertIn("Late Doc policy", adapter.phase_timings)

    def test_feasible_fairness_value_is_fixed_before_later_policy_runs(self):
        adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
        fairness_value = adapter.model.NewBoolVar("fairness")
        solver = self._solver_with_status(precepting_solver.cp_model.FEASIBLE)

        with patch.object(adapter, "_solver", return_value=solver):
            adapter._minimize_then_fix([fairness_value], fairness_stage=True)

        verification_solver = precepting_solver.cp_model.CpSolver()
        self.assertEqual(
            verification_solver.Solve(adapter.model), precepting_solver.cp_model.OPTIMAL
        )
        self.assertEqual(verification_solver.Value(fairness_value), 1)

    def test_fairness_stage_distinguishes_solver_outcomes_in_scheduling_language(self):
        statuses = {
            precepting_solver.cp_model.UNKNOWN: "search limit",
            precepting_solver.cp_model.INFEASIBLE: "no proportional Faculty workload fairness outcome",
            precepting_solver.cp_model.MODEL_INVALID: "invalid model",
        }
        for status, detail in statuses.items():
            with self.subTest(status=status):
                adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())

                with (
                    patch.object(adapter, "_solver", return_value=self._solver_with_status(status)),
                    self.assertRaises(ValidationError) as raised,
                ):
                    adapter._minimize_then_fix(
                        [adapter.model.NewBoolVar("fairness")], fairness_stage=True
                    )
                self.assertIn("proportional Faculty workload fairness", str(raised.exception))
                self.assertIn(detail, str(raised.exception))

    def test_fairness_stage_diagnostics_distinguish_valid_and_best_outcomes(self):
        outcomes = {
            precepting_solver.cp_model.FEASIBLE: "valid",
            precepting_solver.cp_model.OPTIMAL: "best",
        }
        for status, outcome in outcomes.items():
            with self.subTest(status=status):
                adapter = precepting_solver._CpSatFillAdapter(self._empty_solver_input())
                fairness = adapter.model.NewBoolVar("fairness")
                solver = self._solver_with_status(status, value=lambda variable: 0)
                with (
                    patch.object(adapter, "_solver", return_value=solver),
                    self.assertLogs("muriel", level="INFO") as logs,
                ):
                    adapter._minimize_then_fix([fairness], fairness_stage=True)

                self.assertTrue(
                    any(
                        f"stage=faculty_fairness outcome={outcome}" in output
                        for output in logs.output
                    )
                )

    @staticmethod
    def _empty_solver_input(*, faculty_capacities=()):
        return precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(date(2027, 8, 1), date(2027, 8, 1)),
            balance_window=SolveDateRange(date(2027, 8, 1), date(2027, 8, 1)),
            sessions=(),
            late_doc_positions=(),
            community_commitments=(),
            faculty_capacities=faculty_capacities,
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="timing-test",
        )

    @staticmethod
    def _tie_break_requirement():
        return precepting_solver._FillRequirement(date(2027, 8, 1), "AM", 1, "primary-1", (), ())

    @staticmethod
    def _tie_break_candidate():
        return precepting_solver.SolverCandidate(
            1,
            "Faculty",
            ("faculty",),
            "available",
            "available",
            True,
            True,
            False,
            None,
            None,
            (),
        )

    @staticmethod
    def _solver_with_status(status, *, value=None, received_hint=None):
        solver = SimpleNamespace()
        value = value or (lambda variable: 1)

        def solve(model):
            if received_hint is not None:
                received_hint.extend(
                    zip(
                        model.Proto().solution_hint.vars,
                        model.Proto().solution_hint.values,
                        strict=True,
                    )
                )
            return status

        solver.Solve = solve
        solver.Value = value
        solver.ResponseProto = lambda: SimpleNamespace(status=status)
        return solver

    @staticmethod
    def _late_doc_only_solver_input(mondays, candidates, *, assignments=None, sessions=()):
        assignments = assignments or {}
        return precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(mondays[0], mondays[-1]),
            balance_window=SolveDateRange(mondays[0], mondays[-1]),
            sessions=sessions,
            late_doc_positions=tuple(
                precepting_solver.SolverLateDocPosition(
                    on_date,
                    assignments.get(on_date),
                    candidates,
                    candidates,
                )
                for on_date in mondays
            ),
            community_commitments=(),
            faculty_capacities=tuple(
                precepting_solver.FacultyCapacity(
                    physician_id=candidate.physician_id,
                    physician_name=candidate.physician_name,
                    precepting_capacity=1,
                    precepting_share=1 / len(candidates),
                    late_doc_capacity=1,
                    late_doc_share=1 / len(candidates),
                )
                for candidate in candidates
            ),
            balance_assignments=(),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="mixed-late-doc-policy",
        )


class PreceptorPositionPreferenceTests(SimpleTestCase):
    on_date = date(2027, 8, 1)

    @staticmethod
    def candidate(physician_id, name, *, assignment_id=None):
        return precepting_solver.SolverCandidate(
            physician_id,
            name,
            (EffectiveRole.Role.FACULTY,),
            "available",
            "available",
            True,
            True,
            False,
            assignment_id,
            None,
            (),
        )

    def solver_input(self, positions, candidates, *, balance_assignments=()):
        return precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(self.on_date, self.on_date),
            balance_window=SolveDateRange(self.on_date, self.on_date),
            sessions=(
                precepting_solver.SolverSession(
                    self.on_date,
                    "AM",
                    1,
                    positions,
                    candidates,
                ),
            ),
            late_doc_positions=(),
            community_commitments=(),
            faculty_capacities=(),
            balance_assignments=balance_assignments,
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="preceptor-position-preference",
        )

    def test_fill_prefers_primary_one_across_seeds_when_only_one_position_can_fill(self):
        physician = self.candidate(1, "Only Faculty")
        solver_input = self.solver_input(
            (
                precepting_solver.SolverPosition("primary-1", True, None),
                precepting_solver.SolverPosition("primary-2", True, None),
            ),
            (physician,),
        )

        for seed in ("first", "second", "third"):
            proposal = produce_fill_proposal(solver_input, solve_seed=seed)

            self.assertEqual(
                [(item.position, item.physician_id) for item in proposal.assignments],
                [("primary-1", physician.physician_id)],
            )
            self.assertEqual(
                [(item.position, item.code) for item in proposal.unresolved_requirements],
                [("primary-2", "missing_primary_coverage")],
            )

    def test_fill_keeps_both_positions_filled_and_does_not_move_existing_primary_two(self):
        first = self.candidate(1, "First")
        second = self.candidate(2, "Second")
        empty_input = self.solver_input(
            (
                precepting_solver.SolverPosition("primary-1", True, None),
                precepting_solver.SolverPosition("primary-2", True, None),
            ),
            (first, second),
        )
        existing = precepting_solver.SolverAssignment(
            3, first.physician_id, first.physician_name, "primary", 2, False, True
        )
        existing_input = self.solver_input(
            (
                precepting_solver.SolverPosition("primary-1", True, None),
                precepting_solver.SolverPosition("primary-2", True, existing),
            ),
            (self.candidate(1, "First", assignment_id=existing.id), second),
            balance_assignments=(existing,),
        )

        filled = produce_fill_proposal(empty_input)
        existing_proposal = produce_fill_proposal(existing_input)

        self.assertEqual({item.position for item in filled.assignments}, {"primary-1", "primary-2"})
        self.assertFalse(filled.unresolved_requirements)
        self.assertEqual(
            [(item.position, item.physician_id) for item in existing_proposal.assignments],
            [("primary-1", second.physician_id)],
        )
        self.assertFalse(existing_proposal.unresolved_requirements)

    def test_fill_keeps_a_higher_priority_forced_primary_two_assignment(self):
        physician = self.candidate(1, "Required Faculty")
        solver_input = self.solver_input(
            (
                precepting_solver.SolverPosition("primary-1", True, None),
                precepting_solver.SolverPosition("primary-2", True, None),
            ),
            (physician,),
        )

        proposal = precepting_solver._CpSatFillAdapter(
            solver_input,
            forced_choices=((self.on_date, "AM", "primary-2", physician.physician_id),),
        ).solve()

        self.assertEqual(
            [(item.position, item.physician_id) for item in proposal.assignments],
            [("primary-2", physician.physician_id)],
        )
        self.assertEqual(
            [(item.position, item.code) for item in proposal.unresolved_requirements],
            [("primary-1", "missing_primary_coverage")],
        )

    def test_repair_keeps_primary_two_when_moving_it_would_add_changes(self):
        existing = precepting_solver.SolverAssignment(1, 1, "Existing", "primary", 2, False, True)
        solver_input = self.solver_input(
            (
                precepting_solver.SolverPosition("primary-1", True, None),
                precepting_solver.SolverPosition("primary-2", True, existing),
            ),
            (self.candidate(1, "Existing", assignment_id=existing.id),),
            balance_assignments=(existing,),
        )

        proposal = produce_repair_proposal(solver_input)

        self.assertFalse(proposal.assignments)
        self.assertEqual(
            [(item.position, item.code) for item in proposal.unresolved_requirements],
            [("primary-1", "missing_primary_coverage")],
        )


class PreceptingSolverInputTests(TestCase):
    week = date(2025, 2, 3)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def faculty(self, name, *, start=date(2025, 1, 1), availability="open", slots=None):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=start,
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=start,
            effective_end=None,
            slots=slots
            or {slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def ready_week(self, week=None):
        week = week or self.week
        rounder = self.faculty("Rounder")
        record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
        open_weekly_draft(week)
        return rounder

    def preflight(self, solve_range, **kwargs):
        with patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2025, 1, 1)
        ):
            return preflight_precepting_solve(solve_range, **kwargs)

    def test_current_capacity_override_is_fixed_point_prorated_and_stales_input(self):
        computed = self.faculty("Computed")
        custom = self.faculty("Custom")
        set_faculty_precepting_capacity_override(
            physician=custom,
            capacity_override=Decimal("4.5"),
            scheduler=self.scheduler,
        )
        open_weekly_draft(self.week)

        full = self.preflight(
            SolveDateRange(self.week, date(2025, 2, 7)),
            balance_window=SolveDateRange(self.week, date(2025, 2, 7)),
        ).solver_input
        partial = self.preflight(
            SolveDateRange(date(2025, 2, 5), date(2025, 2, 6)),
            balance_window=SolveDateRange(date(2025, 2, 5), date(2025, 2, 6)),
        ).solver_input
        full_capacities = {item.physician_id: item for item in full.faculty_capacities}
        partial_capacities = {item.physician_id: item for item in partial.faculty_capacities}

        self.assertEqual(full_capacities[computed.pk].precepting_capacity, Decimal("10"))
        self.assertEqual(full_capacities[custom.pk].precepting_capacity, Decimal("4.5"))
        self.assertEqual(full_capacities[custom.pk].precepting_capacity_units, 450)
        proposal = produce_fill_proposal(full, solve_seed="capacity-override")
        workloads = {item.physician_id: item for item in proposal.compromises.faculty_workloads}
        self.assertGreater(workloads[computed.pk].target_share, workloads[custom.pk].target_share)
        self.assertAlmostEqual(workloads[custom.pk].target_share, 4.5 / 14.5)
        self.assertEqual(partial_capacities[custom.pk].precepting_capacity, Decimal("1.8"))
        self.assertEqual(partial_capacities[custom.pk].precepting_capacity_units, 180)
        self.assertEqual(full_capacities[custom.pk].late_doc_capacity, 1)
        self.assertEqual(
            next(
                candidate.availability
                for session in full.sessions
                for candidate in session.candidates
                if candidate.physician_id == custom.pk
            ),
            "available",
        )

        set_faculty_precepting_capacity_override(
            physician=custom,
            capacity_override=Decimal("5.0"),
            scheduler=self.scheduler,
        )
        changed = self.preflight(
            SolveDateRange(self.week, date(2025, 2, 7)),
            balance_window=SolveDateRange(self.week, date(2025, 2, 7)),
        ).solver_input
        self.assertNotEqual(full.fingerprint, changed.fingerprint)

    def test_range_helpers_normalize_month_quarter_custom_and_default_balance_window(self):
        self.assertEqual(
            month_solve_range(date(2025, 2, 17)),
            SolveDateRange(date(2025, 2, 1), date(2025, 2, 28)),
        )
        self.assertEqual(
            quarter_solve_range(2025, 2), SolveDateRange(date(2025, 10, 1), date(2025, 12, 31))
        )
        self.assertEqual(
            [quarter_solve_range(2025, quarter) for quarter in (1, 2, 3, 4)],
            [
                SolveDateRange(date(2025, 7, 1), date(2025, 9, 30)),
                SolveDateRange(date(2025, 10, 1), date(2025, 12, 31)),
                SolveDateRange(date(2026, 1, 1), date(2026, 3, 31)),
                SolveDateRange(date(2026, 4, 1), date(2026, 6, 30)),
            ],
        )
        self.assertEqual(
            custom_solve_range(date(2025, 12, 4), date(2025, 12, 12)).start_date, date(2025, 12, 4)
        )
        self.assertEqual(
            default_balance_window(SolveDateRange(date(2025, 10, 1), date(2025, 12, 31))),
            SolveDateRange(date(2025, 7, 1), date(2025, 12, 31)),
        )
        self.assertEqual(
            default_balance_window(SolveDateRange(date(2025, 8, 1), date(2026, 1, 31))),
            SolveDateRange(date(2025, 8, 1), date(2026, 1, 31)),
        )
        self.assertEqual(
            default_balance_window(SolveDateRange(date(2025, 6, 15), date(2025, 12, 12))),
            SolveDateRange(date(2025, 6, 1), date(2025, 12, 31)),
        )
        solve_range = SolveDateRange(date(2028, 2, 1), date(2028, 2, 29))
        self.assertEqual(
            balance_window_for_span(solve_range, "1-day"),
            SolveDateRange(date(2028, 2, 29), date(2028, 2, 29)),
        )
        self.assertEqual(
            balance_window_for_span(solve_range, "2-weeks"),
            SolveDateRange(date(2028, 2, 16), date(2028, 2, 29)),
        )
        self.assertEqual(
            balance_window_for_span(solve_range, "1-month"),
            SolveDateRange(date(2028, 1, 30), date(2028, 2, 29)),
        )
        self.assertEqual(
            balance_window_for_span(solve_range, "1-year"),
            SolveDateRange(date(2027, 3, 1), date(2028, 2, 29)),
        )
        self.assertEqual(
            balance_window_for_span(solve_range, "custom", custom_days=45),
            SolveDateRange(date(2028, 1, 16), date(2028, 2, 29)),
        )

    def test_capacity_uses_calendar_month_boundaries(self):
        accepted_solve_range = SolveDateRange(date(2027, 1, 31), date(2027, 4, 29))
        accepted_balance_window = SolveDateRange(date(2025, 2, 1), date(2027, 1, 31))

        self.assertIsNone(
            validate_precepting_solver_capacity(
                accepted_solve_range, balance_window=accepted_balance_window
            )
        )
        with self.assertRaisesMessage(ValidationError, "at most 3 calendar months"):
            validate_precepting_solver_capacity(
                SolveDateRange(date(2027, 1, 31), date(2027, 4, 30)),
                balance_window=accepted_balance_window,
            )
        with self.assertRaisesMessage(ValidationError, "at most 24 calendar months"):
            validate_precepting_solver_capacity(
                accepted_solve_range,
                balance_window=SolveDateRange(date(2025, 2, 1), date(2027, 2, 1)),
            )
        with (
            patch(
                "schedules.services.precepting_solver.slot_projection",
                side_effect=AssertionError("oversized input must not project slots"),
            ),
            self.assertRaisesMessage(ValidationError, "at most 3 calendar months"),
        ):
            self.preflight(
                SolveDateRange(date(2027, 1, 31), date(2027, 4, 30)),
                balance_window=accepted_balance_window,
            )

    def test_clip_solve_range_uses_organization_today_for_actionable_dates(self):
        today = date(2026, 8, 26)
        selections = (
            (
                SolveDateRange(date(2026, 8, 1), date(2026, 8, 31)),
                SolveDateRange(today, date(2026, 8, 31)),
            ),
            (SolveDateRange(today, date(2026, 8, 31)), SolveDateRange(today, date(2026, 8, 31))),
            (
                SolveDateRange(date(2026, 8, 27), date(2026, 8, 31)),
                SolveDateRange(date(2026, 8, 27), date(2026, 8, 31)),
            ),
            (SolveDateRange(date(2026, 8, 1), date(2026, 8, 25)), None),
        )

        with patch("schedules.services.precepting_solver.organization_today", return_value=today):
            for selected, expected in selections:
                with self.subTest(selected=selected):
                    self.assertEqual(clip_solve_range_at_today(selected), expected)

    def test_projection_prorates_mid_window_faculty_start_and_monthly_override(self):
        established = self.faculty("Established")
        mid_window = self.faculty("Mid-window", start=date(2025, 2, 1))
        self.ready_week()
        create_monthly_physician_availability_rule(
            physician=established,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
            scheduler=self.scheduler,
        )

        result = self.preflight(
            SolveDateRange(self.week, date(2025, 2, 7)),
            balance_window=SolveDateRange(date(2025, 1, 1), date(2025, 2, 28)),
        )
        monday_am = next(
            session
            for session in result.solver_input.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        candidates = {candidate.physician_id: candidate for candidate in monday_am.candidates}
        capacities = {
            capacity.physician_id: capacity for capacity in result.solver_input.faculty_capacities
        }

        self.assertEqual(candidates[established.pk].availability, "backup_only")
        self.assertEqual(candidates[established.pk].base_availability, "backup_only")
        self.assertGreater(
            capacities[established.pk].precepting_capacity,
            capacities[mid_window.pk].precepting_capacity,
        )
        self.assertGreater(capacities[mid_window.pk].precepting_capacity, 0)

    def test_patient_care_is_backup_eligible_but_external_patient_care_is_excluded(self):
        ordinary = self.faculty("Ordinary Patient Care", availability="patient_care")
        external = self.faculty("External Patient Care", availability="patient_care")
        role = add_role(
            physician=external,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start=self.week,
            effective_end=self.week,
            scheduler=self.scheduler,
        )
        self.ready_week()

        result = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))
        monday_am = next(
            session
            for session in result.solver_input.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        candidates = {
            candidate.physician_id: candidate for candidate in monday_am.positions[0].candidates
        }

        self.assertEqual(candidates[ordinary.pk].availability, "backup_only")
        self.assertTrue(candidates[ordinary.pk].automatic_eligible)
        self.assertEqual(candidates[external.pk].availability, "not_available")
        self.assertFalse(candidates[external.pk].automatic_eligible)
        self.assertIn(("external_patient_care", role.pk), candidates[external.pk].source_ids)

    def test_projection_retains_locks_sibling_occupancy_backup_and_community_commitments(self):
        assigned = self.faculty("Locked")
        interviewer = self.faculty("Interviewer")
        backup = self.faculty("Backup", availability="backup_only")
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        self.ready_week()
        session = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assignment = assign_preceptor(
            session=session,
            physician=assigned,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        late_doc_assignment = assign_late_doc(
            on_date=self.week, physician=assigned, scheduler=self.scheduler
        )
        interview = create_interview_shift(
            academic_year=2024, date=self.week, scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=interviewer, scheduler=self.scheduler)
        unlocked = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))
        set_preceptor_assignment_lock(
            assignment=assignment,
            is_locked=True,
            scheduler=self.scheduler,
        )

        result = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))
        projected = next(
            item
            for item in result.solver_input.sessions
            if item.date == self.week and item.period_of_day == "AM"
        )
        unlocked_projected = next(
            item
            for item in unlocked.solver_input.sessions
            if item.date == self.week and item.period_of_day == "AM"
        )
        candidates = {candidate.physician_id: candidate for candidate in projected.candidates}
        unlocked_position = unlocked_projected.positions[0]
        unlocked_late_doc = unlocked.solver_input.late_doc_positions[0]
        late_doc = result.solver_input.late_doc_positions[0]

        self.assertEqual(projected.positions[0].assignment.id, assignment.pk)
        self.assertTrue(projected.positions[0].assignment.is_locked)
        self.assertFalse(unlocked_position.assignment.is_locked)
        self.assertEqual(unlocked_late_doc.assignment.id, late_doc_assignment.pk)
        self.assertIsNone(unlocked_late_doc.assignment.is_locked)
        self.assertIsNone(late_doc.assignment.is_locked)
        self.assertNotEqual(unlocked.solver_input.fingerprint, result.solver_input.fingerprint)
        self.assertEqual(candidates[interviewer.pk].interview_shift_id, interview.pk)
        self.assertEqual(candidates[backup.pk].availability, "backup_only")
        self.assertTrue(candidates[community.pk].expected_community_commitment)
        self.assertEqual(
            result.solver_input.community_commitments[0].physician_id,
            community.pk,
        )

    def test_missing_normal_rounder_is_a_named_preflight_blocker_not_scarcity(self):
        self.faculty("Available")
        open_weekly_draft(self.week)

        result = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))

        self.assertEqual([blocker.code for blocker in result.blockers], ["missing_normal_rounder"])
        self.assertEqual(len(result.solver_input.late_doc_positions), 1)
        self.assertIsNone(result.solver_input.sessions[0].positions[0].assignment)

    def test_no_physicians_is_ordinary_scarcity_with_empty_candidate_sets(self):
        result = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))

        self.assertEqual(len(result.solver_input.sessions), 10)
        self.assertTrue(all(not session.candidates for session in result.solver_input.sessions))
        self.assertEqual(len(result.solver_input.late_doc_positions), 1)
        self.assertFalse(result.solver_input.late_doc_positions[0].candidates)
        self.assertEqual(
            [blocker.code for blocker in result.blockers],
            ["missing_preceptor_sessions", "missing_normal_rounder"],
        )

    def test_past_dates_and_missing_sessions_are_named_preflight_blockers(self):
        self.faculty("Available")
        with patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2025, 2, 4)
        ):
            result = preflight_precepting_solve(SolveDateRange(self.week, date(2025, 2, 7)))

        self.assertEqual(
            [blocker.code for blocker in result.blockers],
            ["past_mutation_range", "missing_preceptor_sessions", "missing_normal_rounder"],
        )


class EffectiveDateFairnessBoundaryTests(TestCase):
    balance_window = SolveDateRange(date(2025, 2, 3), date(2025, 2, 7))
    solve_range = SolveDateRange(date(2025, 2, 5), date(2025, 2, 6))

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        open_weekly_draft(self.balance_window.start_date)

    def _faculty(
        self,
        name,
        *,
        role_start=date(2025, 1, 1),
        role_end=None,
        template_start=date(2025, 1, 1),
        template_end=None,
    ):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=role_start,
            effective_end=role_end,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=template_start,
            effective_end=template_end,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def _anchor_and_rounder(self):
        anchor = self._faculty("Anchor")
        rounder = self._faculty("Rounding physician")
        record_normal_rounder(
            week=self.balance_window.start_date,
            physician=rounder,
            scheduler=self.scheduler,
        )
        return anchor, rounder

    def _assign(self, physician, on_date, period, position):
        session = PreceptorSession.objects.get(date=on_date, period_of_day=period)
        return assign_preceptor(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=position,
            scheduler=self.scheduler,
        )

    def _create_academic_event(self, physician):
        definition = create_academic_event_definition(
            base_name="Effective-date fairness meeting",
            start_academic_year=2024,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.solve_range.start_date,
            end_date=self.solve_range.start_date,
            participants=[physician],
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )

    def _create_monthly_academic_event(self):
        definition = create_academic_event_definition(
            base_name="Effective-date fairness monthly meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            all_available_faculty=True,
            scheduler=self.scheduler,
        )
        return create_academic_event_monthly_rule(
            definition=definition,
            weekday=2,
            ordinals=[1],
            scheduler=self.scheduler,
        )

    def _preflight_and_fill(self):
        with patch(
            "schedules.services.precepting_solver.organization_today",
            return_value=date(2025, 1, 1),
        ):
            result = preflight_precepting_solve(
                self.solve_range, balance_window=self.balance_window
            )
        self.assertTrue(result.ready)
        return result.solver_input, produce_fill_proposal(result.solver_input, solve_seed="dates")

    def _candidate(self, solver_input, physician, on_date, period):
        session = next(
            item
            for item in solver_input.sessions
            if item.date == on_date and item.period_of_day == period
        )
        return next(item for item in session.candidates if item.physician_id == physician.pk)

    def _proposed_slots(self, proposal, physician):
        return [
            (assignment.date, assignment.period_of_day)
            for assignment in proposal.assignments
            if assignment.physician_id == physician.pk
        ]

    def _assert_boundary_result(
        self,
        solver_input,
        proposal,
        physician,
        *,
        capacity,
        denominator,
        existing,
        available_slots,
        proposed,
        final,
        target,
        fairness_deviation,
    ):
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        workloads = {item.physician_id: item for item in proposal.compromises.faculty_workloads}
        workload = workloads[physician.pk]

        self.assertEqual(capacities[physician.pk].precepting_capacity, capacity)
        self.assertEqual(
            sum(item.precepting_capacity for item in solver_input.faculty_capacities), denominator
        )
        self.assertEqual(workload.current_total, existing)
        proposed_slots = self._proposed_slots(proposal, physician)
        self.assertEqual(len(proposed_slots), proposed)
        self.assertTrue(set(proposed_slots).issubset(available_slots))
        self.assertEqual(workload.proposed_total, final)
        self.assertAlmostEqual(workload.target_total, target)

        total = sum(item.proposed_total for item in workloads.values())
        achieved_deviation = sum(
            abs(
                workloads[physician_id].proposed_total * denominator
                - item.precepting_capacity * total
            )
            for physician_id, item in capacities.items()
        )
        self.assertEqual(achieved_deviation, fairness_deviation)

    def test_monthly_rule_dates_independently_control_capacity_and_proposal_eligibility(self):
        self._anchor_and_rounder()
        self._faculty("Monthly rule coverage")
        physician = self._faculty("Effective monthly rules")
        active_rule = create_monthly_physician_availability_rule(
            physician=physician,
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            effective_start=date(2025, 2, 5),
            effective_end=date(2025, 2, 5),
            scheduler=self.scheduler,
        )
        expired_rule = create_monthly_physician_availability_rule(
            physician=physician,
            weekday=3,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            effective_end=date(2025, 2, 5),
            scheduler=self.scheduler,
        )
        future_rule = create_monthly_physician_availability_rule(
            physician=physician,
            weekday=3,
            ordinals=[1],
            periods=["PM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            effective_start=date(2025, 2, 7),
            scheduler=self.scheduler,
        )

        solver_input, proposal = self._preflight_and_fill()
        blocked = self._candidate(solver_input, physician, date(2025, 2, 5), "AM")
        after_end = self._candidate(solver_input, physician, date(2025, 2, 6), "AM")
        before_start = self._candidate(solver_input, physician, date(2025, 2, 6), "PM")

        self.assertEqual(blocked.base_availability, "off")
        self.assertEqual(blocked.availability, "not_available")
        self.assertIn(("monthly_availability_rule", active_rule.pk), blocked.source_ids)
        self.assertEqual(after_end.base_availability, "open")
        self.assertNotIn(("monthly_availability_rule", expired_rule.pk), after_end.source_ids)
        self.assertEqual(before_start.base_availability, "open")
        self.assertNotIn(("monthly_availability_rule", future_rule.pk), before_start.source_ids)
        available_slots = [
            (date(2025, 2, 5), "PM"),
            (date(2025, 2, 6), "AM"),
            (date(2025, 2, 6), "PM"),
        ]
        self._assert_boundary_result(
            solver_input,
            proposal,
            physician,
            capacity=9,
            denominator=39,
            existing=0,
            available_slots=available_slots,
            proposed=2,
            final=2,
            target=24 / 13,
            fairness_deviation=160,
        )
        self.assertNotIn((date(2025, 2, 5), "AM"), self._proposed_slots(proposal, physician))

    def test_academic_event_blocks_a_proposal_without_reducing_base_capacity(self):
        anchor, _ = self._anchor_and_rounder()
        self._faculty("Academic event coverage one")
        self._faculty("Academic event coverage two")
        self._create_academic_event(anchor)

        solver_input, proposal = self._preflight_and_fill()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        workloads = {item.physician_id: item for item in proposal.compromises.faculty_workloads}
        blocked = self._candidate(solver_input, anchor, date(2025, 2, 5), "AM")

        self.assertEqual(capacities[anchor.pk].precepting_capacity, 10)
        self.assertAlmostEqual(workloads[anchor.pk].target_total, 2.0)
        self.assertEqual(blocked.base_availability, "open")
        self.assertEqual(blocked.availability, "not_available")
        self.assertTrue(
            any(source_type == "academic_event" for source_type, _ in blocked.source_ids)
        )
        self.assertNotIn((date(2025, 2, 5), "AM"), self._proposed_slots(proposal, anchor))

    def test_monthly_academic_event_blocks_a_proposal_with_its_durable_source(self):
        anchor, _ = self._anchor_and_rounder()
        self._faculty("Monthly academic event coverage one")
        self._faculty("Monthly academic event coverage two")
        rule = self._create_monthly_academic_event()

        solver_input, proposal = self._preflight_and_fill()
        event = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 2, 5))
        blocked = self._candidate(solver_input, anchor, date(2025, 2, 5), "AM")

        self.assertEqual(blocked.base_availability, "open")
        self.assertEqual(blocked.availability, "not_available")
        self.assertIn(("academic_event", event.pk), blocked.source_ids)
        self.assertNotIn((date(2025, 2, 5), "AM"), self._proposed_slots(proposal, anchor))

    def test_normal_rounding_blocks_proposals_without_reducing_base_capacity(self):
        _, rounder = self._anchor_and_rounder()
        self._faculty("Rounding coverage")

        solver_input, proposal = self._preflight_and_fill()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        workloads = {item.physician_id: item for item in proposal.compromises.faculty_workloads}

        self.assertEqual(capacities[rounder.pk].precepting_capacity, 10)
        self.assertAlmostEqual(workloads[rounder.pk].target_total, 8 / 3)
        for session in solver_input.sessions:
            candidate = self._candidate(solver_input, rounder, session.date, session.period_of_day)
            self.assertEqual(candidate.base_availability, "open")
            self.assertEqual(candidate.availability, "not_available")
            self.assertTrue(
                any(source_type == "normal_rounder" for source_type, _ in candidate.source_ids)
            )
        self.assertEqual(self._proposed_slots(proposal, rounder), [])

    def test_template_start_independently_controls_capacity_and_proposal_eligibility(self):
        self._anchor_and_rounder()
        physician = self._faculty(
            "Template starts during solve",
            template_start=date(2025, 2, 6),
        )

        solver_input, proposal = self._preflight_and_fill()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        before_start = self._candidate(solver_input, physician, date(2025, 2, 5), "AM")
        at_start = self._candidate(solver_input, physician, date(2025, 2, 6), "AM")
        available_slots = [(date(2025, 2, 6), "AM"), (date(2025, 2, 6), "PM")]

        self.assertTrue(before_start.is_faculty)
        self.assertEqual(before_start.base_availability, "off")
        self.assertEqual(before_start.availability, "not_available")
        self.assertTrue(at_start.is_faculty)
        self.assertEqual(at_start.base_availability, "open")
        self.assertEqual(capacities[physician.pk].precepting_capacity, 4)
        self.assertEqual(
            sum(item.precepting_capacity for item in solver_input.faculty_capacities), 24
        )
        proposed_slots = self._proposed_slots(proposal, physician)
        self.assertTrue(proposed_slots)
        self.assertTrue(set(proposed_slots).issubset(available_slots))

    def test_template_end_independently_controls_capacity_and_proposal_eligibility(self):
        self._anchor_and_rounder()
        physician = self._faculty(
            "Template ends during solve",
            template_end=date(2025, 2, 5),
        )

        solver_input, proposal = self._preflight_and_fill()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        at_end = self._candidate(solver_input, physician, date(2025, 2, 5), "AM")
        after_end = self._candidate(solver_input, physician, date(2025, 2, 6), "AM")
        available_slots = [(date(2025, 2, 5), "AM"), (date(2025, 2, 5), "PM")]

        self.assertTrue(at_end.is_faculty)
        self.assertEqual(at_end.base_availability, "open")
        self.assertTrue(after_end.is_faculty)
        self.assertEqual(after_end.base_availability, "off")
        self.assertEqual(after_end.availability, "not_available")
        self.assertEqual(capacities[physician.pk].precepting_capacity, 6)
        self.assertEqual(
            sum(item.precepting_capacity for item in solver_input.faculty_capacities), 26
        )
        proposed_slots = self._proposed_slots(proposal, physician)
        self.assertTrue(proposed_slots)
        self.assertTrue(set(proposed_slots).issubset(available_slots))

    def test_start_inside_balance_window_before_solve_prorates_capacity_and_vacation_only_blocks_eligibility(
        self,
    ):
        self._anchor_and_rounder()
        physician = self._faculty("Starts before solve", role_start=date(2025, 2, 4))
        self._assign(physician, date(2025, 2, 4), "AM", 1)
        create_vacation(
            physician=physician,
            start_date=date(2025, 2, 5),
            end_date=date(2025, 2, 5),
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        create_vacation(
            physician=physician,
            start_date=date(2025, 2, 6),
            end_date=date(2025, 2, 6),
            scheduler=self.scheduler,
        )

        solver_input, proposal = self._preflight_and_fill()

        self._assert_boundary_result(
            solver_input,
            proposal,
            physician,
            capacity=8,
            denominator=28,
            existing=1,
            available_slots=[(date(2025, 2, 5), "PM")],
            proposed=1,
            final=2,
            target=12 / 7,
            fairness_deviation=120,
        )

    def test_end_inside_balance_window_before_solve_counts_history_but_prevents_proposals(
        self,
    ):
        anchor, _ = self._anchor_and_rounder()
        physician = self._faculty("Ends before solve", role_end=date(2025, 2, 4))
        self._assign(physician, date(2025, 2, 4), "AM", 1)
        self._create_academic_event(anchor)

        solver_input, proposal = self._preflight_and_fill()

        self._assert_boundary_result(
            solver_input,
            proposal,
            physician,
            capacity=4,
            denominator=24,
            existing=1,
            available_slots=[],
            proposed=0,
            final=1,
            target=2 / 3,
            fairness_deviation=80,
        )

    def test_start_during_solve_prorates_capacity_and_interview_only_blocks_that_session(self):
        self._anchor_and_rounder()
        physician = self._faculty("Starts during solve", role_start=date(2025, 2, 6))
        interview = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 6), scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=physician, scheduler=self.scheduler)

        solver_input, proposal = self._preflight_and_fill()

        self._assert_boundary_result(
            solver_input,
            proposal,
            physician,
            capacity=4,
            denominator=24,
            existing=0,
            available_slots=[(date(2025, 2, 6), "PM")],
            proposed=1,
            final=1,
            target=5 / 6,
            fairness_deviation=100,
        )

    def test_end_during_solve_prorates_capacity_and_sibling_occupancy_is_not_a_second_proposal(
        self,
    ):
        self._anchor_and_rounder()
        physician = self._faculty("Ends during solve", role_end=date(2025, 2, 5))
        self._assign(physician, date(2025, 2, 5), "AM", 1)

        solver_input, proposal = self._preflight_and_fill()

        self._assert_boundary_result(
            solver_input,
            proposal,
            physician,
            capacity=6,
            denominator=26,
            existing=1,
            available_slots=[(date(2025, 2, 5), "PM")],
            proposed=1,
            final=2,
            target=18 / 13,
            fairness_deviation=120,
        )

    def test_all_four_effective_boundaries_keep_their_capacity_eligibility_and_closest_workloads_together(
        self,
    ):
        anchor, rounder = self._anchor_and_rounder()
        starts_before = self._faculty(
            "Starts before solve",
            role_start=date(2025, 2, 4),
            template_start=date(2025, 2, 4),
        )
        ends_before = self._faculty(
            "Ends before solve",
            role_end=date(2025, 2, 4),
            template_end=date(2025, 2, 4),
        )
        starts_during = self._faculty(
            "Starts during solve",
            role_start=date(2025, 2, 6),
            template_start=date(2025, 2, 6),
        )
        ends_during = self._faculty(
            "Ends during solve",
            role_end=date(2025, 2, 5),
            template_end=date(2025, 2, 5),
        )
        self._assign(starts_before, date(2025, 2, 4), "AM", 1)
        self._assign(ends_before, date(2025, 2, 4), "AM", 2)
        self._assign(ends_during, date(2025, 2, 5), "AM", 1)
        create_vacation(
            physician=starts_before,
            start_date=date(2025, 2, 5),
            end_date=date(2025, 2, 5),
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        create_vacation(
            physician=starts_before,
            start_date=date(2025, 2, 6),
            end_date=date(2025, 2, 6),
            scheduler=self.scheduler,
        )
        self._create_academic_event(anchor)
        interview = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 6), scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=starts_during, scheduler=self.scheduler)

        solver_input, proposal = self._preflight_and_fill()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}

        self.assertEqual(capacities[anchor.pk].precepting_capacity, 10)
        self.assertEqual(capacities[rounder.pk].precepting_capacity, 10)
        expectations = (
            (starts_before, 8, 1, [(date(2025, 2, 5), "PM")], 1, 2, 32 / 21),
            (ends_before, 4, 1, [], 0, 1, 16 / 21),
            (starts_during, 4, 0, [(date(2025, 2, 6), "PM")], 1, 1, 16 / 21),
            (ends_during, 6, 1, [(date(2025, 2, 5), "PM")], 1, 2, 8 / 7),
        )
        for physician, capacity, existing, eligible, proposed, final, target in expectations:
            with self.subTest(physician=physician.display_name):
                self._assert_boundary_result(
                    solver_input,
                    proposal,
                    physician,
                    capacity=capacity,
                    denominator=42,
                    existing=existing,
                    available_slots=eligible,
                    proposed=proposed,
                    final=final,
                    target=target,
                    fairness_deviation=160,
                )


class PreceptingFillProposalTests(PreceptingSolverInputTests):
    def proposal(self):
        result = self.preflight(SolveDateRange(self.week, date(2025, 2, 7)))
        return produce_fill_proposal(result.solver_input)

    def test_preserves_existing_assignments_and_excludes_interview_occupancy(self):
        assigned = self.faculty("Assigned")
        interviewer = self.faculty("Interviewer")
        replacement = self.faculty("Replacement")
        self.ready_week()
        monday_am = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        existing = assign_preceptor(
            session=monday_am,
            physician=assigned,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        interview = create_interview_shift(
            academic_year=2024, date=self.week, scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=interviewer, scheduler=self.scheduler)

        proposal = self.proposal()

        monday_am_assignments = [
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week and assignment.period_of_day == "AM"
        ]
        self.assertEqual(existing.physician_id, assigned.pk)
        self.assertFalse(
            any(assignment.position == "primary-1" for assignment in monday_am_assignments)
        )
        self.assertNotIn(
            interviewer.pk, [assignment.physician_id for assignment in monday_am_assignments]
        )
        self.assertIn(
            replacement.pk, [assignment.physician_id for assignment in monday_am_assignments]
        )

    def test_community_proposals_sort_the_commitment_into_the_first_primary_slot(self):
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        self.ready_week()

        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        for seed in ("community-first", "community-second"):
            proposal = produce_fill_proposal(solver_input, solve_seed=seed)
            community_assignments = [
                assignment
                for assignment in proposal.assignments
                if assignment.physician_id == community.pk
            ]
            self.assertTrue(community_assignments)
            self.assertTrue(
                all(assignment.position == "primary-1" for assignment in community_assignments)
            )
            self.assertFalse(
                any(
                    requirement.position == "secondary"
                    for requirement in proposal.unresolved_requirements
                )
            )

    def test_multiple_community_commitments_put_alphabetically_first_community_first(self):
        communities = []
        for name in ("Community B", "Community A"):
            community = create_physician(display_name=name, scheduler=self.scheduler)
            add_role(
                physician=community,
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
            create_community_preceptor_rule(
                physician=community,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                scheduler=self.scheduler,
            )
            communities.append(community)
        self.ready_week()

        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        alphabetically_first = next(
            community for community in communities if community.display_name == "Community A"
        )
        for seed in ("community-a-first", "community-b-first", "faculty-first"):
            proposal = produce_fill_proposal(solver_input, solve_seed=seed)
            first = next(
                assignment
                for assignment in proposal.assignments
                if assignment.date == self.week
                and assignment.period_of_day == "AM"
                and assignment.position == "primary-1"
            )
            self.assertEqual(first.physician_id, alphabetically_first.pk)

    def test_fill_leaves_unrequested_secondary_open_after_covering_required_work(self):
        self.faculty("Faculty A")
        self.faculty("Faculty B")
        self.ready_week()

        proposal = self.proposal()

        self.assertEqual(len(proposal.assignments), 20)
        self.assertTrue(
            all(assignment.position.startswith("primary-") for assignment in proposal.assignments)
        )
        self.assertEqual(len(proposal.late_doc_assignments), 1)
        self.assertFalse(proposal.unresolved_requirements)

    def test_fill_uses_open_secondary_when_existing_primaries_lack_faculty_coverage(self):
        existing_faculty = self.ready_week()
        secondary_faculty = self.faculty("Available Secondary")
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        monday_am = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=monday_am,
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=monday_am,
            physician=existing_faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )

        proposal = self.proposal()

        self.assertIn(
            ("secondary", secondary_faculty.pk),
            [
                (assignment.position, assignment.physician_id)
                for assignment in proposal.assignments
                if assignment.date == self.week and assignment.period_of_day == "AM"
            ],
        )
        self.assertNotIn(
            "insufficient_faculty_equivalent_coverage",
            [
                requirement.code
                for requirement in proposal.unresolved_requirements
                if requirement.date == self.week and requirement.period_of_day == "AM"
            ],
        )

    def test_excluded_fixed_faculty_keeps_coverage_metadata_without_opening_secondary(self):
        excluded = self.faculty("Excluded")
        other_faculty = self.faculty("Other Faculty")
        self.ready_week()
        monday_am = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=monday_am,
            physician=excluded,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=monday_am,
            physician=other_faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )
        solver_input = without_excluded_physicians(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            [excluded.pk],
        )

        proposal = produce_fill_proposal(solver_input)

        monday_assignments = [
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week and assignment.period_of_day == "AM"
        ]
        projected = next(
            session
            for session in solver_input.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        self.assertEqual(solver_input.excluded_physician_ids, (excluded.pk,))
        self.assertIn(excluded.pk, {candidate.physician_id for candidate in projected.candidates})
        self.assertNotIn(
            excluded.pk, [assignment.physician_id for assignment in proposal.assignments]
        )
        self.assertFalse(
            any(assignment.position == "secondary" for assignment in monday_assignments)
        )
        self.assertNotIn(
            "insufficient_faculty_equivalent_coverage",
            [
                requirement.code
                for requirement in proposal.unresolved_requirements
                if requirement.date == self.week and requirement.period_of_day == "AM"
            ],
        )

    def test_excluded_ordinary_alternative_is_not_selected_or_displayed_as_available(self):
        self.ready_week()
        available = self.faculty("Available")
        excluded = self.faculty("Excluded")
        solver_input = without_excluded_physicians(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            [excluded.pk],
        )

        proposal = produce_fill_proposal(solver_input)
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.physician_id == available.pk
        )

        self.assertNotIn(
            excluded.pk, [assignment.physician_id for assignment in proposal.assignments]
        )
        self.assertNotIn(
            excluded.pk, [assignment.physician_id for assignment in proposal.late_doc_assignments]
        )
        exclusion = next(
            item
            for item in selected.explanation.hard_exclusions
            if item.physician_id == excluded.pk
        )
        self.assertEqual(exclusion.physician_name, "Excluded")
        self.assertEqual(
            [(reason.code, reason.summary, reason.detail) for reason in exclusion.reasons],
            [
                (
                    "solve_local_exclusion",
                    "Excluded from this Fill proposal",
                    "This physician was excluded for this proposal only.",
                )
            ],
        )
        self.assertNotIn(
            excluded.pk,
            [
                alternative.physician_id
                for alternatives in (
                    selected.explanation.equally_good_alternatives,
                    selected.explanation.other_normally_available_alternatives,
                    selected.explanation.backup_only_alternatives,
                )
                for alternative in alternatives
            ],
        )

    def test_assessments_keep_multiple_interview_and_position_exclusions_structured(self):
        self.ready_week()
        available = self.faculty("Available")
        director = self.faculty("Program Director")
        director_role = EffectiveRole.objects.get(physician=director)
        director_role.faculty_leadership = "program_director"
        director_role.save(update_fields=("faculty_leadership",))
        interviewer = self.faculty("Interviewer")
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=self.week.weekday(),
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        vacation = create_vacation(
            physician=director,
            start_date=self.week,
            end_date=self.week,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        interview = create_interview_shift(
            academic_year=2024, date=self.week, scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=interviewer, scheduler=self.scheduler)

        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        proposal = produce_fill_proposal(solver_input)
        monday_am = next(
            session
            for session in solver_input.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        primary_one = monday_am.positions[0]
        secondary = monday_am.positions[2]
        director_candidate = next(
            candidate
            for candidate in primary_one.candidates
            if candidate.physician_id == director.pk
        )
        interviewer_candidate = next(
            candidate
            for candidate in primary_one.candidates
            if candidate.physician_id == interviewer.pk
        )
        community_secondary = next(
            candidate
            for candidate in secondary.candidates
            if candidate.physician_id == community.pk
        )
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week
            and assignment.period_of_day == "AM"
            and assignment.physician_id == available.pk
        )
        exclusions = {
            exclusion.physician_id: exclusion for exclusion in selected.explanation.hard_exclusions
        }

        self.assertFalse(director_candidate.automatic_eligible)
        self.assertFalse(interviewer_candidate.automatic_eligible)
        self.assertEqual(
            [reason.code for reason in director_candidate.reasons if reason.severity == "hard"],
            [
                "not_available_assignment",
                "availability_source",
                "availability_source",
                "program_director_interview_commitment",
            ],
        )
        self.assertEqual(
            [reason.summary for reason in exclusions[director.pk].reasons][-2:],
            [f"Vacation {vacation.pk}", "Program Director Interview"],
        )
        self.assertEqual(
            [reason.summary for reason in exclusions[interviewer.pk].reasons],
            ["Interview conflict"],
        )
        self.assertEqual(
            exclusions[interviewer.pk].reasons[0].detail,
            "Assigned to this AM Interview shift.",
        )
        self.assertEqual(
            exclusions[director.pk].reasons[-1].source_identity,
            ("interview_shift", interview.pk),
        )
        self.assertEqual(
            [item.physician_name for item in selected.explanation.hard_exclusions],
            ["Interviewer", "Program Director", "Rounder"],
        )
        self.assertEqual(
            [reason.code for reason in community_secondary.reasons if reason.severity == "hard"],
            ["role_responsibility_mismatch"],
        )
        self.assertFalse(community_secondary.automatic_eligible)
        self.assertNotIn(
            director.pk,
            [
                assignment.physician_id
                for assignment in proposal.assignments
                if assignment.date == self.week and assignment.period_of_day == "AM"
            ],
        )
        director_role.faculty_leadership = ""
        director_role.save(update_fields=("faculty_leadership",))
        changed_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input

        self.assertNotEqual(solver_input.fingerprint, changed_input.fingerprint)
        self.assertNotIn(
            interviewer.pk,
            [
                assignment.physician_id
                for assignment in proposal.assignments
                if assignment.date == self.week and assignment.period_of_day == "AM"
            ],
        )

    def test_late_doc_requires_actual_faculty_and_may_share_pm_precepting(self):
        eligible = self.faculty("Eligible")
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        self.ready_week()

        proposal = self.proposal()

        late_doc = next(
            assignment
            for assignment in proposal.late_doc_assignments
            if assignment.date == self.week
        )
        self.assertEqual(late_doc.physician_id, eligible.pk)
        self.assertNotEqual(late_doc.physician_id, community.pk)
        self.assertTrue(
            any(
                assignment.date == self.week
                and assignment.period_of_day == "PM"
                and assignment.physician_id == eligible.pk
                for assignment in proposal.assignments
            )
        )

    def test_backup_only_faculty_is_a_feasible_precepting_and_late_doc_fallback(self):
        self.ready_week()
        backup = self.faculty("Backup", availability="backup_only")

        first = self.proposal()
        second = self.proposal()

        self.assertEqual(first, second)
        self.assertTrue(first.assignments)
        self.assertTrue(
            all(assignment.physician_id == backup.pk for assignment in first.assignments)
        )
        self.assertEqual(
            [
                (assignment.date, assignment.physician_id)
                for assignment in first.late_doc_assignments
            ],
            [(self.week, backup.pk)],
        )
        self.assertIn(
            "missing_primary_coverage", [item.code for item in first.unresolved_requirements]
        )
        self.assertTrue(all(item.hard_causes for item in first.unresolved_requirements))

    def test_available_faculty_are_preferred_to_alphabetically_earlier_backup_candidates(self):
        self.ready_week()
        backup = self.faculty("A Backup", availability="backup_only")
        existing_faculty = self.faculty("Y Existing")
        available = self.faculty("Z Available")
        monday_am = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=monday_am,
            physician=existing_faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        proposal = self.proposal()

        self.assertIn(
            available.pk,
            [
                assignment.physician_id
                for assignment in proposal.assignments
                if assignment.date == self.week and assignment.period_of_day == "AM"
            ],
        )
        self.assertNotIn(
            backup.pk, [assignment.physician_id for assignment in proposal.assignments]
        )
        self.assertNotEqual(proposal.late_doc_assignments[0].physician_id, backup.pk)

    def test_workload_summary_uses_base_capacity_and_existing_window_assignments(self):
        rounder = self.faculty("Rounder", availability="patient_care")
        broad = self.faculty("Broad capacity")
        narrow_slots = {
            slot: ("open" if slot == "monday_am" else "off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        narrow = self.faculty("Narrow capacity", slots=narrow_slots)
        previous_week = date(2025, 1, 27)
        record_normal_rounder(week=previous_week, physician=rounder, scheduler=self.scheduler)
        record_normal_rounder(week=self.week, physician=rounder, scheduler=self.scheduler)
        open_weekly_draft(previous_week)
        open_weekly_draft(self.week)
        previous_session = next(
            session
            for session in open_weekly_draft(previous_week)
            if session.date == previous_week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=previous_session,
            physician=broad,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        result = self.preflight(
            SolveDateRange(self.week, date(2025, 2, 7)),
            balance_window=SolveDateRange(previous_week, date(2025, 2, 7)),
        )
        proposal = produce_fill_proposal(result.solver_input)
        workloads = {item.physician_id: item for item in proposal.compromises.faculty_workloads}

        self.assertEqual(workloads[broad.pk].current_total, 1)
        for physician_id, workload in workloads.items():
            with self.subTest(physician_id=physician_id):
                proposed_assignments = sum(
                    assignment.physician_id == physician_id for assignment in proposal.assignments
                )
                self.assertEqual(
                    workload.proposed_total,
                    workload.current_total + proposed_assignments,
                )
        self.assertGreater(workloads[broad.pk].target_share, workloads[narrow.pk].target_share)
        self.assertGreater(workloads[broad.pk].target_total, workloads[narrow.pk].target_total)

    def test_explanation_reports_forced_faculty_workload_imbalance(self):
        previous_week = date(2025, 1, 27)
        rounder = self.faculty("Rounder", availability="patient_care")
        overloaded = self.faculty("Overloaded")
        self.faculty("Underloaded")
        for week in (previous_week, self.week):
            record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
            open_weekly_draft(week)
        for session in open_weekly_draft(previous_week):
            assign_preceptor(
                session=session,
                physician=overloaded,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
                scheduler=self.scheduler,
            )

        proposal = produce_fill_proposal(
            self.preflight(
                SolveDateRange(self.week, date(2025, 2, 7)),
                balance_window=SolveDateRange(previous_week, date(2025, 2, 7)),
            ).solver_input
        )
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.physician_id == overloaded.pk
        )

        self.assertIn(
            "Adds a Faculty Precepting shift for Overloaded despite 20 shifts against a 15.0 "
            "proportional target.",
            selected.explanation.policy_disadvantages,
        )
        self.assertNotIn(
            "Contributes to Faculty workload balance.", selected.explanation.policy_advantages
        )

    def test_explanations_and_counterfactuals_are_retained_and_read_only(self):
        self.ready_week()
        first = self.faculty("First")
        second = self.faculty("Second")
        third = self.faculty("Third")
        backup = self.faculty("Backup", availability="backup_only")

        proposal = self.proposal()
        selected = next(
            assignment for assignment in proposal.assignments if assignment.physician_id == first.pk
        )
        occupied = {
            assignment.physician_id
            for assignment in proposal.assignments
            if assignment.date == selected.date
            and assignment.period_of_day == selected.period_of_day
        }
        alternative = next(
            physician for physician in (first, second, third) if physician.pk not in occupied
        )
        comparison = compare_fill_proposal(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            proposal,
            selected,
            alternative_physician_id=alternative.pk,
        )

        self.assertIn(
            backup.pk,
            [item.physician_id for item in selected.explanation.backup_only_alternatives],
        )
        self.assertTrue(comparison.feasible)
        self.assertEqual(
            alternative.pk,
            next(
                assignment.physician_id
                for assignment in comparison.proposal.assignments
                if assignment.date == selected.date
                and assignment.period_of_day == selected.period_of_day
                and assignment.position == selected.position
            ),
        )
        self.assertEqual(proposal, self.proposal())

    def test_counterfactual_rejects_a_proposal_from_a_different_solver_input(self):
        self.ready_week()
        self.faculty("Faculty")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        proposal = produce_fill_proposal(solver_input)

        with self.assertRaisesMessage(
            ValidationError,
            "The FillProposal was produced from a different PreceptingSolverInput.",
        ):
            compare_fill_proposal(
                replace(solver_input, fingerprint="different-input"),
                proposal,
                proposal.assignments[0],
                exclude=True,
            )

    def test_counterfactual_rejects_an_eligible_assignment_not_selected_by_the_proposal(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        proposal = produce_fill_proposal(solver_input)
        selected = proposal.assignments[0]
        alternative = selected.explanation.equally_good_alternatives[0]
        unselected = replace(
            selected,
            physician_id=alternative.physician_id,
            physician_name=alternative.physician_name,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "The assignment is not selected by the FillProposal.",
        ):
            compare_fill_proposal(
                solver_input,
                proposal,
                unselected,
                exclude=True,
            )

    def test_counterfactual_reports_sibling_primary_that_becomes_unresolved(self):
        self.ready_week()
        first = self.faculty("First")
        second = self.faculty("Second")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        proposal = produce_fill_proposal(solver_input)
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week
            and assignment.period_of_day == "AM"
            and assignment.position == "primary-1"
        )
        other = next(
            physician for physician in (first, second) if physician.pk != selected.physician_id
        )
        sibling = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == selected.date
            and assignment.period_of_day == selected.period_of_day
            and assignment.position == "primary-2"
            and assignment.physician_id == other.pk
        )
        compared = replace(
            proposal,
            assignments=tuple(
                assignment for assignment in proposal.assignments if assignment != sibling
            ),
            unresolved_requirements=(
                *proposal.unresolved_requirements,
                FillProposalUnresolvedRequirement(
                    sibling.date,
                    sibling.period_of_day,
                    sibling.position,
                    "missing_primary_coverage",
                    "No feasible proposal fills Preceptor 2.",
                    ("candidate_scarcity",),
                ),
            ),
        )

        with patch(
            "schedules.services.precepting_solver._CpSatFillAdapter.solve", return_value=compared
        ):
            comparison = compare_fill_proposal(
                solver_input,
                proposal,
                selected,
                alternative_physician_id=other.pk,
            )

        self.assertFalse(comparison.feasible)
        self.assertIn(
            f"{sibling.date.isoformat()} {sibling.period_of_day} Preceptor 2: unresolved",
            comparison.additional_changes,
        )

    def test_explanation_names_only_alternatives_with_an_equal_policy_outcome(self):
        self.ready_week()
        first = self.faculty("First")
        second = self.faculty("Second")

        proposal = self.proposal()
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week
            and assignment.period_of_day == "AM"
            and assignment.position == "primary-1"
        )
        other = next(
            physician for physician in (first, second) if physician.pk != selected.physician_id
        )

        self.assertIn(
            other.pk,
            [item.physician_id for item in selected.explanation.equally_good_alternatives],
        )
        self.assertEqual(
            "Equal-policy candidates were ordered by this proposal's recorded seeded tie-break.",
            selected.explanation.stable_tie_break,
        )

    def test_explanation_projects_every_position_eligible_comparison_alternative(self):
        self.ready_week()
        first = self.faculty("First")
        equal = self.faculty("Equal")
        backup = self.faculty("Backup", availability="backup_only")
        unavailable = self.faculty("Unavailable", availability="off")
        community = create_physician(display_name="Expected Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )

        proposal = self.proposal()
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week
            and assignment.period_of_day == "AM"
            and assignment.physician_id in {first.pk, equal.pk}
        )

        explanation = selected.explanation
        self.assertTrue(
            {first.pk, equal.pk}.intersection(
                alternative.physician_id for alternative in explanation.equally_good_alternatives
            )
        )
        self.assertIn(
            community.pk,
            [
                alternative.physician_id
                for alternative in (
                    *explanation.equally_good_alternatives,
                    *explanation.other_normally_available_alternatives,
                )
            ],
        )
        self.assertIn(
            backup.pk,
            [alternative.physician_id for alternative in explanation.backup_only_alternatives],
        )
        self.assertNotIn(
            unavailable.pk,
            [
                alternative.physician_id
                for category in (
                    explanation.equally_good_alternatives,
                    explanation.other_normally_available_alternatives,
                    explanation.backup_only_alternatives,
                )
                for alternative in category
            ],
        )

    def test_late_doc_policy_counts_existing_work_and_reports_pm_overlap(self):
        previous_week = date(2025, 1, 27)
        rounder = self.faculty("Rounder", availability="patient_care")
        established = self.faculty("Established")
        thin_monday_slots = {
            slot: ("open" if slot == "monday_pm" else "off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        thin_monday = self.faculty("Thin Monday", slots=thin_monday_slots)
        for week in (previous_week, self.week):
            record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
            open_weekly_draft(week)
        assign_late_doc(on_date=previous_week, physician=established, scheduler=self.scheduler)
        monday_pm = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "PM"
        )
        assign_preceptor(
            session=monday_pm,
            physician=established,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        # Retain both PM positions so Fill cannot also assign Thin Monday here.
        # Otherwise both Late Doc candidates can overlap PM Precepting and the
        # selected name is an arbitrary tie, not evidence of the overlap policy.
        assign_preceptor(
            session=monday_pm,
            physician=rounder,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )

        proposal = produce_fill_proposal(
            self.preflight(
                SolveDateRange(self.week, date(2025, 2, 7)),
                balance_window=SolveDateRange(previous_week, date(2025, 2, 7)),
            ).solver_input
        )
        late_totals = {item.physician_id: item for item in proposal.compromises.late_doc_workloads}

        self.assertEqual(late_totals[established.pk].current_total, 1)
        self.assertEqual(proposal.late_doc_assignments[0].physician_id, established.pk)
        self.assertEqual(
            proposal.compromises.late_doc_pm_precepting_overlap,
            (f"{self.week.isoformat()}: Established",),
        )
        self.assertNotIn(
            thin_monday.pk,
            [
                alternative.physician_id
                for alternative in proposal.late_doc_assignments[
                    0
                ].explanation.equally_good_alternatives
            ],
        )

    def test_seeded_ties_reproduce_without_name_order_and_can_vary(self):
        self.ready_week()
        zulu = self.faculty("Zulu")
        self.faculty("Mira")
        self.faculty("Alpha")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input

        repeated = produce_fill_proposal(solver_input, solve_seed="reproducible")
        self.assertEqual(repeated, produce_fill_proposal(solver_input, solve_seed="reproducible"))
        outcomes = {
            tuple(
                (item.date, item.period_of_day, item.position, item.physician_id)
                for item in produce_fill_proposal(
                    solver_input, solve_seed=f"seed-{index}"
                ).assignments
            )
            for index in range(12)
        }

        self.assertGreater(len(outcomes), 1)
        self.assertTrue(
            any(
                zulu.pk
                in {
                    item.physician_id
                    for item in produce_fill_proposal(
                        solver_input, solve_seed=f"seed-{index}"
                    ).assignments
                }
                for index in range(12)
            )
        )

    def test_seeded_ties_ignore_display_names_with_fixed_ids(self):
        self.ready_week()
        physicians = (
            self.faculty("Alpha"),
            self.faculty("Mira"),
            self.faculty("Zulu"),
        )
        solve_range = SolveDateRange(self.week, date(2025, 2, 7))

        def choices_and_equal_alternatives(solver_input):
            proposal = produce_fill_proposal(solver_input, solve_seed="fixed-seed")
            return tuple(
                (
                    assignment.date,
                    assignment.period_of_day,
                    assignment.position,
                    assignment.physician_id,
                    tuple(
                        alternative.physician_id
                        for alternative in assignment.explanation.equally_good_alternatives
                    ),
                )
                for assignment in proposal.assignments
            )

        original = choices_and_equal_alternatives(self.preflight(solve_range).solver_input)
        self.assertTrue(any(alternatives for *_, alternatives in original))
        for physician, display_name in zip(physicians, ("Zulu", "Alpha", "Mira"), strict=True):
            Physician.objects.filter(pk=physician.pk).update(display_name=display_name)

        renamed = choices_and_equal_alternatives(self.preflight(solve_range).solver_input)

        self.assertEqual(renamed, original)

    def test_policy_priority_dominates_the_same_seeded_equal_tier_choice(self):
        self.ready_week()
        candidates = (self.faculty("First"), self.faculty("Second"), self.faculty("Third"))
        solve_range = SolveDateRange(self.week, date(2025, 2, 7))
        seed = "policy-priority-control"
        for candidate in candidates:
            set_faculty_precepting_capacity_override(
                physician=candidate,
                capacity_override=Decimal("10.0"),
                scheduler=self.scheduler,
            )

        equal_input = self.preflight(solve_range).solver_input
        equal_tier = produce_fill_proposal(equal_input, solve_seed=seed)
        chosen = next(
            assignment
            for assignment in equal_tier.assignments
            if (assignment.date, assignment.period_of_day, assignment.position)
            == (self.week, "AM", "primary-1")
        )
        self.assertIn(chosen.physician_id, {physician.pk for physician in candidates})
        candidate_ids = {physician.pk for physician in candidates}
        original_precepting_capacities = {
            capacity.physician_id: capacity.precepting_capacity
            for capacity in equal_input.faculty_capacities
            if capacity.physician_id in candidate_ids
        }
        original_capacity = next(
            capacity
            for capacity in equal_input.faculty_capacities
            if capacity.physician_id == chosen.physician_id
        )

        template = FacultyAvailabilityTemplate.objects.get(physician_id=chosen.physician_id)
        slots = dict(template.slots)
        slots["monday_am"] = "backup_only"
        correct_faculty_availability_template(
            template=template,
            effective_start=template.effective_start,
            effective_end=template.effective_end,
            slots=slots,
            scheduler=self.scheduler,
        )
        policy_input = self.preflight(solve_range).solver_input
        selected_candidate = next(
            candidate
            for session in policy_input.sessions
            if (session.date, session.period_of_day) == (self.week, "AM")
            for candidate in session.candidates
            if candidate.physician_id == chosen.physician_id
        )
        normally_available_ids = {
            candidate.physician_id
            for session in policy_input.sessions
            if (session.date, session.period_of_day) == (self.week, "AM")
            for candidate in session.candidates
            if candidate.physician_id in candidate_ids
            and candidate.physician_id != chosen.physician_id
            and candidate.availability == "available"
        }
        capacities = {item.physician_id: item for item in policy_input.faculty_capacities}
        self.assertEqual(selected_candidate.availability, "backup_only")
        self.assertEqual(normally_available_ids, candidate_ids - {chosen.physician_id})
        self.assertEqual(
            {
                physician_id: capacities[physician_id].precepting_capacity
                for physician_id in candidate_ids
            },
            original_precepting_capacities,
        )
        self.assertEqual(
            capacities[chosen.physician_id].late_doc_capacity,
            original_capacity.late_doc_capacity,
        )
        policy_priority = produce_fill_proposal(policy_input, solve_seed=seed)
        monday_am_assignments = [
            assignment
            for assignment in policy_priority.assignments
            if (assignment.date, assignment.period_of_day, assignment.position)
            in {
                (self.week, "AM", "primary-1"),
                (self.week, "AM", "primary-2"),
            }
        ]

        self.assertEqual(
            {assignment.position for assignment in monday_am_assignments},
            {"primary-1", "primary-2"},
        )
        self.assertEqual(
            {assignment.physician_id for assignment in monday_am_assignments},
            candidate_ids - {chosen.physician_id},
        )

    def test_policy_priority_prefers_clean_faculty_over_soft_academic_event(self):
        self.ready_week()
        first_clean = self.faculty("First clean Faculty")
        second_clean = self.faculty("Second clean Faculty")
        soft = self.faculty("Soft Academic Event Faculty")
        create_academic_event_occurrence(
            definition=create_academic_event_definition(
                base_name="Soft Monday event", start_academic_year=2024, scheduler=self.scheduler
            ),
            academic_year=2024,
            start_date=self.week,
            end_date=self.week,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            participants=(soft,),
            scheduler=self.scheduler,
        )

        proposal = produce_fill_proposal(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        )

        monday_am = {
            assignment.physician_id
            for assignment in proposal.assignments
            if assignment.date == self.week and assignment.period_of_day == "AM"
        }
        self.assertEqual(monday_am, {first_clean.pk, second_clean.pk})

    def test_explanation_lists_soft_academic_event_as_other_available(self):
        self.ready_week()
        clean = self.faculty("Clean Faculty")
        self.faculty("Second clean Faculty")
        soft = self.faculty("Soft Academic Event Faculty")
        create_academic_event_occurrence(
            definition=create_academic_event_definition(
                base_name="Soft Monday event", start_academic_year=2024, scheduler=self.scheduler
            ),
            academic_year=2024,
            start_date=self.week,
            end_date=self.week,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            participants=(soft,),
            scheduler=self.scheduler,
        )

        proposal = self.proposal()
        selected = next(
            assignment
            for assignment in proposal.assignments
            if assignment.date == self.week
            and assignment.period_of_day == "AM"
            and assignment.physician_id == clean.pk
        )

        self.assertNotIn(
            soft.pk,
            [item.physician_id for item in selected.explanation.equally_good_alternatives],
        )
        self.assertIn(
            soft.pk,
            [
                item.physician_id
                for item in selected.explanation.other_normally_available_alternatives
            ],
        )


class PreceptingRepairProposalTests(TestCase):
    on_date = date(2025, 2, 3)

    def candidate(self, physician_id, name, availability, assignment_id=None):
        return precepting_solver.SolverCandidate(
            physician_id=physician_id,
            physician_name=name,
            effective_roles=(EffectiveRole.Role.FACULTY,),
            availability=availability,
            base_availability=availability,
            ordinarily_eligible_to_precept=True,
            faculty_equivalent=True,
            expected_community_commitment=False,
            preceptor_assignment_id=assignment_id,
            interview_shift_id=None,
            source_ids=(),
        )

    def assignment(self, assignment_id, physician_id, name, *, locked=False):
        return precepting_solver.SolverAssignment(
            id=assignment_id,
            physician_id=physician_id,
            physician_name=name,
            responsibility="primary",
            primary_position=1,
            is_locked=locked,
            is_faculty=True,
        )

    def solver_input(self, *, first_assignment, candidates):
        locked = self.assignment(20, 20, "Locked Faculty", locked=True)
        return precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(self.on_date, self.on_date),
            balance_window=SolveDateRange(self.on_date, self.on_date),
            sessions=(
                precepting_solver.SolverSession(
                    date=self.on_date,
                    period_of_day="AM",
                    session_id=1,
                    positions=(
                        precepting_solver.SolverPosition("primary-1", True, first_assignment),
                        precepting_solver.SolverPosition("primary-2", True, locked),
                        precepting_solver.SolverPosition("secondary", False, None),
                    ),
                    candidates=(*candidates, self.candidate(20, "Locked Faculty", "available", 20)),
                ),
            ),
            late_doc_positions=(),
            community_commitments=(),
            faculty_capacities=(),
            balance_assignments=(first_assignment, locked),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="repair-input",
        )

    def test_repair_replaces_an_unavailable_unlocked_assignment(self):
        current = self.assignment(10, 10, "Unavailable")
        proposal = produce_repair_proposal(
            self.solver_input(
                first_assignment=current,
                candidates=(
                    self.candidate(10, "Unavailable", "not_available", 10),
                    self.candidate(11, "Replacement", "available"),
                ),
            )
        )

        self.assertEqual(
            [(item.position, item.physician_name) for item in proposal.assignments],
            [("primary-1", "Replacement")],
        )
        self.assertEqual(
            proposal.assignments[0].explanation.repair_reason,
            "Needed to resolve a hard finding while retaining other assignments.",
        )

    def test_repair_does_not_retain_an_excluded_unlocked_assignment(self):
        current = self.assignment(10, 10, "Excluded")
        solver_input = without_excluded_physicians(
            self.solver_input(
                first_assignment=current,
                candidates=(
                    self.candidate(10, "Excluded", "available", 10),
                    self.candidate(11, "Replacement", "available"),
                ),
            ),
            [10],
        )

        proposal = produce_repair_proposal(solver_input)

        self.assertEqual(
            [(item.position, item.physician_id) for item in proposal.assignments],
            [("primary-1", 11)],
        )

    def test_repair_keeps_a_locked_blocker_unchanged(self):
        locked = self.assignment(10, 10, "Unavailable", locked=True)
        proposal = produce_repair_proposal(
            self.solver_input(
                first_assignment=locked,
                candidates=(self.candidate(10, "Unavailable", "not_available", 10),),
            )
        )

        self.assertFalse(proposal.assignments)
        self.assertFalse(proposal.precepting_removals)

    def test_impossible_repair_retains_an_unlocked_assignment_and_reports_the_hard_cause(self):
        current = self.assignment(10, 10, "Unavailable")
        proposal = produce_repair_proposal(
            self.solver_input(
                first_assignment=current,
                candidates=(self.candidate(10, "Unavailable", "not_available", 10),),
            )
        )

        self.assertFalse(proposal.assignments)
        self.assertFalse(proposal.precepting_removals)
        self.assertEqual(
            [(item.code, item.hard_causes) for item in proposal.unresolved_requirements],
            [
                ("missing_primary_coverage", ("no_eligible_candidate",)),
            ],
        )

    def test_repair_uses_a_two_slot_swap_when_it_resolves_both_assignments(self):
        first = self.assignment(10, 10, "First")
        second = self.assignment(11, 11, "Second")
        first_locked = self.assignment(20, 20, "First Locked", locked=True)
        second_locked = self.assignment(21, 21, "Second Locked", locked=True)
        second_date = date(2025, 2, 4)
        solver_input = precepting_solver.PreceptingSolverInput(
            solve_range=SolveDateRange(self.on_date, second_date),
            balance_window=SolveDateRange(self.on_date, second_date),
            sessions=(
                precepting_solver.SolverSession(
                    self.on_date,
                    "AM",
                    1,
                    (
                        precepting_solver.SolverPosition("primary-1", True, first),
                        precepting_solver.SolverPosition("primary-2", True, first_locked),
                        precepting_solver.SolverPosition("secondary", False, None),
                    ),
                    (
                        self.candidate(10, "First", "not_available", 10),
                        self.candidate(11, "Second", "available", 11),
                        self.candidate(20, "First Locked", "available", 20),
                    ),
                ),
                precepting_solver.SolverSession(
                    second_date,
                    "AM",
                    2,
                    (
                        precepting_solver.SolverPosition("primary-1", True, second),
                        precepting_solver.SolverPosition("primary-2", True, second_locked),
                        precepting_solver.SolverPosition("secondary", False, None),
                    ),
                    (
                        self.candidate(10, "First", "available", 10),
                        self.candidate(11, "Second", "not_available", 11),
                        self.candidate(21, "Second Locked", "available", 21),
                    ),
                ),
            ),
            late_doc_positions=(),
            community_commitments=(),
            faculty_capacities=(),
            balance_assignments=(first, second, first_locked, second_locked),
            balance_late_doc_assignments=(),
            source_versions=(),
            fingerprint="repair-swap-input",
        )

        proposal = produce_repair_proposal(solver_input)

        self.assertEqual(
            [(item.date, item.physician_name) for item in proposal.assignments],
            [(self.on_date, "Second"), (second_date, "First")],
        )

    def test_repair_does_not_use_a_sibling_tier_one_assignment_outside_its_movable_set(self):
        current = self.assignment(10, 10, "Unavailable")
        proposal = produce_repair_proposal(
            self.solver_input(
                first_assignment=current,
                candidates=(
                    self.candidate(10, "Unavailable", "not_available", 10),
                    self.candidate(11, "Sibling Tier 1", "available", 99),
                ),
            )
        )

        self.assertFalse(proposal.assignments)
        self.assertFalse(proposal.precepting_removals)
        self.assertIn(
            "missing_primary_coverage", [item.code for item in proposal.unresolved_requirements]
        )

    def test_repair_uses_stable_minimal_change_choice_between_replacements(self):
        current = self.assignment(10, 10, "Unavailable")
        solver_input = self.solver_input(
            first_assignment=current,
            candidates=(
                self.candidate(10, "Unavailable", "not_available", 10),
                self.candidate(12, "Zebra", "available"),
                self.candidate(11, "Alpha", "available"),
            ),
        )

        proposal = produce_repair_proposal(solver_input, solve_seed="repair-tie")
        self.assertEqual(proposal, produce_repair_proposal(solver_input, solve_seed="repair-tie"))
        self.assertIn(proposal.assignments[0].physician_name, {"Alpha", "Zebra"})


class RetainedPreceptingSolverProposalTests(PreceptingSolverInputTests):
    def setUp(self):
        super().setUp()
        self.today = patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2025, 1, 1)
        )
        self.today.start()
        self.addCleanup(self.today.stop)

    def retained_proposal(self):
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        proposal = produce_fill_proposal(solver_input)
        return retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=proposal,
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": []},
        )

    def confirm_late_docs(self, proposal):
        for review in proposal.late_doc_reviews.all():
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
            )
            proposal.refresh_from_db()

    def test_capacity_override_change_makes_an_unapplied_proposal_stale(self):
        self.ready_week()
        physician = self.faculty("Capacity override")
        self.faculty("Other Faculty")
        proposal = self.retained_proposal()

        set_faculty_precepting_capacity_override(
            physician=physician,
            capacity_override=Decimal("4.5"),
            scheduler=self.scheduler,
        )

        with self.assertRaisesMessage(ValidationError, "The solver proposal is stale"):
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

    def proposal_with_two_hard_findings(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        unavailable_one = self.faculty("Unavailable one", availability="off")
        unavailable_two = self.faculty("Unavailable two", availability="off")
        proposal = self.retained_proposal()
        first = proposal.proposed_assignments["precepting"][0]
        second = next(
            assignment
            for assignment in proposal.proposed_assignments["precepting"]
            if (assignment["date"], assignment["period_of_day"])
            != (first["date"], first["period_of_day"])
        )
        for assignment, physician in ((first, unavailable_one), (second, unavailable_two)):
            edit_proposed_assignment(
                proposal=proposal,
                assignment_type="precepting",
                on_date=date.fromisoformat(assignment["date"]),
                period_of_day=assignment["period_of_day"],
                position=assignment["position"],
                physician=physician,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
            )
            proposal.refresh_from_db()
        self.confirm_late_docs(proposal)
        with self.assertRaises(HardFindingConfirmationRequired) as confirmation:
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)
        return proposal, confirmation.exception

    def test_hard_finding_checkpoint_retains_its_reason_without_acknowledgements(self):
        proposal, confirmation = self.proposal_with_two_hard_findings()

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Awaiting external coverage.",
        )

        proposal.refresh_from_db()
        self.assertEqual(proposal.applied_reason, "Awaiting external coverage.")
        self.assertEqual(FindingAcknowledgement.objects.count(), 0)
        self.assertEqual(len(confirmation.finding_keys), 2)
        self.assertEqual(
            len(draft_findings_for_week(self.week).findings),
            len(confirmation.findings),
        )

    def test_hard_finding_checkpoint_acknowledges_only_selected_findings(self):
        proposal, confirmation = self.proposal_with_two_hard_findings()

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Awaiting external coverage.",
            selected_hard_finding_keys=(confirmation.finding_keys[0],),
        )

        self.assertEqual(FindingAcknowledgement.objects.count(), 1)
        acknowledgement = FindingAcknowledgement.objects.get()
        self.assertEqual(acknowledgement.reason, "Awaiting external coverage.")

    def test_hard_finding_checkpoint_requires_a_reason_and_current_selected_findings(self):
        proposal, confirmation = self.proposal_with_two_hard_findings()

        with self.assertRaisesMessage(ValidationError, "short reason is required"):
            apply_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                confirm_hard_findings=True,
                expected_revision=proposal.revision,
            )
        with self.assertRaisesMessage(ValidationError, "selected hard findings changed"):
            apply_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                confirm_hard_findings=True,
                expected_revision=proposal.revision,
                hard_finding_reason="Awaiting external coverage.",
                selected_hard_finding_keys=(confirmation.finding_keys[0], "stale-finding"),
            )

        proposal.refresh_from_db()
        self.assertIsNone(proposal.applied_at)
        self.assertEqual(FindingAcknowledgement.objects.count(), 0)

    def test_hard_finding_checkpoint_acknowledges_every_explicitly_selected_finding(self):
        proposal, confirmation = self.proposal_with_two_hard_findings()

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Awaiting external coverage.",
            selected_hard_finding_keys=confirmation.finding_keys,
        )

        self.assertEqual(FindingAcknowledgement.objects.count(), len(confirmation.finding_keys))
        self.assertEqual(
            {acknowledgement.reason for acknowledgement in FindingAcknowledgement.objects.all()},
            {"Awaiting external coverage."},
        )

    def test_failed_selected_acknowledgement_rolls_back_the_checkpoint(self):
        proposal, confirmation = self.proposal_with_two_hard_findings()

        with (
            patch(
                "schedules.services.finding_acknowledgements.acknowledge_finding",
                side_effect=ValidationError("acknowledgement failed"),
            ),
            self.assertRaisesMessage(ValidationError, "acknowledgement failed"),
        ):
            apply_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                confirm_hard_findings=True,
                expected_revision=proposal.revision,
                hard_finding_reason="Awaiting external coverage.",
                selected_hard_finding_keys=(confirmation.finding_keys[0],),
            )

        proposal.refresh_from_db()
        self.assertIsNone(proposal.applied_at)
        self.assertEqual(FindingAcknowledgement.objects.count(), 0)
        self.assertEqual(PreceptorAssignment.objects.count(), 0)

    def test_retained_seed_reproduces_the_proposal_order(self):
        self.ready_week()
        self.faculty("Zulu")
        self.faculty("Alpha")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        generated = produce_fill_proposal(solver_input, solve_seed="retained-order")
        retained = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=generated,
            scheduler=self.scheduler,
            solve_settings={"excluded_physician_ids": []},
        )

        self.assertEqual(retained.solve_settings["solve_seed"], "retained-order")
        self.assertEqual(
            generated,
            produce_fill_proposal(solver_input, solve_seed=retained.solve_settings["solve_seed"]),
        )

    def test_repair_leaves_assignments_outside_the_solve_range_byte_for_byte_unchanged(self):
        self.ready_week()
        self.ready_week(self.week.replace(day=10))
        outside_physician = self.faculty("Outside range")
        outside_session = next(
            session
            for session in open_weekly_draft(self.week.replace(day=10))
            if session.date == self.week.replace(day=10) and session.period_of_day == "AM"
        )
        outside_assignment = assign_preceptor(
            session=outside_session,
            physician=outside_physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        outside_assignment_before = (
            PreceptorAssignment.objects.filter(pk=outside_assignment.pk).values().get()
        )
        outside_history_before = list(
            PreceptorAssignmentChange.objects.filter(session=outside_session)
            .order_by("pk")
            .values()
        )
        solver_input = self.preflight(
            SolveDateRange(self.week, self.week.replace(day=7))
        ).solver_input
        repair = produce_repair_proposal(solver_input)
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=repair,
            scheduler=self.scheduler,
            mode="repair",
        )
        in_range_session = PreceptorSession.objects.get(date=self.week, period_of_day="AM")
        in_range_assignment = next(
            item
            for item in repair.assignments
            if (item.date, item.period_of_day, item.position) == (self.week, "AM", "primary-1")
        )

        self.assertEqual(
            PreceptorAssignment.objects.filter(pk=outside_assignment.pk).values().get(),
            outside_assignment_before,
        )
        self.assertEqual(
            list(
                PreceptorAssignmentChange.objects.filter(session=outside_session)
                .order_by("pk")
                .values()
            ),
            outside_history_before,
        )

        self.confirm_late_docs(proposal)
        apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.applied_at)
        self.assertEqual(proposal.applied_by, self.scheduler)
        applied_in_range_assignment = PreceptorAssignment.objects.get(
            session=in_range_session,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        self.assertEqual(applied_in_range_assignment.physician_id, in_range_assignment.physician_id)
        in_range_history = PreceptorAssignmentChange.objects.get(
            session=in_range_session,
            assignment_id=applied_in_range_assignment.pk,
            action=PreceptorAssignmentChange.Action.ASSIGNED,
        )
        self.assertEqual(in_range_history.changed_by, self.scheduler)
        self.assertEqual(
            in_range_history.recorded_value["physician_id"],
            in_range_assignment.physician_id,
        )

        self.assertEqual(
            PreceptorAssignment.objects.filter(pk=outside_assignment.pk).values().get(),
            outside_assignment_before,
        )
        self.assertEqual(
            list(
                PreceptorAssignmentChange.objects.filter(session=outside_session)
                .order_by("pk")
                .values()
            ),
            outside_history_before,
        )

    def test_repair_removes_an_unavailable_optional_secondary_without_replacement(self):
        self.ready_week()
        first = self.faculty("First")
        second = self.faculty("Second")
        unavailable = self.faculty("Unavailable")
        session = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        first_assignment = assign_preceptor(
            session=session,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        second_assignment = assign_preceptor(
            session=session,
            physician=second,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=session,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
            scheduler=self.scheduler,
        )
        set_preceptor_assignment_lock(
            assignment=first_assignment, is_locked=True, scheduler=self.scheduler
        )
        set_preceptor_assignment_lock(
            assignment=second_assignment, is_locked=True, scheduler=self.scheduler
        )
        create_monthly_physician_availability_rule(
            physician=unavailable,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input

        repair = produce_repair_proposal(solver_input)
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=repair,
            scheduler=self.scheduler,
            mode="repair",
        )

        self.assertEqual(
            [(item.period_of_day, item.position) for item in repair.precepting_removals],
            [("AM", "secondary")],
        )
        self.assertFalse(repair.unresolved_requirements)
        self.assertEqual(
            [
                (item["period_of_day"], item["position"], item.get("physician_id"))
                for item in proposal.proposed_assignments["precepting"]
                if item["date"] == self.week.isoformat()
                and item["period_of_day"] == "AM"
                and item["position"] == "secondary"
            ],
            [("AM", "secondary", None)],
        )
        self.assertIn(
            "not_available_assignment", {item["code"] for item in proposal.before_findings}
        )
        self.assertNotIn(
            "not_available_assignment", {item["code"] for item in proposal.after_findings}
        )

    def test_late_doc_removal_requires_review_before_apply_and_can_be_confirmed(self):
        self.ready_week()
        baseline = self.faculty("Baseline")
        assign_late_doc(on_date=self.week, physician=baseline, scheduler=self.scheduler)
        solver_input = self.preflight(
            SolveDateRange(self.week, self.week.replace(day=7))
        ).solver_input
        removal = precepting_solver.FillProposalRemoval(
            self.week,
            None,
            None,
            "late-doc",
            precepting_solver._CpSatFillAdapter._repair_explanation(),
        )
        fill_proposal = precepting_solver.FillProposal(
            solver_input.fingerprint,
            (),
            (),
            (),
            precepting_solver.FillProposalCompromiseSummary((), (), (), (), (), ()),
            late_doc_removals=(removal,),
        )
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=fill_proposal,
            scheduler=self.scheduler,
            mode="repair",
        )
        review = proposal.late_doc_reviews.get(date=self.week)

        self.assertEqual(review.proposed_physician_id, baseline.pk)
        self.assertEqual(review.decision, PreceptingSolverLateDocReview.Decision.PENDING)
        with self.assertRaisesMessage(ValidationError, "confirmed or replaced"):
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        review_proposed_late_doc(
            proposal=proposal,
            on_date=self.week,
            decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()
        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )

        self.assertFalse(LateDocAssignment.objects.filter(date=self.week).exists())

    def test_retracting_a_proposal_only_late_doc_addition_removes_its_review_and_can_apply(self):
        self.ready_week()
        self.faculty("Faculty")
        proposal = self.retained_proposal()
        late_doc = proposal.proposed_assignments["late_doc"][0]

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="late_doc",
            on_date=date.fromisoformat(late_doc["date"]),
            period_of_day="",
            position="late-doc",
            physician=None,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        proposal.refresh_from_db()
        key = ("late_doc", self.week.isoformat(), "", "late-doc")
        self.assertNotIn(key, precepting_solver._effective_proposal_changes(proposal))
        self.assertFalse(proposal.late_doc_reviews.exists())

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )

        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.applied_at)
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_restoring_a_removed_baseline_late_doc_removes_its_review_and_can_apply(self):
        self.ready_week()
        baseline = self.faculty("Baseline")
        assign_late_doc(on_date=self.week, physician=baseline, scheduler=self.scheduler)
        solver_input = self.preflight(
            SolveDateRange(self.week, self.week.replace(day=7))
        ).solver_input
        fill_proposal = precepting_solver.FillProposal(
            solver_input.fingerprint,
            (),
            (),
            (),
            precepting_solver.FillProposalCompromiseSummary((), (), (), (), (), ()),
        )
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=fill_proposal,
            scheduler=self.scheduler,
            mode="repair",
        )

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="late_doc",
            on_date=self.week,
            period_of_day="",
            position="late-doc",
            physician=None,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()
        self.assertTrue(proposal.late_doc_reviews.filter(date=self.week).exists())

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="late_doc",
            on_date=self.week,
            period_of_day="",
            position="late-doc",
            physician=baseline,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        proposal.refresh_from_db()
        key = ("late_doc", self.week.isoformat(), "", "late-doc")
        self.assertNotIn(key, precepting_solver._effective_proposal_changes(proposal))
        self.assertFalse(
            PreceptingSolverLateDocReview.objects.filter(proposal=proposal, date=self.week).exists()
        )

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )

        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.applied_at)
        self.assertEqual(LateDocAssignment.objects.get(date=self.week).physician_id, baseline.pk)

    def test_retention_keeps_review_evidence_without_mutating_the_draft(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input
        fill_proposal = produce_fill_proposal(solver_input)
        assignment_count = PreceptorAssignment.objects.count()
        late_doc_count = LateDocAssignment.objects.count()

        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=fill_proposal,
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": []},
        )
        comparison = compare_fill_proposal(
            solver_input,
            fill_proposal,
            fill_proposal.assignments[0],
            exclude=True,
        )
        retained_comparison = retain_proposal_comparison(
            proposal=proposal, comparison=comparison, scheduler=self.scheduler
        )

        proposal.refresh_from_db()
        self.assertEqual(proposal.mode, "fill")
        self.assertEqual(proposal.input_fingerprint, solver_input.fingerprint)
        self.assertEqual(proposal.policy_version, "late-doc-band-v1")
        self.assertEqual(proposal.proposed_assignments["precepting"][0]["position"], "primary-1")
        self.assertEqual(proposal.late_doc_reviews.count(), 1)
        self.assertEqual(proposal.late_doc_reviews.first().decision, "pending")
        self.assertEqual(retained_comparison.proposal_id, proposal.pk)
        self.assertEqual(PreceptorAssignment.objects.count(), assignment_count)
        self.assertEqual(LateDocAssignment.objects.count(), late_doc_count)

    def test_retention_projects_proposed_late_doc_findings_without_mutating_the_draft(self):
        self.ready_week()
        self.faculty("Faculty")

        proposal = self.retained_proposal()

        self.assertNotIn(
            "missing_late_doc", {finding["code"] for finding in proposal.before_findings}
        )
        self.assertNotIn(
            "missing_late_doc", {finding["code"] for finding in proposal.after_findings}
        )
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_late_doc_replacement_refreshes_the_projected_findings(self):
        self.ready_week()
        self.faculty("Available")
        replacement = self.faculty("Backup", availability="backup_only")
        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()

        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision=PreceptingSolverLateDocReview.Decision.REPLACED,
            replacement_physician=replacement,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        proposal.refresh_from_db()
        self.assertIn(
            "backup_only_late_doc", {finding["code"] for finding in proposal.after_findings}
        )
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_retention_keeps_existing_assignment_and_week_level_findings(self):
        unavailable = self.faculty("Unavailable")
        self.faculty("Available")
        open_weekly_draft(self.week)
        session = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=unavailable,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        solver_input = self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input

        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
        )

        findings = {
            (finding["code"], finding["date"], finding["period_of_day"]): finding
            for finding in proposal.before_findings
        }
        self.assertIn(("missing_normal_rounder", self.week.isoformat(), None), findings)
        assignment_finding = findings[("not_available_assignment", self.week.isoformat(), "AM")]
        self.assertEqual(assignment_finding["physician_ids"], [unavailable.pk])
        self.assertEqual(assignment_finding["explanation"]["position"], "primary-1")

    def test_retention_scopes_session_and_late_doc_findings_to_a_custom_range(self):
        unavailable = self.faculty("Unavailable")
        self.faculty("Available")
        sessions = open_weekly_draft(self.week)
        monday_am = next(
            session
            for session in sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=monday_am,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=unavailable,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        solver_input = self.preflight(
            SolveDateRange(date(2025, 2, 4), date(2025, 2, 7))
        ).solver_input

        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
        )

        for findings in (proposal.before_findings, proposal.after_findings):
            self.assertIn(
                ("missing_normal_rounder", self.week.isoformat(), None),
                {
                    (finding["code"], finding["date"], finding["period_of_day"])
                    for finding in findings
                },
            )
            self.assertNotIn("not_available_assignment", {finding["code"] for finding in findings})
            self.assertNotIn("missing_late_doc", {finding["code"] for finding in findings})

    def test_apply_requires_review_and_uses_replacement_late_doc_with_history(self):
        self.ready_week()
        proposed = self.faculty("Proposed")
        replacement = self.faculty("Replacement")
        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()

        with self.assertRaisesMessage(
            ValidationError, "Every proposed Late Doc assignment must be confirmed or replaced."
        ):
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision=PreceptingSolverLateDocReview.Decision.REPLACED,
            replacement_physician=replacement,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        period = SchedulePeriod.objects.get(month=self.week.replace(day=1))
        before_edit = period.last_edited_at
        applied = apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        period.refresh_from_db()
        self.assertIsNotNone(period.last_edited_at)
        self.assertNotEqual(period.last_edited_at, before_edit)
        self.assertEqual(applied.applied_by, self.scheduler)
        self.assertIsNotNone(applied.applied_at)
        self.assertEqual(LateDocAssignment.objects.get(date=self.week).physician, replacement)
        self.assertNotEqual(LateDocAssignment.objects.get(date=self.week).physician, proposed)
        self.assertTrue(
            PreceptorAssignmentChange.objects.filter(
                changed_by=self.scheduler,
                reason=f"Applied Precepting solver proposal {proposal.pk}.",
            ).exists()
        )
        self.assertTrue(
            LateDocAssignmentChange.objects.filter(
                changed_by=self.scheduler, note=f"Applied Precepting solver proposal {proposal.pk}."
            ).exists()
        )

    def test_apply_refuses_a_stale_proposal_without_partial_mutation(self):
        self.ready_week()
        first = self.faculty("First")
        self.faculty("Second")
        proposal = self.retained_proposal()
        self.confirm_late_docs(proposal)
        target = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=target,
            physician=first,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        history_count = PreceptorAssignmentChange.objects.count()
        late_doc_history_count = LateDocAssignmentChange.objects.count()

        with self.assertRaisesMessage(ValidationError, "The solver proposal is stale"):
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        self.assertEqual(PreceptorAssignmentChange.objects.count(), history_count)
        self.assertEqual(LateDocAssignmentChange.objects.count(), late_doc_history_count)
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_apply_rolls_back_all_assignments_when_a_later_mutation_fails(self):
        self.ready_week()
        self.faculty("Faculty")
        proposal = self.retained_proposal()
        self.confirm_late_docs(proposal)
        preceptor_count = PreceptorAssignment.objects.count()
        preceptor_history_count = PreceptorAssignmentChange.objects.count()
        period = SchedulePeriod.objects.get(month=self.week.replace(day=1))
        before_edit = period.last_edited_at

        with (
            patch(
                "schedules.services.late_docs.assign_late_doc",
                side_effect=ValidationError("late doc failed"),
            ),
            self.assertRaisesMessage(ValidationError, "late doc failed"),
        ):
            apply_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                confirm_hard_findings=True,
                expected_revision=proposal.revision,
                hard_finding_reason="Keep this reviewed checkpoint.",
            )

        proposal.refresh_from_db()
        self.assertIsNone(proposal.applied_at)
        self.assertEqual(PreceptorAssignment.objects.count(), preceptor_count)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), preceptor_history_count)
        self.assertFalse(LateDocAssignment.objects.exists())
        period.refresh_from_db()
        self.assertEqual(period.last_edited_at, before_edit)

    def test_reapplying_an_applied_proposal_does_not_duplicate_effects(self):
        self.ready_week()
        self.faculty("Faculty")
        proposal = self.retained_proposal()
        self.confirm_late_docs(proposal)

        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )
        counts = (
            PreceptorAssignment.objects.count(),
            PreceptorAssignmentChange.objects.count(),
            LateDocAssignmentChange.objects.count(),
        )
        apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        self.assertEqual(
            (
                PreceptorAssignment.objects.count(),
                PreceptorAssignmentChange.objects.count(),
                LateDocAssignmentChange.objects.count(),
            ),
            counts,
        )

    def test_manual_assignment_edit_is_retained_without_rewriting_solver_output(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        alternatives = [self.faculty(f"Manual choice {index}") for index in range(4)]
        proposal = self.retained_proposal()
        original = proposal.proposed_assignments["precepting"][0].copy()
        alternative = next(
            physician
            for physician in alternatives
            if physician.pk
            not in {
                assignment["physician_id"]
                for assignment in proposal.proposed_assignments["precepting"]
                if assignment["date"] == original["date"]
                and assignment["period_of_day"] == original["period_of_day"]
            }
        )

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=date.fromisoformat(original["date"]),
            period_of_day=original["period_of_day"],
            position=original["position"],
            physician=alternative,
            note="Coverage tradeoff discussed with the team.",
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        proposal.refresh_from_db()
        assignments, _ = effective_proposal_assignments(proposal)
        edited = next(
            item
            for item in assignments
            if item["date"] == original["date"]
            and item["period_of_day"] == original["period_of_day"]
            and item["position"] == original["position"]
        )
        history = PreceptingSolverProposalEdit.objects.get(proposal=proposal)
        self.assertEqual(proposal.proposed_assignments["precepting"][0], original)
        self.assertEqual(edited["physician_id"], alternative.pk)
        self.assertEqual(history.previous_value["physician_id"], original["physician_id"])
        self.assertEqual(history.resulting_value["physician_id"], alternative.pk)
        self.assertEqual(history.note, "Coverage tradeoff discussed with the team.")
        self.assertEqual(proposal.last_edited_by, self.scheduler)
        self.assertEqual(proposal.revision, 2)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.confirm_late_docs(proposal)

        apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        self.assertEqual(
            PreceptorAssignment.objects.get(
                session_id=original["session_id"],
                responsibility="primary",
                primary_position=int(original["position"].removeprefix("primary-")),
            ).physician,
            alternative,
        )

    def test_proposal_assignment_context_uses_effective_schedule_and_all_availability_tiers(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        self.faculty("Third")
        community = create_physician(display_name="Community", scheduler=self.scheduler)
        add_role(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        backup = self.faculty("Backup", availability="backup_only")
        unavailable = self.faculty("Unavailable", availability="off")
        proposal = self.retained_proposal()
        assignment = proposal.proposed_assignments["precepting"][0]

        context = proposal_assignment_context(
            proposal=proposal,
            assignment_type="precepting",
            on_date=date.fromisoformat(assignment["date"]),
            period_of_day=assignment["period_of_day"],
            position=assignment["position"],
        )

        alternatives = context["alternatives"]
        sibling = next(
            item
            for item in proposal.proposed_assignments["precepting"]
            if item["date"] == assignment["date"]
            and item["period_of_day"] == assignment["period_of_day"]
            and item["position"] != assignment["position"]
        )
        self.assertEqual(
            [tier.key for tier in alternatives.tiers],
            [
                "available_expected_community",
                "available_other",
                "backup_only",
                "not_available",
            ],
        )
        self.assertEqual(alternatives.current.physician.pk, assignment["physician_id"])
        self.assertNotIn(sibling["physician_id"], [item.physician.pk for item in alternatives])
        self.assertEqual(
            next(item for item in alternatives if item.physician == backup).tier, "backup_only"
        )
        unavailable_alternative = next(
            item for item in alternatives if item.physician == unavailable
        )
        self.assertEqual(unavailable_alternative.tier, "not_available")
        self.assertIn("hard availability finding", unavailable_alternative.selection_consequence)

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=date.fromisoformat(assignment["date"]),
            period_of_day=assignment["period_of_day"],
            position=assignment["position"],
            physician=unavailable,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        proposal.refresh_from_db()
        self.assertIn(
            "not_available_assignment", {finding["code"] for finding in proposal.after_findings}
        )
        self.assertFalse(PreceptorAssignment.objects.exists())

        with self.assertRaisesMessage(ValidationError, "no longer eligible"):
            edit_proposed_assignment(
                proposal=proposal,
                assignment_type="precepting",
                on_date=date.fromisoformat(assignment["date"]),
                period_of_day=assignment["period_of_day"],
                position="secondary",
                physician=community,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
            )
        self.assertFalse(PreceptorAssignment.objects.exists())

        self.confirm_late_docs(proposal)
        preceptor_count = PreceptorAssignment.objects.count()
        late_doc_count = LateDocAssignment.objects.count()
        with self.assertRaises(HardFindingConfirmationRequired) as confirmation:
            apply_precepting_solver_proposal(proposal=proposal, scheduler=self.scheduler)

        self.assertIn(
            "not_available_assignment",
            {finding["code"] for finding in confirmation.exception.findings},
        )
        self.assertEqual(PreceptorAssignment.objects.count(), preceptor_count)
        self.assertEqual(LateDocAssignment.objects.count(), late_doc_count)

        proposal.refresh_from_db()
        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )

        self.assertEqual(
            PreceptorAssignment.objects.get(
                session_id=assignment["session_id"],
                responsibility="primary",
                primary_position=int(assignment["position"].removeprefix("primary-")),
            ).physician,
            unavailable,
        )
        self.assertIn(
            "not_available_assignment",
            {finding.code for finding in draft_findings_for_week(self.week).findings},
        )

    def test_concurrent_or_stale_manual_edit_refuses_without_history_mutation(self):
        self.ready_week()
        self.faculty("First")
        self.faculty("Second")
        replacements = [self.faculty(f"Replacement {index}") for index in range(4)]
        proposal = self.retained_proposal()
        original = proposal.proposed_assignments["precepting"][0]
        replacement = next(
            physician
            for physician in replacements
            if physician.pk
            not in {
                assignment["physician_id"]
                for assignment in proposal.proposed_assignments["precepting"]
                if assignment["date"] == original["date"]
                and assignment["period_of_day"] == original["period_of_day"]
            }
        )
        stale_revision = proposal.revision
        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=date.fromisoformat(original["date"]),
            period_of_day=original["period_of_day"],
            position=original["position"],
            physician=replacement,
            scheduler=self.scheduler,
            expected_revision=stale_revision,
        )

        with self.assertRaisesMessage(ValidationError, "changed while you were editing"):
            edit_proposed_assignment(
                proposal=proposal,
                assignment_type="precepting",
                on_date=date.fromisoformat(original["date"]),
                period_of_day=original["period_of_day"],
                position=original["position"],
                physician=self.faculty("Ignored"),
                scheduler=self.scheduler,
                expected_revision=stale_revision,
            )

        self.assertEqual(PreceptingSolverProposalEdit.objects.filter(proposal=proposal).count(), 1)

    def test_manual_assignment_edit_refuses_a_retained_excluded_physician(self):
        self.ready_week()
        self.faculty("First")
        excluded = self.faculty("Excluded")
        solver_input = without_excluded_physicians(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            [excluded.pk],
        )
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": [excluded.pk]},
        )
        assignment = proposal.proposed_assignments["precepting"][0]

        with self.assertRaisesMessage(ValidationError, "no longer eligible"):
            edit_proposed_assignment(
                proposal=proposal,
                assignment_type="precepting",
                on_date=date.fromisoformat(assignment["date"]),
                period_of_day=assignment["period_of_day"],
                position=assignment["position"],
                physician=excluded,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
            )

        self.assertFalse(proposal.edits.exists())

    def test_late_doc_review_refuses_a_retained_excluded_physician(self):
        self.ready_week()
        self.faculty("Available")
        excluded = self.faculty("Excluded")
        solver_input = without_excluded_physicians(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            [excluded.pk],
        )
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": [excluded.pk], "solve_seed": "0"},
        )
        review = proposal.late_doc_reviews.get()

        with self.assertRaisesMessage(ValidationError, "no longer eligible for Late Doc"):
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.REPLACED,
                replacement_physician=excluded,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
            )

    def test_retained_proposal_reconstruction_keeps_excluded_assignment_metadata(self):
        excluded = self.faculty("Excluded")
        other_faculty = self.faculty("Other Faculty")
        self.ready_week()
        monday_am = next(
            session
            for session in open_weekly_draft(self.week)
            if session.date == self.week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=monday_am,
            physician=excluded,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=monday_am,
            physician=other_faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.scheduler,
        )
        solver_input = without_excluded_physicians(
            self.preflight(SolveDateRange(self.week, date(2025, 2, 7))).solver_input,
            [excluded.pk],
        )
        proposal = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": [excluded.pk], "solve_seed": "0"},
        )

        reconstructed = precepting_solver._current_proposal_input(proposal)
        reconstructed_session = next(
            session
            for session in reconstructed.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )

        self.assertEqual(reconstructed.excluded_physician_ids, (excluded.pk,))
        self.assertIn(
            excluded.pk,
            {candidate.physician_id for candidate in reconstructed_session.candidates},
        )
        self.assertNotIn(
            "insufficient_faculty_equivalent_coverage",
            [
                requirement.code
                for requirement in produce_fill_proposal(reconstructed).unresolved_requirements
                if requirement.date == self.week and requirement.period_of_day == "AM"
            ],
        )

    def test_manual_late_doc_choice_requires_fresh_confirmation(self):
        self.ready_week()
        physicians = (self.faculty("First"), self.faculty("Second"))
        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()
        replacement = next(
            physician for physician in physicians if physician.pk != review.proposed_physician_id
        )
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="late_doc",
            on_date=review.date,
            period_of_day="",
            position="late-doc",
            physician=replacement,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        review.refresh_from_db()
        self.assertEqual(review.proposed_physician, replacement)
        self.assertEqual(review.decision, PreceptingSolverLateDocReview.Decision.PENDING)

    def test_late_doc_review_refuses_missing_stale_and_concurrent_revisions_without_writing(self):
        self.ready_week()
        changed = self.faculty("Changed")
        replacement = self.faculty("Replacement")
        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()
        revision = proposal.revision

        with self.assertRaisesMessage(ValidationError, "proposal revision is required"):
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
                scheduler=self.scheduler,
            )
        review.refresh_from_db()
        self.assertEqual(review.decision, PreceptingSolverLateDocReview.Decision.PENDING)

        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=changed,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        with self.assertRaisesMessage(ValidationError, "proposal is stale"):
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
                scheduler=self.scheduler,
                expected_revision=revision,
            )
        proposal.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(proposal.revision, revision)
        self.assertEqual(review.decision, PreceptingSolverLateDocReview.Decision.PENDING)

        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()
        stale_revision = proposal.revision
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
            scheduler=self.scheduler,
            expected_revision=stale_revision,
        )
        with self.assertRaisesMessage(ValidationError, "changed while you were editing"):
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.REPLACED,
                replacement_physician=replacement,
                scheduler=self.scheduler,
                expected_revision=stale_revision,
            )
        review.refresh_from_db()
        self.assertEqual(review.decision, PreceptingSolverLateDocReview.Decision.CONFIRMED)

    def test_manual_removal_projects_findings_and_workload_as_the_applied_draft(self):
        self.ready_week()
        baseline = self.faculty("Baseline")
        self.faculty("Alternative")
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=baseline,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        proposal = self.retained_proposal()

        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=self.week,
            period_of_day="AM",
            position="primary-1",
            physician=None,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()
        after_codes = {finding["code"] for finding in proposal.after_findings}
        self.assertIn("missing_primary_position", after_codes)
        self.assertNotIn("insufficient_faculty_equivalent_coverage", after_codes)
        projected_total = next(
            item["proposed_total"]
            for item in proposal.workload_effects["faculty"]
            if item["physician_id"] == baseline.pk
        )

        self.confirm_late_docs(proposal)
        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=proposal.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )

        self.assertIn(
            "missing_primary_position",
            {finding.code for finding in draft_findings_for_week(self.week).findings},
        )
        self.assertEqual(
            PreceptorAssignment.objects.filter(
                physician=baseline,
                session__date__range=(self.week, date(2025, 2, 7)),
            ).count(),
            projected_total,
        )


class RealisticPreceptingSolverScenarioTests(TestCase):
    """A six-month fixture that keeps the completed operator workflow exercised together."""

    week = date(2027, 1, 4)
    balance_window = SolveDateRange(date(2026, 7, 9), date(2027, 1, 8))

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scenario-scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.today = patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2026, 7, 1)
        )
        self.today.start()
        self.addCleanup(self.today.stop)
        self._build_six_month_fixture()

    @staticmethod
    def _core_slots():
        return {
            slot: ("off" if slot == "friday_pm" else "open")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }

    def faculty(self, name, *, start=date(2026, 7, 1), slots=None):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=start,
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=start,
            effective_end=None,
            slots=slots or self._core_slots(),
            scheduler=self.scheduler,
        )
        return physician

    def preflight(self):
        return preflight_precepting_solve(
            SolveDateRange(self.week, date(2027, 1, 8)), balance_window=self.balance_window
        ).solver_input

    def _build_six_month_fixture(self):
        self.ada = self.faculty("Ada Broad Capacity")
        self.eve = self.faculty("Eve Equal Choice")
        self.casey = self.faculty("Casey November Start", start=date(2026, 11, 1))
        self.blair = create_physician(
            display_name="Blair Effective-Date Change", scheduler=self.scheduler
        )
        add_role(
            physician=self.blair,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 7, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=self.blair,
            effective_start=date(2026, 7, 1),
            effective_end=date(2026, 10, 31),
            slots=self._core_slots(),
            scheduler=self.scheduler,
        )
        blair_changed_slots = self._core_slots()
        blair_changed_slots["tuesday_pm"] = "off"
        create_faculty_availability_template(
            physician=self.blair,
            effective_start=date(2026, 11, 1),
            effective_end=None,
            slots=blair_changed_slots,
            scheduler=self.scheduler,
        )
        thin_monday_slots = {
            slot: ("open" if slot == "monday_pm" else "off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        self.thin_monday = self.faculty("Dev Thin Monday PM", slots=thin_monday_slots)
        backup_slots = {slot: "backup_only" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        self.backup_one = self.faculty("Backup Fallback One", slots=backup_slots)
        self.backup_two = self.faculty("Backup Fallback Two", slots=backup_slots)
        self.normal_rounder = self.faculty("Normal Rounder")

        self.community = create_physician(
            display_name="Community Monday Commitment", scheduler=self.scheduler
        )
        add_role(
            physician=self.community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
            effective_start=date(2026, 7, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_community_preceptor_rule(
            physician=self.community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2026, 7, 1),
            scheduler=self.scheduler,
        )
        casual = create_physician(display_name="Casual Rounding Support", scheduler=self.scheduler)
        create_casual_rounder_rule(
            physician=casual,
            weekday=0,
            ordinals=[1],
            effective_start=date(2026, 7, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )

        monday = self.balance_window.start_date - timedelta(
            days=self.balance_window.start_date.weekday()
        )
        while monday <= self.week:
            record_normal_rounder(
                week=monday, physician=self.normal_rounder, scheduler=self.scheduler
            )
            monday += timedelta(days=7)
        august_week = date(2026, 8, 3)
        august_session = next(
            session
            for session in open_weekly_draft(august_week)
            if session.date == august_week and session.period_of_day == "AM"
        )
        assign_preceptor(
            session=august_session,
            physician=self.ada,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        open_weekly_draft(self.week)
        locked_session = PreceptorSession.objects.get(date=self.week, period_of_day="PM")
        locked = assign_preceptor(
            session=locked_session,
            physician=self.ada,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        set_preceptor_assignment_lock(assignment=locked, is_locked=True, scheduler=self.scheduler)

        create_vacation(
            physician=self.eve,
            start_date=date(2027, 1, 5),
            end_date=date(2027, 1, 5),
            scheduler=self.scheduler,
        )
        definition = create_academic_event_definition(
            base_name="January faculty retreat",
            start_academic_year=2026,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2027, 1, 6),
            end_date=date(2027, 1, 6),
            participants=[self.ada],
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        interview = create_interview_shift(
            academic_year=2026, date=date(2027, 1, 7), scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=self.blair, scheduler=self.scheduler)

    def test_six_month_fill_repair_review_apply_scenario(self):
        solver_input = self.preflight()
        capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
        tuesday_pm = next(
            session
            for session in solver_input.sessions
            if session.date == date(2027, 1, 5) and session.period_of_day == "PM"
        )
        monday_am = next(
            session
            for session in solver_input.sessions
            if session.date == self.week and session.period_of_day == "AM"
        )
        monday_pm = next(
            session
            for session in solver_input.sessions
            if session.date == self.week and session.period_of_day == "PM"
        )
        wednesday_am = next(
            session
            for session in solver_input.sessions
            if session.date == date(2027, 1, 6) and session.period_of_day == "AM"
        )
        thursday_am = next(
            session
            for session in solver_input.sessions
            if session.date == date(2027, 1, 7) and session.period_of_day == "AM"
        )

        self.assertGreater(
            capacities[self.ada.pk].precepting_capacity,
            capacities[self.casey.pk].precepting_capacity,
        )
        self.assertEqual(
            next(
                item for item in tuesday_pm.candidates if item.physician_id == self.blair.pk
            ).availability,
            "not_available",
        )
        self.assertEqual(
            next(
                item for item in tuesday_pm.candidates if item.physician_id == self.eve.pk
            ).availability,
            "not_available",
        )
        self.assertEqual(
            next(
                item for item in wednesday_am.candidates if item.physician_id == self.ada.pk
            ).availability,
            "not_available",
        )
        self.assertIsNotNone(
            next(
                item for item in thursday_am.candidates if item.physician_id == self.blair.pk
            ).interview_shift_id
        )
        self.assertEqual(
            next(
                item for item in monday_am.candidates if item.physician_id == self.normal_rounder.pk
            ).availability,
            "backup_only",
        )
        self.assertEqual(
            next(
                item for item in monday_pm.candidates if item.physician_id == self.thin_monday.pk
            ).availability,
            "available",
        )
        self.assertTrue(monday_pm.positions[0].assignment.is_locked)

        started = perf_counter()
        first = produce_fill_proposal(solver_input)
        elapsed = perf_counter() - started
        repeated = produce_fill_proposal(solver_input)

        self.assertEqual(first, repeated)
        self.assertFalse(first.unresolved_requirements)
        self.assertLess(elapsed, 10, f"Representative bounded solve took {elapsed:.2f} seconds.")
        self.assertTrue(first.compromises.faculty_workloads)
        self.assertTrue(first.compromises.late_doc_workloads)
        community_choice = next(
            item for item in first.assignments if item.physician_id == self.community.pk
        )
        backup_choices = [
            item
            for item in first.assignments
            if item.physician_id in {self.backup_one.pk, self.backup_two.pk}
        ]
        workload_choice = next(
            item
            for item in first.assignments
            if "workload balance" in " ".join(item.explanation.policy_advantages).lower()
        )
        normal_choice = next(
            item
            for item in first.assignments
            if item.physician_id not in {self.community.pk, self.backup_one.pk, self.backup_two.pk}
        )
        late_doc = first.late_doc_assignments[0]

        self.assertIn(
            "Fulfills the expected Community Preceptor commitment.",
            community_choice.explanation.policy_advantages,
        )
        self.assertEqual(len(backup_choices), 2)
        self.assertTrue(
            all(
                "backup-only Faculty fallback" in " ".join(item.explanation.policy_disadvantages)
                for item in backup_choices
            )
        )
        self.assertTrue(workload_choice.explanation.policy_advantages)
        normal_session = next(
            session
            for session in solver_input.sessions
            if (session.date, session.period_of_day)
            == (normal_choice.date, normal_choice.period_of_day)
        )
        self.assertEqual(
            next(
                item
                for item in normal_session.candidates
                if item.physician_id == normal_choice.physician_id
            ).availability,
            "available",
        )
        self.assertIn("Late Doc balance policy", late_doc.explanation.policy_advantages[-1])

        initial = retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=first,
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
        )
        self.assertFalse(
            [finding for finding in initial.after_findings if finding["severity"] == "hard"]
        )
        self.assertGreaterEqual(
            next(
                item.current_total
                for item in first.compromises.faculty_workloads
                if item.physician_id == self.ada.pk
            ),
            1,
        )
        for review in initial.late_doc_reviews.all():
            review_proposed_late_doc(
                proposal=initial,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
                scheduler=self.scheduler,
                expected_revision=initial.revision,
            )
            initial.refresh_from_db()
        apply_precepting_solver_proposal(
            proposal=initial, scheduler=self.scheduler, expected_revision=initial.revision
        )

        stale_input = self.preflight()
        stale = retain_fill_proposal(
            solver_input=stale_input,
            fill_proposal=produce_fill_proposal(stale_input),
            scheduler=self.scheduler,
        )
        repair_target = (
            PreceptorAssignment.objects.filter(
                session__date__range=(self.week, date(2027, 1, 8)), is_locked=False
            )
            .exclude(physician=self.ada)
            .select_related("session", "physician")
            .first()
        )
        create_vacation(
            physician=repair_target.physician,
            start_date=repair_target.session.date,
            end_date=repair_target.session.date,
            period_of_day=repair_target.session.period_of_day,
            scheduler=self.scheduler,
        )
        with self.assertRaisesMessage(ValidationError, "The solver proposal is stale"):
            apply_precepting_solver_proposal(proposal=stale, scheduler=self.scheduler)

        repair_input = self.preflight()
        repair = produce_repair_proposal(repair_input)
        self.assertFalse(repair.unresolved_requirements)
        self.assertEqual(
            len(repair.assignments),
            1,
            [
                (item.date, item.period_of_day, item.position, item.physician_name)
                for item in repair.assignments
            ],
        )
        self.assertFalse(repair.precepting_removals)
        self.assertEqual(
            (
                repair.assignments[0].date,
                repair.assignments[0].period_of_day,
                repair.assignments[0].position,
            ),
            (repair_target.session.date, repair_target.session.period_of_day, "primary-1"),
        )
        repaired = retain_fill_proposal(
            solver_input=repair_input,
            fill_proposal=repair,
            scheduler=self.scheduler,
            mode="repair",
        )
        for review in repaired.late_doc_reviews.all():
            review_proposed_late_doc(
                proposal=repaired,
                on_date=review.date,
                decision=PreceptingSolverLateDocReview.Decision.CONFIRMED,
                scheduler=self.scheduler,
                expected_revision=repaired.revision,
            )
            repaired.refresh_from_db()
        self.assertFalse(
            [finding for finding in repaired.after_findings if finding["severity"] == "hard"]
        )
        apply_precepting_solver_proposal(
            proposal=repaired, scheduler=self.scheduler, expected_revision=repaired.revision
        )

        self.assertTrue(
            PreceptorAssignmentChange.objects.filter(
                changed_by=self.scheduler, reason__contains="Applied Precepting solver proposal"
            ).exists()
        )


class PreceptingSolverProposalReconciliationTests(RetainedPreceptingSolverProposalTests):
    def _stale_proposal_with_same_position_conflict(self):
        self.ready_week()
        self.faculty("Proposed")
        proposal = self.retained_proposal()
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
        return proposal, proposed, current

    def test_reconcile_creates_a_new_proposal_with_conflict_choice_and_fresh_late_doc_review(self):
        proposal, proposed, _current = self._stale_proposal_with_same_position_conflict()
        draft_history_count = PreceptorAssignmentChange.objects.count()
        preview = preview_proposal_reconciliation(proposal)
        conflict = next(
            item
            for item in preview.conflicts
            if item["date"] == proposed["date"]
            and item["period_of_day"] == proposed["period_of_day"]
            and item["position"] == proposed["position"]
        )

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={":".join(conflict["key"]): "proposal"},
        )

        proposal.refresh_from_db()
        self.assertEqual(proposal.revision, 1)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), draft_history_count)
        self.assertEqual(reconciled.reconciliation.source_proposal, proposal)
        self.assertEqual(reconciled.reconciliation.reconciled_by, self.scheduler)
        self.assertEqual(reconciled.reconciliation.choices[0]["choice"], "proposal")
        self.assertEqual(
            next(
                assignment["physician_id"]
                for assignment in reconciled.proposed_assignments["precepting"]
                if assignment["date"] == proposed["date"]
                and assignment["period_of_day"] == proposed["period_of_day"]
                and assignment["position"] == proposed["position"]
            ),
            proposed["physician_id"],
        )
        self.assertTrue(reconciled.late_doc_reviews.exists())
        self.assertTrue(
            all(
                review.decision == PreceptingSolverLateDocReview.Decision.PENDING
                for review in reconciled.late_doc_reviews.all()
            )
        )

    def test_reconcile_combines_independent_positions_and_omits_already_matched_changes(self):
        self.ready_week()
        self.faculty("Proposed")
        proposal = self.retained_proposal()
        current = self.faculty("Current")
        matched = proposal.proposed_assignments["precepting"][0]
        proposal_only = next(
            assignment
            for assignment in proposal.proposed_assignments["precepting"]
            if assignment["session_id"] != matched["session_id"]
        )
        session = PreceptorSession.objects.get(pk=matched["session_id"])
        assign_preceptor(
            session=session,
            physician=Physician.objects.get(pk=matched["physician_id"]),
            responsibility="primary",
            primary_position=int(matched["position"].removeprefix("primary-")),
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=session,
            physician=current,
            responsibility="secondary",
            primary_position=None,
            scheduler=self.scheduler,
        )
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )

        effective_precepting, _ = effective_proposal_assignments(reconciled)
        self.assertEqual(preview.incorporated_count, 1)
        self.assertEqual(preview.conflicts, ())
        self.assertNotIn(
            (matched["date"], matched["period_of_day"], matched["position"]),
            {
                (assignment["date"], assignment["period_of_day"], assignment["position"])
                for assignment in effective_precepting
            },
        )
        self.assertIn(
            (proposal_only["date"], proposal_only["period_of_day"], proposal_only["position"]),
            {
                (assignment["date"], assignment["period_of_day"], assignment["position"])
                for assignment in effective_precepting
            },
        )
        current_snapshot = next(
            item
            for item in reconciled.input_snapshot["sessions"]
            if item["date"] == matched["date"] and item["period_of_day"] == matched["period_of_day"]
        )
        self.assertEqual(
            next(item for item in current_snapshot["positions"] if item["key"] == "secondary")[
                "assignment"
            ]["physician_id"],
            current.pk,
        )

    def test_reconcile_preserves_removal_as_an_explicit_delta(self):
        self.ready_week()
        assigned = self.faculty("Assigned")
        self.faculty("Proposed")
        session = next(
            item
            for item in open_weekly_draft(self.week)
            if item.date == self.week and item.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=assigned,
            responsibility="primary",
            primary_position=1,
            scheduler=self.scheduler,
        )
        proposal = self.retained_proposal()
        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=self.week,
            period_of_day="AM",
            position="primary-1",
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        create_monthly_physician_availability_rule(
            physician=assigned,
            weekday=self.week.weekday(),
            ordinals=[1],
            periods=["PM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        proposal.refresh_from_db()
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )

        self.assertTrue(
            all(
                assignment is not None
                for assignments in reconciled.proposed_assignments.values()
                for assignment in assignments
            )
        )
        removal = reconciled.edits.get(
            assignment_type="precepting",
            date=self.week,
            period_of_day="AM",
            position="primary-1",
        )
        self.assertIsNone(removal.resulting_value)
        self.assertEqual(removal.previous_value["physician_id"], assigned.pk)
        self.assertEqual(removal.note, f"Reconciled from proposal {proposal.pk}.")

    def test_reconcile_refuses_a_draft_changed_after_its_preview(self):
        proposal, proposed, _current = self._stale_proposal_with_same_position_conflict()
        preview = preview_proposal_reconciliation(proposal)
        session = PreceptorSession.objects.get(pk=proposed["session_id"])
        later = self.faculty("Later")
        assign_preceptor(
            session=session,
            physician=later,
            responsibility="primary",
            primary_position=2 if proposed["position"] != "primary-2" else 1,
            scheduler=self.scheduler,
        )

        with self.assertRaisesMessage(
            ValidationError, "ordinary draft changed while you were reconciling"
        ):
            reconcile_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
                expected_current_input_fingerprint=preview.current_input_fingerprint,
                choices={},
            )

        self.assertEqual(proposal.reconciled_proposals.count(), 0)

    def test_reconcile_refuses_a_second_result_for_the_same_draft_revision(self):
        proposal, proposed, _current = self._stale_proposal_with_same_position_conflict()
        preview = preview_proposal_reconciliation(proposal)
        conflict = next(
            item
            for item in preview.conflicts
            if item["date"] == proposed["date"]
            and item["period_of_day"] == proposed["period_of_day"]
            and item["position"] == proposed["position"]
        )
        choices = {":".join(conflict["key"]): "proposal"}
        reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices=choices,
        )

        with self.assertRaisesMessage(ValidationError, "already reconciled with the current draft"):
            reconcile_precepting_solver_proposal(
                proposal=proposal,
                scheduler=self.scheduler,
                expected_revision=proposal.revision,
                expected_current_input_fingerprint=preview.current_input_fingerprint,
                choices=choices,
            )

        self.assertEqual(proposal.reconciled_proposals.count(), 1)

    def test_reconcile_retains_a_newly_ineligible_assignment_as_a_hard_finding(self):
        self.ready_week()
        self.faculty("Proposed")
        proposal = self.retained_proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        proposed_date = date.fromisoformat(proposed["date"])
        physician = Physician.objects.get(pk=proposed["physician_id"])
        create_monthly_physician_availability_rule(
            physician=physician,
            weekday=proposed_date.weekday(),
            ordinals=[(proposed_date.day - 1) // 7 + 1],
            periods=[proposed["period_of_day"]],
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            scheduler=self.scheduler,
        )
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )

        self.assertIn(
            "not_available_assignment", {finding["code"] for finding in reconciled.after_findings}
        )
        self.confirm_late_docs(reconciled)
        apply_precepting_solver_proposal(
            proposal=reconciled,
            scheduler=self.scheduler,
            confirm_hard_findings=True,
            expected_revision=reconciled.revision,
            hard_finding_reason="Keep this reviewed checkpoint.",
        )
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session_id=proposed["session_id"],
                responsibility="primary",
                primary_position=int(proposed["position"].removeprefix("primary-")),
            ).physician,
            physician,
        )

    def test_reconcile_discards_proposed_positions_closed_by_a_new_holiday(self):
        self.ready_week()
        self.faculty("Proposed")
        proposal = self.retained_proposal()
        self.assertTrue(
            any(
                assignment["date"] == self.week.isoformat()
                for assignment in proposal.proposed_assignments["precepting"]
            )
        )
        occurrence = holiday_occurrences_for(2025).get(holiday=ClinicHoliday.INDEPENDENCE_DAY)
        schedule_holiday_occurrence(
            occurrence=occurrence,
            observed_date=self.week,
            scheduler=self.scheduler,
        )
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )

        self.assertFalse(
            any(
                assignment["date"] == self.week.isoformat()
                for assignment in reconciled.proposed_assignments["precepting"]
            )
        )
        self.assertFalse(reconciled.late_doc_reviews.filter(date=self.week).exists())

    def test_reconcile_keeps_current_choice_validation_for_changed_source_facts(self):
        self.ready_week()
        self.faculty("Proposed")
        proposal = self.retained_proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        proposed_date = date.fromisoformat(proposed["date"])
        physician = Physician.objects.get(pk=proposed["physician_id"])
        interview = create_interview_shift(
            academic_year=2024, date=proposed_date, scheduler=self.scheduler
        )
        assign_interview_shift(shift=interview, physician=physician, scheduler=self.scheduler)
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )
        self.confirm_late_docs(reconciled)

        with self.assertRaisesMessage(
            ValidationError, "A proposed Precepting assignment is no longer valid."
        ):
            apply_precepting_solver_proposal(
                proposal=reconciled,
                scheduler=self.scheduler,
                confirm_hard_findings=True,
                expected_revision=reconciled.revision,
                hard_finding_reason="Keep this reviewed checkpoint.",
            )

    def test_reconcile_carries_a_late_doc_replacement_for_fresh_confirmation(self):
        self.ready_week()
        self.faculty("Proposed")
        replacement = self.faculty("Replacement")
        proposal = self.retained_proposal()
        review = proposal.late_doc_reviews.get()
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision=PreceptingSolverLateDocReview.Decision.REPLACED,
            replacement_physician=replacement,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()
        self.faculty("Changed source fact")
        preview = preview_proposal_reconciliation(proposal)

        reconciled = reconcile_precepting_solver_proposal(
            proposal=proposal,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
            expected_current_input_fingerprint=preview.current_input_fingerprint,
            choices={},
        )

        carried_review = reconciled.late_doc_reviews.get(date=review.date)
        self.assertEqual(carried_review.proposed_physician, replacement)
        self.assertEqual(carried_review.decision, PreceptingSolverLateDocReview.Decision.PENDING)


class PreceptingSolverProposalReconciliationSerializationTests(TransactionTestCase):
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
        self.today = patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2025, 1, 1)
        )
        self.today.start()
        self.addCleanup(self.today.stop)

    def faculty(self, name):
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
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def retained_proposal(self):
        solver_input = preflight_precepting_solve(
            SolveDateRange(self.week, date(2025, 2, 7))
        ).solver_input
        return retain_fill_proposal(
            solver_input=solver_input,
            fill_proposal=produce_fill_proposal(solver_input),
            scheduler=self.scheduler,
            policy_version="late-doc-band-v1",
            solve_settings={"excluded_physician_ids": []},
        )

    def test_draft_mutation_waits_until_reconciliation_persists(self):
        rounder = self.faculty("Rounder")
        record_normal_rounder(week=self.week, physician=rounder, scheduler=self.scheduler)
        open_weekly_draft(self.week)
        self.faculty("Proposed")
        proposal = self.retained_proposal()
        proposed = proposal.proposed_assignments["precepting"][0]
        current = self.faculty("Current")
        session = PreceptorSession.objects.get(pk=proposed["session_id"])
        responsibility = "secondary" if proposed["position"] == "secondary" else "primary"
        primary_position = (
            None
            if responsibility == "secondary"
            else int(proposed["position"].removeprefix("primary-"))
        )
        assign_preceptor(
            session=session,
            physician=current,
            responsibility=responsibility,
            primary_position=primary_position,
            scheduler=self.scheduler,
        )
        later = self.faculty("Later")
        preview = preview_proposal_reconciliation(proposal)
        conflict = next(
            item for item in preview.conflicts if item["position"] == proposed["position"]
        )
        mutation_waits_for_reconciliation = Event()
        mutation_finished = Event()
        errors = []

        def change_draft():
            close_old_connections()
            try:
                assign_preceptor(
                    session=session,
                    physician=later,
                    responsibility=responsibility,
                    primary_position=primary_position,
                    scheduler=self.scheduler,
                )
            except Exception as error:
                errors.append(error)
            finally:
                mutation_finished.set()
                close_old_connections()

        worker = Thread(target=change_draft)

        class TrackingLock:
            def __init__(self):
                self.lock = RLock()

            def __enter__(self):
                if current_thread() is worker:
                    mutation_waits_for_reconciliation.set()
                self.lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.lock.release()

        original_create = precepting_solver.PreceptingSolverProposal.objects.create

        def create_while_mutation_waits(*args, **kwargs):
            worker.start()
            self.assertTrue(mutation_waits_for_reconciliation.wait(1))
            self.assertFalse(mutation_finished.is_set())
            return original_create(*args, **kwargs)

        with patch.object(_mutation, "_scheduling_mutation_lock", TrackingLock()):
            with patch.object(
                precepting_solver.PreceptingSolverProposal.objects,
                "create",
                side_effect=create_while_mutation_waits,
            ):
                reconciled = reconcile_precepting_solver_proposal(
                    proposal=proposal,
                    scheduler=self.scheduler,
                    expected_revision=proposal.revision,
                    expected_current_input_fingerprint=preview.current_input_fingerprint,
                    choices={":".join(conflict["key"]): "proposal"},
                )
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(reconciled.input_fingerprint, preview.current_input_fingerprint)
        self.assertEqual(proposal.reconciled_proposals.count(), 1)
