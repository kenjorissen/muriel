"""Read-only monthly Faculty work ledgers."""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q

from ..models.availability_categories import CustomAvailabilityCategory
from ..models.physicians import EffectiveRole, FacultyAvailabilityTemplate, Physician
from .operational_attention import organization_today
from .physicians import physician_for_user
from .slot_projection import RequestedSlot, slot_projection

COVERAGE_CHOICES = (
    ("history", "History"),
    ("history-and-scheduled", "History + scheduled"),
    ("scheduled", "Scheduled"),
)

HOURS_PER_PERIOD = Decimal("4")
ACGME_PERCENT = Decimal("0.10")
UMN_PERCENT = Decimal("0.20")

_BUILT_IN_OPTION_ORDER = (
    FacultyAvailabilityTemplate.AvailabilityState.OPEN,
    FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
    FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE,
    FacultyAvailabilityTemplate.AvailabilityState.OFF,
)


def can_view_faculty_admin_time_report(user, *, today=None):
    """Whether a linked physician is a Program Director effective today."""
    physician = physician_for_user(user)
    if not physician:
        return False
    today = organization_today() if today is None else today
    return (
        EffectiveRole.objects.filter(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            effective_start__lte=today,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
        .exists()
    )


@dataclass(frozen=True)
class FacultyWorkOption:
    """A stable schedule-option identity used by every row in one ledger."""

    key: str
    label: str
    category_id: int | None = None


@dataclass(frozen=True)
class FacultyWorkOptionHours:
    """Worked hours credited to one option in one Faculty row."""

    option_key: str
    hours: Decimal


@dataclass(frozen=True)
class FacultyWorkExclusion:
    """All sources that excluded one otherwise explicit scheduled period."""

    on_date: date
    period_of_day: str
    option_key: str
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class FacultyAdminTimeRow:
    """One Faculty physician's auditable monthly work and Admin Time ledger."""

    physician: Physician
    option_hours: tuple[FacultyWorkOptionHours, ...]
    exclusions: tuple[FacultyWorkExclusion, ...]
    not_available_hours: Decimal
    total_worked_hours: Decimal
    potential_admin_hours: Decimal
    precepting_from_admin_hours: Decimal
    computed_admin_hours: Decimal
    acgme_ten_percent: Decimal
    acgme_prorated_cap: Decimal
    acgme_goal: Decimal
    umn_goal: Decimal
    acgme_met: bool
    umn_met: bool

    def hours_for(self, option_key):
        """Return the row's credited hours for one report option identity."""
        return next(
            (item.hours for item in self.option_hours if item.option_key == option_key),
            Decimal("0"),
        )

    @property
    def excluded_hours(self):
        """Each excluded period is removed once, regardless of source count."""
        return HOURS_PER_PERIOD * len(self.exclusions)

    @property
    def patient_care_hours(self):
        return self.hours_for("base:patient_care")

    @property
    def unassigned_primary_eligible_hours(self):
        """Temporary presentation input until the ledger table replaces this column."""
        return self.hours_for("base:open")

    @property
    def explicit_admin_hours(self):
        """Temporary presentation input until the ledger table replaces this column."""
        return self.potential_admin_hours - self.unassigned_primary_eligible_hours


@dataclass(frozen=True)
class MonthlyFacultyAdminTimeReport:
    """A bounded calculation from current facts, never retained ledger rows."""

    month: date
    coverage: str
    as_of_date: date
    included_dates: tuple[date, ...]
    options: tuple[FacultyWorkOption, ...]
    rows: tuple[FacultyAdminTimeRow, ...]

    @property
    def included_weekdays(self):
        return len(self.included_dates)


def monthly_faculty_admin_time_report(month, coverage, as_of_date):
    """Compute an explicit-option work ledger for one Faculty calendar-month slice.

    The shared slot projection remains the sole resolver for effective templates,
    monthly rules, annual windows, transitions, and independent current blockers.
    This service classifies the resulting facts; it does not use final Precepting
    eligibility as a proxy for worked time.
    """
    month = _month_start(month)
    _validate_coverage(coverage)
    if not isinstance(as_of_date, date):
        raise ValidationError({"as_of_date": "Choose an as-of date."})

    admin_category = (
        CustomAvailabilityCategory.objects.filter(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        .only("pk", "name")
        .first()
    )

    included_dates = _included_weekdays(month, coverage, as_of_date)
    if not included_dates:
        return MonthlyFacultyAdminTimeReport(
            month, coverage, as_of_date, (), _built_in_options(admin_category), ()
        )

    physicians = tuple(
        sorted(
            Physician.objects.filter(
                roles__role=EffectiveRole.Role.FACULTY,
                roles__effective_start__lte=included_dates[-1],
            )
            .filter(
                Q(roles__effective_end__isnull=True)
                | Q(roles__effective_end__gte=included_dates[0])
            )
            .distinct(),
            key=lambda physician: (physician.display_name.casefold(), physician.pk),
        )
    )
    if not physicians:
        return MonthlyFacultyAdminTimeReport(
            month, coverage, as_of_date, included_dates, _built_in_options(admin_category), ()
        )

    slots = tuple(
        RequestedSlot(on_date, period) for on_date in included_dates for period in ("AM", "PM")
    )
    calculations = {physician.pk: _Calculation() for physician in physicians}
    admin_category_id = admin_category.pk if admin_category else None
    custom_options = {}

    for projection in slot_projection(slots, physicians=physicians, include_holiday_closures=True):
        if not any(role.role == EffectiveRole.Role.FACULTY for role in projection.effective_roles):
            continue
        availability = projection.tier_zero
        selection = (
            availability.availability_selection
            or FacultyAvailabilityTemplate.base_selection(
                FacultyAvailabilityTemplate.AvailabilityState.OFF
            )
        )
        option = _option_for_selection(selection)
        if option.category_id is not None:
            # The selection snapshot is authoritative: an inactive category, or
            # one renamed after a template was recorded, still identifies these hours.
            if option.category_id != admin_category_id:
                custom_options.setdefault(option.key, option)

        calculation = calculations[projection.physician.pk]
        if option.key == "base:off":
            # Explicit unavailability is visible but is never gross capacity
            # and therefore cannot be an independently removed work period.
            calculation.not_available_hours += HOURS_PER_PERIOD
            continue
        exclusion_labels = tuple(
            dict.fromkeys(blocker.label for blocker in projection.independent_blockers)
        )
        if exclusion_labels:
            calculation.exclusions.append(
                FacultyWorkExclusion(
                    projection.slot.on_date,
                    projection.slot.period_of_day,
                    option.key,
                    tuple(exclusion_labels),
                )
            )
            continue

        calculation.option_hours[option.key] += HOURS_PER_PERIOD
        calculation.total_worked_hours += HOURS_PER_PERIOD
        if option.key == "base:open" or option.category_id == admin_category_id:
            calculation.potential_admin_hours += HOURS_PER_PERIOD
            if projection.preceptor_assignment is not None:
                calculation.precepting_from_admin_hours += HOURS_PER_PERIOD

    options = (
        *_built_in_options(admin_category),
        *sorted(custom_options.values(), key=lambda option: option.category_id),
    )
    cap = HOURS_PER_PERIOD * Decimal(len(included_dates)) / Decimal("5")
    report_rows = []
    for physician in physicians:
        calculation = calculations[physician.pk]
        computed_admin_hours = (
            calculation.potential_admin_hours - calculation.precepting_from_admin_hours
        )
        acgme_ten_percent = calculation.total_worked_hours * ACGME_PERCENT
        acgme_goal = min(acgme_ten_percent, cap)
        umn_goal = calculation.total_worked_hours * UMN_PERCENT
        report_rows.append(
            FacultyAdminTimeRow(
                physician=physician,
                option_hours=tuple(
                    FacultyWorkOptionHours(option.key, calculation.option_hours[option.key])
                    for option in options
                ),
                exclusions=tuple(calculation.exclusions),
                not_available_hours=calculation.not_available_hours,
                total_worked_hours=calculation.total_worked_hours,
                potential_admin_hours=calculation.potential_admin_hours,
                precepting_from_admin_hours=calculation.precepting_from_admin_hours,
                computed_admin_hours=computed_admin_hours,
                acgme_ten_percent=acgme_ten_percent,
                acgme_prorated_cap=cap,
                acgme_goal=acgme_goal,
                umn_goal=umn_goal,
                acgme_met=computed_admin_hours >= acgme_goal,
                umn_met=computed_admin_hours >= umn_goal,
            )
        )
    return MonthlyFacultyAdminTimeReport(
        month, coverage, as_of_date, included_dates, tuple(options), tuple(report_rows)
    )


@dataclass
class _Calculation:
    option_hours: defaultdict = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    exclusions: list[FacultyWorkExclusion] = field(default_factory=list)
    not_available_hours: Decimal = Decimal("0")
    total_worked_hours: Decimal = Decimal("0")
    potential_admin_hours: Decimal = Decimal("0")
    precepting_from_admin_hours: Decimal = Decimal("0")


def _built_in_options(admin_category):
    options = tuple(
        FacultyWorkOption(
            f"base:{state}", FacultyAvailabilityTemplate.AvailabilityState(state).label
        )
        for state in _BUILT_IN_OPTION_ORDER
    )
    if admin_category:
        return (
            options[0],
            FacultyWorkOption(
                f"category:{admin_category.pk}", admin_category.name, admin_category.pk
            ),
            *options[1:],
        )
    return options


def _option_for_selection(selection):
    if "base_state" in selection:
        state = selection["base_state"]
        return FacultyWorkOption(
            f"base:{state}", FacultyAvailabilityTemplate.AvailabilityState(state).label
        )
    return FacultyWorkOption(
        f"category:{selection['category_id']}", selection["category_name"], selection["category_id"]
    )


def _month_start(value):
    if not isinstance(value, date):
        raise ValidationError({"month": "Choose a calendar month."})
    return value.replace(day=1)


def _validate_coverage(coverage):
    if coverage not in dict(COVERAGE_CHOICES):
        raise ValidationError("Choose History, Scheduled, or History + scheduled coverage.")


def _included_weekdays(month, coverage, as_of_date):
    last_day = calendar.monthrange(month.year, month.month)[1]
    days = tuple(
        date(month.year, month.month, day_number)
        for day_number in range(1, last_day + 1)
        if date(month.year, month.month, day_number).weekday() < 5
    )
    if coverage == "history":
        return tuple(day for day in days if day < as_of_date)
    if coverage == "scheduled":
        return tuple(day for day in days if day >= as_of_date)
    return days


__all__ = (
    "ACGME_PERCENT",
    "COVERAGE_CHOICES",
    "HOURS_PER_PERIOD",
    "UMN_PERCENT",
    "FacultyAdminTimeRow",
    "FacultyWorkExclusion",
    "FacultyWorkOption",
    "FacultyWorkOptionHours",
    "MonthlyFacultyAdminTimeReport",
    "can_view_faculty_admin_time_report",
    "monthly_faculty_admin_time_report",
)
