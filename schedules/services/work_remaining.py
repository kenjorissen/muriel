"""Derived work remaining values shared by workspace summaries and global views."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .operational_attention import organization_today

WorkReasonClassification = Literal["required", "advisory"]
WorkSummaryTone = Literal["danger", "attention", "clear", "neutral"]
WorkProjectionPolicy = Literal["current-attention", "focused-range", "retained-history"]

# These domains have no accepted completeness schedule, so they intentionally do
# not contribute to the initial organization-wide projection.
NON_PRODUCING_WORK_AREAS = (
    "Vacation entry",
    "Community Precepting recurrence",
    "Casual Rounding rules",
    "Physician setup",
    "Report and rate administration",
)


@dataclass(frozen=True)
class InclusiveDateRange:
    """A calendar-date range including both endpoints."""

    start: date
    end: date

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("An inclusive date range cannot end before it starts.")

    def intersects(self, other):
        return self.start <= other.end and other.start <= self.end


@dataclass(frozen=True)
class WorkSummaryType:
    """Stable presentation metadata for one aggregate required-work type."""

    key: str
    singular_label: str
    plural_label: str

    def __post_init__(self):
        if not all((self.key, self.singular_label, self.plural_label)):
            raise ValueError("A work summary type needs an identity and both display labels.")


@dataclass(frozen=True)
class WorkSummaryItem:
    """One counted, grammatically complete required-work summary phrase."""

    key: str
    count: int
    singular_label: str
    plural_label: str

    @property
    def label(self):
        return self.singular_label if self.count == 1 else self.plural_label


@dataclass(frozen=True)
class WorkRemainingRangeAction:
    """An optional range-level action rendered beside a scoped work summary heading."""

    label: str
    url: str


@dataclass(frozen=True)
class GlobalWorkBucket:
    """Stable presentation details for related global work rows."""

    key: str
    label: str
    affected_range: InclusiveDateRange
    target_url: str


@dataclass(frozen=True)
class ProjectedWorkReason:
    """One domain-provided explanation for unresolved work."""

    classification: WorkReasonClassification
    label: str
    quantity: int
    summary_type: WorkSummaryType | None = None

    def __post_init__(self):
        if self.classification not in {"required", "advisory"}:
            raise ValueError("A work reason must be required or advisory.")
        if (
            not isinstance(self.quantity, int)
            or isinstance(self.quantity, bool)
            or self.quantity <= 0
        ):
            raise ValueError("A work reason needs a positive actionable quantity.")


@dataclass(frozen=True)
class ProjectedWorkItem:
    """An immutable, domain-neutral description of unresolved work."""

    key: str
    area: str
    label: str
    affected_range: InclusiveDateRange
    matters_on: date
    target_url: str
    reasons: tuple[ProjectedWorkReason, ...]
    actionable_dates: tuple[date, ...] = ()
    global_bucket: GlobalWorkBucket | None = None

    def __post_init__(self):
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "actionable_dates", tuple(self.actionable_dates))
        if not self.reasons:
            raise ValueError("A projected work item needs at least one reason.")

    @property
    def required(self):
        return any(reason.classification == "required" for reason in self.reasons)


@dataclass(frozen=True)
class ActionableWorkProjection:
    """The shared membership and rollups for one date scope and as-of date."""

    selected_range: InclusiveDateRange | None
    as_of: date
    policy: WorkProjectionPolicy
    items: tuple[ProjectedWorkItem, ...]
    required_item_count: int
    advisory_item_count: int
    required_summary_items: tuple[WorkSummaryItem, ...]

    @property
    def tone(self):
        if self.required_item_count:
            return "danger"
        if self.advisory_item_count:
            return "attention"
        return "clear"


@dataclass(frozen=True)
class ScopedWorkRemaining:
    """A work summary for one explicitly selected range."""

    selected_range: InclusiveDateRange
    evaluated: bool
    items: tuple[ProjectedWorkItem, ...]
    required_item_count: int
    advisory_item_count: int
    required_summary_items: tuple[WorkSummaryItem, ...]
    tone: WorkSummaryTone
    prose: str
    range_action: WorkRemainingRangeAction | None = None


@dataclass(frozen=True)
class GlobalWorkRemaining:
    """The organization-wide actionable projection and its shared summary."""

    items: tuple[ProjectedWorkItem, ...]
    required_item_count: int
    advisory_item_count: int
    tone: WorkSummaryTone
    prose: str


def project_actionable_work(items, selected_range, *, as_of, policy):
    """Project unresolved facts into one scope using a named attention policy.

    ``current-attention`` excludes facts that have not reached ``matters_on`` or
    whose affected period is historical. ``retained-history`` preserves such
    facts for correction views while still applying the requested date scope.
    ``focused-range`` includes only the current or future part of an explicit
    selected range, without applying the attention cutoff.
    """
    if policy not in {"current-attention", "focused-range", "retained-history"}:
        raise ValueError(
            "A work projection policy must be current-attention, focused-range, or retained-history."
        )
    if policy == "focused-range" and selected_range is None:
        raise ValueError("A focused-range work projection needs an explicit selected range.")

    projected_items = tuple(
        sorted(
            (
                item
                for item in items
                if _intersects_scope(item, selected_range)
                and (
                    policy == "retained-history"
                    or (
                        policy == "focused-range"
                        and focused_range_has_membership(
                            item.affected_range,
                            selected_range,
                            actionable_dates=item.actionable_dates,
                            as_of=as_of,
                        )
                    )
                    or (
                        policy == "current-attention"
                        and item.matters_on <= as_of
                        and item.affected_range.end >= as_of
                    )
                )
            ),
            key=(
                _current_attention_sort_key
                if policy == "current-attention"
                else _retained_history_sort_key
            ),
        )
    )
    return ActionableWorkProjection(
        selected_range=selected_range,
        as_of=as_of,
        policy=policy,
        items=projected_items,
        required_item_count=_reason_quantity(projected_items, "required"),
        advisory_item_count=_reason_quantity(projected_items, "advisory"),
        required_summary_items=_complete_required_summary_items(projected_items),
    )


def scoped_work_remaining(items, selected_range, *, evaluated, range_action=None):
    """Summarize retained work intersecting a caller-selected date range."""
    projection = project_actionable_work(
        items,
        selected_range,
        as_of=organization_today(),
        policy="retained-history",
    )
    required_item_count = projection.required_item_count
    advisory_item_count = projection.advisory_item_count
    required_summary_items = _required_summary_items(projection.items)
    if not evaluated:
        tone = "neutral"
    else:
        tone = projection.tone
    return ScopedWorkRemaining(
        selected_range=selected_range,
        evaluated=evaluated,
        items=projection.items,
        required_item_count=required_item_count,
        advisory_item_count=advisory_item_count,
        required_summary_items=required_summary_items,
        tone=tone,
        prose=_summary_prose(
            required_item_count,
            advisory_item_count,
            evaluated,
            required_summary_items=required_summary_items,
        ),
        range_action=range_action,
    )


def focused_work_remaining(items, selected_range, *, range_action=None):
    """Summarize current or future unresolved work in an explicit workspace range."""
    as_of = organization_today()
    projection = project_actionable_work(
        items,
        selected_range,
        as_of=as_of,
        policy="focused-range",
    )
    evaluated = selected_range.end >= as_of
    return ScopedWorkRemaining(
        selected_range=selected_range,
        evaluated=evaluated,
        items=projection.items,
        required_item_count=projection.required_item_count,
        advisory_item_count=projection.advisory_item_count,
        required_summary_items=projection.required_summary_items,
        tone=projection.tone if evaluated else "neutral",
        prose=_summary_prose(
            projection.required_item_count,
            projection.advisory_item_count,
            evaluated,
            required_summary_items=projection.required_summary_items,
        ),
        range_action=range_action,
    )


def shift_date_months(value, offset):
    """Move a calendar date by whole months, clamping to the target month's end."""
    month_number = value.month - 1 + offset
    year, month = divmod(month_number, 12)
    year += value.year
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _lead_time_horizon_end(today, months):
    """Return the last possible anchor date that can matter today."""
    return shift_date_months(today.replace(day=1), months + 1) - timedelta(days=1)


