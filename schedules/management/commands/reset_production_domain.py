"""Replace production scheduling data without changing login-owned records."""

from datetime import date
from time import perf_counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from schedules.management.commands.reset_demo import seed_demo_data
from schedules.management.production_database import validate_production_database
from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    AcademicEventMonthlyRuleChange,
    CasualRounderRule,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    HolidayOccurrence,
    HolidayOccurrenceChange,
    InterviewShift,
    InterviewShiftChange,
    LateDocAssignment,
    LateDocAssignmentChange,
    MonthlyPhysicianAvailabilityRule,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptingSolverLateDocReview,
    PreceptingSolverProposal,
    PreceptingSolverProposalComparison,
    PreceptingSolverProposalEdit,
    PreceptingSolverProposalReconciliation,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulePeriod,
    SchedulingMutationAuthority,
    StipendRate,
    StipendRateChange,
    Vacation,
    VacationChange,
)
from schedules.services.availability_categories import ensure_system_schedule_options
from schedules.services.lead_times import lead_time_months
from schedules.services.preceptors import work_week_start
from schedules.services.synthetic_scenario import BASELINE_START, SyntheticScenario
from schedules.services.work_remaining import shift_date_months

DOMAIN_MODELS = (
    FindingAcknowledgement,
    PreceptingSolverProposalReconciliation,
    PreceptingSolverProposalComparison,
    PreceptingSolverLateDocReview,
    PreceptingSolverProposalEdit,
    PreceptingSolverProposal,
    LateDocAssignmentChange,
    LateDocAssignment,
    StipendRateChange,
    StipendRate,
    HolidayOccurrenceChange,
    HolidayOccurrence,
    AcademicEventChange,
    AcademicEventMonthlyRuleChange,
    AcademicEventMonthlyRule,
    AcademicEventDefinitionChange,
    CommunityPreceptorScheduleChange,
    CustomAvailabilityCategoryChange,
    InterviewShiftChange,
    PreceptorAssignmentChange,
    RoundingScheduleChange,
    VacationChange,
    PhysicianChange,
    PreceptorAssignment,
    AcademicEvent,
    CommunityPreceptorRule,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategory,
    InterviewShift,
    CasualRounderRule,
    NormalRounder,
    MonthlyPhysicianAvailabilityRule,
    FacultyAvailabilityTemplate,
    EffectiveRole,
    PreceptorSession,
    SchedulePeriod,
    AcademicEventDefinition,
    SchedulingMutationAuthority,
    Vacation,
    Physician,
)

SYNTHETIC_DEFAULT_SEED = "muriel-synthetic-v1"


def _select_actor(username):
    user_model = get_user_model()
    candidates = user_model.objects.filter(is_active=True, is_superuser=True).order_by(
        user_model.USERNAME_FIELD
    )
    if username:
        try:
            return candidates.get(**{user_model.USERNAME_FIELD: username})
        except user_model.DoesNotExist as error:
            raise CommandError("--actor must name an existing active superuser.") from error
    if candidates.count() != 1:
        raise CommandError(
            "This reset requires exactly one active superuser; select one explicitly with --actor USERNAME."
        )
    return candidates.get()


def _synthetic_summary(*, today):
    completion_end = _month_end(today)
    horizon_end = (
        work_week_start(shift_date_months(today, lead_time_months("precepting")))
        + date.resolution * 4
    )
    counts = {
        "physicians": Physician.objects.count(),
        "sessions": PreceptorSession.objects.count(),
        "assignments": PreceptorAssignment.objects.count(),
        "assignment changes": PreceptorAssignmentChange.objects.count(),
        "Late Doc assignments": LateDocAssignment.objects.count(),
        "Late Doc changes": LateDocAssignmentChange.objects.count(),
    }
    historical_assignments = PreceptorAssignment.objects.filter(
        session__date__gte=BASELINE_START,
        session__date__lte=completion_end,
    ).count()
    if (
        not historical_assignments
        or not PreceptorAssignmentChange.objects.filter(
            session__date__range=(BASELINE_START, completion_end)
        ).exists()
        or not LateDocAssignment.objects.filter(
            date__range=(BASELINE_START, completion_end)
        ).exists()
        or not LateDocAssignmentChange.objects.filter(
            date__range=(BASELINE_START, completion_end)
        ).exists()
    ):
        raise CommandError(
            "Synthetic historical validation failed: expected assignments and ordinary change history."
        )
    if (
        PreceptorAssignment.objects.filter(
            session__date__gt=completion_end, session__date__lte=horizon_end
        ).exists()
        or LateDocAssignment.objects.filter(date__gt=completion_end, date__lte=horizon_end).exists()
    ):
        raise CommandError(
            "Synthetic future Precepting validation failed: later work is not empty."
        )
    return counts, historical_assignments, completion_end


def _month_end(value):
    return (value.replace(day=28) + date.resolution * 4).replace(day=1) - date.resolution


def _database_size():
    if connection.vendor == "sqlite":
        return None
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            return cursor.fetchone()[0]
    return None


def replace_domain(*, mode, actor=None, seed=SYNTHETIC_DEFAULT_SEED):
    """Delete every scheduling-domain row in one transaction, then populate the requested mode."""
    with transaction.atomic():
        for model in DOMAIN_MODELS:
            model.objects.all().delete()
        ensure_system_schedule_options()
        if mode == "demo":
            seed_demo_data(scheduler=actor, scheduling_administrator=actor)
        elif mode == "synthetic":
            from schedules.services.operational_attention import organization_today

            today = organization_today()
            scenario = SyntheticScenario(actor=actor, seed=seed)
            baseline = scenario.establish_baseline()
            scenario.generate_source_schedule_history(baseline, today=today)
            scenario.generate_precepting_history(baseline, today=today)
            return _synthetic_summary(today=today)
    return None


class Command(BaseCommand):
    help = "Replace only the production physician and scheduling domain with empty or demo data."

    def add_arguments(self, parser):
        parser.add_argument("mode", choices=("demo", "empty", "synthetic"))
        parser.add_argument("--actor", metavar="USERNAME")
        parser.add_argument("--seed", default=SYNTHETIC_DEFAULT_SEED)

    def handle(self, *args, **options):
        validate_production_database("domain reset")
        mode = options["mode"]
        if mode != "synthetic" and options["seed"] != SYNTHETIC_DEFAULT_SEED:
            raise CommandError("--seed is valid only in synthetic mode.")
        actor = _select_actor(options["actor"]) if mode in {"demo", "synthetic"} else None
        started = perf_counter()
        synthetic_summary = replace_domain(
            mode=mode,
            actor=actor,
            seed=options["seed"],
        )
        if mode == "synthetic":
            if synthetic_summary is None:
                raise CommandError("Synthetic reset did not produce a validation summary.")
            counts, historical_assignments, completion_end = synthetic_summary
            self.stdout.write(
                "Synthetic scenario: "
                + ", ".join(f"{label}={count}" for label, count in counts.items())
            )
            self.stdout.write(
                f"Historical validation: {historical_assignments} assignments through "
                f"{completion_end:%Y-%m}."
            )
            self.stdout.write(
                f"Work remaining: Precepting is complete through {completion_end:%Y-%m}; "
                "later attention-visible work remains open."
            )
            size = _database_size()
            if size is not None:
                self.stdout.write(f"Database size: {size} bytes.")
            self.stdout.write(f"Elapsed: {perf_counter() - started:.1f}s.")
        self.stdout.write(
            self.style.SUCCESS(f"Production scheduling domain replaced with {mode} data.")
        )
