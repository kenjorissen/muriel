"""Current-record monthly stipend report projections."""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import F, Q

from ..models.late_docs import LateDocAssignment
from ..models.physicians import EffectiveRole, Physician
from ..models.preceptors import PreceptorAssignment
from ..models.stipend_rates import StipendRate
from .stipend_rates import stipend_rates


@dataclass(frozen=True)
class StipendReportDetail:
    date: date
    period: str
    physician: Physician
    hours: Decimal
    hourly_rate: Decimal | None
    amount: Decimal | None


@dataclass(frozen=True)
class StipendReportPhysician:
    physician: Physician
    shift_total: int
    hour_total: Decimal
    amount_total: Decimal | None


@dataclass(frozen=True)
class StipendReportRatePeriod:
    hourly_rate: Decimal
    effective_start: date | None
    effective_end: date | None


@dataclass(frozen=True)
class MonthlyStipendReport:
    kind: str
    month: date
    details: tuple[StipendReportDetail, ...]
    physicians: tuple[StipendReportPhysician, ...]
    rate_periods: tuple[StipendReportRatePeriod, ...]
    missing_rate_dates: tuple[date, ...]
    missing_late_doc_dates: tuple[date, ...]
    shift_total: int
    hour_total: Decimal
    amount_total: Decimal | None

    @property
    def is_incomplete(self):
        return bool(self.missing_rate_dates)


def community_preceptor_stipend_report(month):
    return monthly_stipend_report(StipendRate.Kind.COMMUNITY_PRECEPTOR, month)


def late_doc_stipend_report(month):
    return monthly_stipend_report(StipendRate.Kind.LATE_DOC, month)


def monthly_stipend_report(kind, month):
    """Project one fixed calendar month from current corrected assignments and rates."""
    month = month.replace(day=1)
    next_month = _next_month(month)
    if kind == StipendRate.Kind.COMMUNITY_PRECEPTOR:
        assignments = _community_preceptor_assignments(month, next_month)
        rows = (
            (assignment.physician, assignment.session.date, assignment.session.period_of_day)
            for assignment in assignments
        )
        hours = Decimal("4")
    elif kind == StipendRate.Kind.LATE_DOC:
        assignments = LateDocAssignment.objects.filter(
            date__gte=month, date__lt=next_month
        ).select_related("physician")
        rows = ((assignment.physician, assignment.date, "Late Doc") for assignment in assignments)
        hours = Decimal("1")
    else:
        raise ValueError("Unknown stipend report kind.")

    rates = tuple(stipend_rates(kind))
    details = []
    for physician, shift_date, period in rows:
        hourly_rate = _rate_on(rates, shift_date)
        details.append(
            StipendReportDetail(
                date=shift_date,
                period=period,
                physician=physician,
                hours=hours,
                hourly_rate=hourly_rate,
                amount=hourly_rate * hours if hourly_rate is not None else None,
            )
        )
    details = tuple(sorted(details, key=_detail_sort_key))

    details_by_physician = {}
    for detail in details:
        details_by_physician.setdefault(detail.physician, []).append(detail)
    physicians = tuple(
        _physician_report(physician, physician_details)
        for physician, physician_details in sorted(
            details_by_physician.items(), key=lambda item: _physician_sort_key(item[0])
        )
    )
    missing_rate_dates = tuple(detail.date for detail in details if detail.hourly_rate is None)
    missing_late_doc_dates = (
        _missing_late_doc_dates(month, next_month) if kind == StipendRate.Kind.LATE_DOC else ()
    )
    return MonthlyStipendReport(
        kind=kind,
        month=month,
        details=details,
        physicians=physicians,
        rate_periods=tuple(
            StipendReportRatePeriod(
                hourly_rate=rate.hourly_rate,
                effective_start=rate.effective_start,
                effective_end=rate.effective_end,
            )
            for rate in rates
            if (rate.effective_start is None or rate.effective_start < next_month)
            and (rate.effective_end is None or rate.effective_end >= month)
        ),
        missing_rate_dates=missing_rate_dates,
        missing_late_doc_dates=missing_late_doc_dates,
        shift_total=len(details),
        hour_total=sum((detail.hours for detail in details), Decimal("0")),
        amount_total=(
            None if missing_rate_dates else sum((detail.amount for detail in details), Decimal("0"))
        ),
    )


def _community_preceptor_assignments(month, next_month):
    return (
        PreceptorAssignment.objects.filter(
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            session__date__gte=month,
            session__date__lt=next_month,
            physician__roles__role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            physician__roles__effective_start__lte=F("session__date"),
        )
        .filter(
            Q(physician__roles__effective_end__isnull=True)
            | Q(physician__roles__effective_end__gte=F("session__date"))
        )
        .select_related("physician", "session")
        .order_by("session__date", "session__period_of_day", "pk")
    )


def _physician_report(physician, details):
    has_missing_rate = any(detail.amount is None for detail in details)
    return StipendReportPhysician(
        physician=physician,
        shift_total=len(details),
        hour_total=sum((detail.hours for detail in details), Decimal("0")),
        amount_total=(
            None if has_missing_rate else sum((detail.amount for detail in details), Decimal("0"))
        ),
    )


def _detail_sort_key(detail):
    return (detail.date, detail.period, *_physician_sort_key(detail.physician))


def _physician_sort_key(physician):
    return (physician.display_name.casefold(), physician.pk)


def _rate_on(rates, on_date):
    return next(
        (
            rate.hourly_rate
            for rate in rates
            if (rate.effective_start is None or rate.effective_start <= on_date)
            and (rate.effective_end is None or rate.effective_end >= on_date)
        ),
        None,
    )


def _missing_late_doc_dates(month, next_month):
    assigned_dates = set(
        LateDocAssignment.objects.filter(date__gte=month, date__lt=next_month).values_list(
            "date", flat=True
        )
    )
    return tuple(
        day
        for day in (
            date(month.year, month.month, day_number)
            for day_number in range(1, calendar.monthrange(month.year, month.month)[1] + 1)
        )
        if day.weekday() == 0 and day not in assigned_dates
    )


def _next_month(month):
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


__all__ = (
    "MonthlyStipendReport",
    "StipendReportDetail",
    "StipendReportPhysician",
    "StipendReportRatePeriod",
    "community_preceptor_stipend_report",
    "late_doc_stipend_report",
    "monthly_stipend_report",
)