def global_work_items(items):
    """Return work that has begun to matter and is not historical today."""
    return project_actionable_work(
        items,
        None,
        as_of=organization_today(),
        policy="current-attention",
    ).items


def global_work_remaining():
    """Aggregate every initial producer into the actionable organization-wide view."""
    projection = project_actionable_work(
        _global_projected_work_items(),
        None,
        as_of=organization_today(),
        policy="current-attention",
    )
    items = _group_global_work_items(projection.items)
    required_item_count = projection.required_item_count
    advisory_item_count = projection.advisory_item_count
    return GlobalWorkRemaining(
        items=items,
        required_item_count=required_item_count,
        advisory_item_count=advisory_item_count,
        tone=projection.tone,
        prose=(
            _summary_prose(required_item_count, advisory_item_count, evaluated=True)
            if items
            else "No work remains."
        ),
    )


def _global_projected_work_items():
    """Read the bounded horizons that can currently contribute actionable work."""
    # Imports remain local because every producer imports the shared item types above.
    from ..academic_years import academic_year_for_date
    from ..models.academic_events import AcademicEvent
    from ..models.holidays import HolidayOccurrence
    from .academic_events import academic_event_projected_work_items
    from .holidays import holiday_occurrences_for, holiday_projected_work_items
    from .interviews import interview_projected_work_items
    from .lead_times import lead_time_policy
    from .preceptors import (
        ensure_precepting_horizon_sessions,
        precepting_projected_work_items,
        work_week_start,
    )
    from .rounding import normal_rounding_projected_work_items

    today = organization_today()
    academic_year = academic_year_for_date(today)
    policy = lead_time_policy()
    precepting_horizon_end = shift_date_months(today, policy["precepting"])
    normal_rounding_horizon_end = _lead_time_horizon_end(today, policy["normal_rounding"])
    week_start = work_week_start(today)
    week_end = work_week_start(normal_rounding_horizon_end)
    planning_weeks = tuple(
        week_start + timedelta(days=7 * offset)
        for offset in range(((week_end - week_start).days // 7) + 1)
    )
    precepting_week_end = work_week_start(precepting_horizon_end)
    precepting_weeks = tuple(
        week_start + timedelta(days=7 * offset)
        for offset in range(((precepting_week_end - week_start).days // 7) + 1)
    )
    ensure_precepting_horizon_sessions(precepting_weeks)
    holiday_end = _lead_time_horizon_end(today, policy["holidays"])
    for calendar_year in range(today.year, holiday_end.year + 1):
        holiday_occurrences_for(calendar_year)
    holidays = HolidayOccurrence.objects.filter(
        calendar_year__range=(today.year, holiday_end.year)
    ).order_by("nominal_date", "pk")
    event_academic_years = range(
        academic_year,
        academic_year_for_date(_lead_time_horizon_end(today, policy["academic_events"])) + 1,
    )
    events = AcademicEvent.objects.filter(academic_year__in=event_academic_years).prefetch_related(
        "participants"
    )
    interview_academic_years = range(
        academic_year,
        academic_year_for_date(_lead_time_horizon_end(today, policy["interviews"])) + 1,
    )
    return (
        *holiday_projected_work_items(holidays),
        *(
            item
            for year in event_academic_years
            for item in academic_event_projected_work_items(
                year, tuple(event for event in events if event.academic_year == year)
            )
        ),
        *precepting_projected_work_items(precepting_weeks),
        *normal_rounding_projected_work_items(planning_weeks),
        *interview_projected_work_items(interview_academic_years),
    )


def _reason_quantity(items, classification):
    return sum(
        reason.quantity
        for item in items
        for reason in item.reasons
        if reason.classification == classification
    )


def _group_global_work_items(items):
    """Collapse producer-marked items only in the organization-wide projection."""
    grouped_items = {}
    ungrouped_items = []
    for item in items:
        if item.global_bucket is None:
            ungrouped_items.append(item)
            continue
        grouped_items.setdefault(item.global_bucket, []).append(item)

    return tuple(
        sorted(
            (
                *ungrouped_items,
                *(
                    ProjectedWorkItem(
                        key=bucket.key,
                        area="academic-events",
                        label=bucket.label,
                        affected_range=bucket.affected_range,
                        matters_on=min(item.matters_on for item in bucket_items),
                        target_url=bucket.target_url,
                        reasons=_global_bucket_reasons(bucket.label, bucket_items),
                    )
                    for bucket, bucket_items in grouped_items.items()
                ),
            ),
            key=_current_attention_sort_key,
        )
    )


def _global_bucket_reasons(label, items):
    quantities = {
        classification: _reason_quantity(items, classification)
        for classification in ("required", "advisory")
    }
    return tuple(
        ProjectedWorkReason(
            classification,
            f"{label}: {_item_clause(quantities[classification], classification)}",
            quantities[classification],
        )
        for classification in ("required", "advisory")
        if quantities[classification]
    )


def _intersects_scope(item, selected_range):
    """Keep facts visible for either their affected span or explicit action dates."""
    if selected_range is None:
        return True
    return item.affected_range.intersects(selected_range) or any(
        selected_range.start <= actionable_date <= selected_range.end
        for actionable_date in item.actionable_dates
    )


def focused_range_has_membership(
    affected_range, selected_range, *, actionable_dates=(), as_of=None
):
    """Whether a domain fact has current-or-future membership in a focused range."""
    as_of = organization_today() if as_of is None else as_of
    affected_start = max(affected_range.start, selected_range.start, as_of)
    affected_end = min(affected_range.end, selected_range.end)
    return affected_start <= affected_end or any(
        max(selected_range.start, as_of) <= actionable_date <= selected_range.end
        for actionable_date in actionable_dates
    )


def _current_attention_sort_key(item):
    return (item.matters_on, item.area, item.key)


def _retained_history_sort_key(item):
    return (
        not item.required,
        item.affected_range.start,
        item.affected_range.end,
        item.area,
        item.key,
    )


def _required_summary_items(items):
    """Aggregate supplied required-work types, preserving the projection's order."""
    reasons = tuple(
        reason for item in items for reason in item.reasons if reason.classification == "required"
    )
    if not any(reason.summary_type is not None for reason in reasons):
        return ()
    if any(reason.summary_type is None for reason in reasons):
        raise ValueError("Typed required work must classify every required action.")

    counts = {}
    types = {}
    for reason in reasons:
        summary_type = reason.summary_type
        if summary_type.key in types and types[summary_type.key] != summary_type:
            raise ValueError("A work summary type identity needs consistent display labels.")
        types.setdefault(summary_type.key, summary_type)
        counts[summary_type.key] = counts.get(summary_type.key, 0) + reason.quantity
    return tuple(
        WorkSummaryItem(
            key=key,
            count=count,
            singular_label=types[key].singular_label,
            plural_label=types[key].plural_label,
        )
        for key, count in counts.items()
    )


def _complete_required_summary_items(items):
    """Return typed rollups only when the complete projection can use them."""
    reasons = tuple(
        reason for item in items for reason in item.reasons if reason.classification == "required"
    )
    if any(reason.summary_type is None for reason in reasons):
        return ()
    return _required_summary_items(items)


def _summary_prose(
    required_item_count, advisory_item_count, evaluated, *, required_summary_items=()
):
    clauses = []
    if required_item_count:
        if required_summary_items:
            clauses.append(_typed_required_clause(required_item_count, required_summary_items))
        else:
            clauses.append(_item_clause(required_item_count, "required"))
    if advisory_item_count:
        clauses.append(_item_clause(advisory_item_count, "advisory"))
    if clauses:
        return " ".join(clauses)
    if evaluated:
        return "No work remains in this range."
    return "Work remaining is not evaluated for this range."


# TEXT-CONTRACT: work-remaining-count-grammar
# Reason: aggregate and typed Work Remaining counts must read grammatically.
# Scope: ``action/actions`` and ``remains/remain``, plus each typed singular/plural label,
# agree with its count; punctuation may vary.
# Protecting tests: schedules/tests/test_work_remaining.py::ScopedWorkRemainingTests and
# schedules/tests/test_projected_precepting_work.py::ProjectedPreceptingWorkTests.
def _item_clause(count, classification):
    noun = "action" if count == 1 else "actions"
    verb = "remains" if count == 1 else "remain"
    return f"{count} {classification} {noun} {verb}."


# TEXT-CONTRACT: work-remaining-count-grammar
def _typed_required_clause(required_item_count, summary_items):
    noun = "action" if required_item_count == 1 else "actions"
    verb = "remains" if required_item_count == 1 else "remain"
    phrases = tuple(f"{item.count} {item.label}" for item in summary_items)
    return f"{required_item_count} required {noun} {verb}: {_join_phrases(phrases)}."


def _join_phrases(phrases):
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return " and ".join(phrases)
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"
