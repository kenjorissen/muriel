from datetime import date
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from schedules.management.commands.reset_demo import DEMO_SCHEDULING_ADMINISTRATOR_USERNAME
from schedules.management.commands.reset_production_domain import (
    SYNTHETIC_DEFAULT_SEED,
    replace_domain,
)
from schedules.services.precepting_solver import (
    SYNCHRONOUS_SOLVER_TIME_SECONDS,
    PreceptingSolverTimeout,
    SolveDateRange,
    preflight_precepting_solve,
    produce_fill_proposal,
    produce_repair_proposal,
)
from schedules.services.preceptors import initialize_preceptor_session_range
from schedules.services.work_remaining import shift_date_months

SYNTHETIC_TIMING_TODAY = date(2026, 8, 1)
SYNTHETIC_SOLVE_START = date(2026, 9, 1)


class Command(BaseCommand):
    help = "Time the untouched commissioned synthetic Schedule Bot scenario."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=SYNCHRONOUS_SOLVER_TIME_SECONDS)

    def handle(self, *args, **options):
        timeout = options["timeout"]
        if timeout < 1:
            raise CommandError("Timeout must be at least one second.")
        self._prepare_synthetic_scenario()
        measurements = tuple(
            (
                f"Synthetic {months}-month",
                SolveDateRange(
                    SYNTHETIC_SOLVE_START,
                    date.fromordinal(
                        shift_date_months(SYNTHETIC_SOLVE_START, months).toordinal() - 1
                    ),
                ),
            )
            for months in (1, 3)
        )
        with patch(
            "schedules.services.precepting_solver.organization_today",
            return_value=SYNTHETIC_TIMING_TODAY,
        ):
            results = [
                self._measure_range(label, solve_range, timeout)
                for label, solve_range in measurements
            ]
        three_month = results[-1]
        self.stdout.write(
            "three-month target (each end-to-end preflight plus one solve <= 6.000s): "
            f"{'met' if self._three_month_target_met(*three_month) else 'not met'}"
        )

    def _prepare_synthetic_scenario(self):
        """Replace demo scheduling rows with the commissioned scenario before timing."""
        scheduler = get_user_model().objects.get(username=DEMO_SCHEDULING_ADMINISTRATOR_USERNAME)
        with (
            patch(
                "schedules.services.operational_attention.organization_today",
                return_value=SYNTHETIC_TIMING_TODAY,
            ),
            patch(
                "schedules.services.work_remaining.organization_today",
                return_value=SYNTHETIC_TIMING_TODAY,
            ),
        ):
            replace_domain(
                mode="synthetic",
                actor=scheduler,
                seed=SYNTHETIC_DEFAULT_SEED,
            )

    def _measure_range(self, label, solve_range, timeout):
        initialize_preceptor_session_range(solve_range.start_date, solve_range.end_date)
        balance_window = SolveDateRange(
            shift_date_months(solve_range.end_date.replace(day=1), -11),
            solve_range.end_date,
        )
        started = perf_counter()
        preflight = preflight_precepting_solve(solve_range, balance_window=balance_window)
        preflight_seconds = perf_counter() - started
        if preflight.blockers:
            raise CommandError(
                f"{label} input is not ready for a solver measurement: "
                + "; ".join(blocker.message for blocker in preflight.blockers)
            )

        try:
            fill_phases = {}
            fill_seconds = self._time_one(
                lambda: produce_fill_proposal(
                    preflight.solver_input,
                    solve_seed="timing-fill",
                    max_time_seconds=timeout,
                    phase_timings=fill_phases,
                )
            )
        except PreceptingSolverTimeout as error:
            fill_seconds = None
            fill_phases = {"bounded failure": str(error)}
        try:
            repair_phases = {}
            repair_seconds = self._time_one(
                lambda: produce_repair_proposal(
                    preflight.solver_input,
                    solve_seed="timing-repair",
                    max_time_seconds=timeout,
                    phase_timings=repair_phases,
                )
            )
        except PreceptingSolverTimeout as error:
            repair_seconds = None
            repair_phases = {"bounded failure": str(error)}
        self.stdout.write(f"{label}:")
        self.stdout.write(f"  preflight/input construction: {preflight_seconds:.3f}s")
        self.stdout.write(f"  Fill optimization: {self._duration(fill_seconds, timeout)}")
        self._write_phases("Fill", fill_phases)
        self.stdout.write(f"  Repair optimization: {self._duration(repair_seconds, timeout)}")
        self._write_phases("Repair", repair_phases)
        self.stdout.write(
            "  end-to-end preflight plus one Fill solve: "
            f"{self._end_to_end_duration(preflight_seconds, fill_seconds, timeout)}"
        )
        self.stdout.write(
            "  end-to-end preflight plus one Repair solve: "
            f"{self._end_to_end_duration(preflight_seconds, repair_seconds, timeout)}"
        )
        return preflight_seconds, fill_seconds, repair_seconds

    def _write_phases(self, mode, phases):
        for phase, value in phases.items():
            duration = f"{value:.3f}s" if isinstance(value, float) else value
            self.stdout.write(f"    {mode} {phase}: {duration}")

    @staticmethod
    def _three_month_target_met(preflight_seconds, fill_seconds, repair_seconds):
        return (
            fill_seconds is not None
            and repair_seconds is not None
            and max(
                preflight_seconds + fill_seconds,
                preflight_seconds + repair_seconds,
            )
            <= 6
        )

    @staticmethod
    def _duration(seconds, timeout):
        return f"{seconds:.3f}s" if seconds is not None else f"timed out at {timeout:.3f}s"

    @staticmethod
    def _end_to_end_duration(preflight_seconds, solve_seconds, timeout):
        return (
            f"{preflight_seconds + solve_seconds:.3f}s"
            if solve_seconds is not None
            else (f">= {preflight_seconds + timeout:.3f}s (solver timed out at {timeout:.3f}s)")
        )

    @staticmethod
    def _time_one(operation):
        started = perf_counter()
        operation()
        return perf_counter() - started
