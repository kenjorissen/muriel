"""Explicit, application-owned values for the shared calendar lattice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .services.holidays import HolidayProjection


@dataclass(frozen=True)
class CalendarAction:
    url: str
    label: str
    text: str = "+"
    identifier: str = ""
    class_name: str = ""


@dataclass(frozen=True)
class CalendarStatusPresentation:
    state: str
    symbol: str
    name: str
    explanation: str
    class_name: str = ""


@dataclass(frozen=True)
class CalendarDay:
    iso_date: str
    label: str
    in_selected_month: bool
    action: CalendarAction | None = None
    holidays: tuple[HolidayProjection, ...] = ()


@dataclass(frozen=True)
class CalendarItem:
    """A positioned domain partial. Its position is always owned by the lattice."""

    column: int
    span: int
    partial: str
    content: object
    status: str = ""


@dataclass(frozen=True)
class CalendarLane:
    label: str
    items: tuple[CalendarItem, ...]
    status: str = ""
    variant: str = ""
    period: str = ""
    holiday_items: bool = False
    holiday_columns: tuple[int, ...] = ()


@dataclass(frozen=True)
class CalendarWeek:
    label: str
    days: tuple[CalendarDay, ...]
    lanes: tuple[CalendarLane, ...]
    action: CalendarAction | None = None
    status: CalendarStatusPresentation | None = None
    finding: CalendarFindingPresentation | None = None
    finding_url: str = ""
    required_work_count: int = 0
    target: str = ""

    @cached_property
    def calendar_days(self):
        from .services.holidays import holiday_projections_for_dates

        holidays_by_date = {}
        for holiday in holiday_projections_for_dates(
            date.fromisoformat(day.iso_date) for day in self.days
        ):
            holidays_by_date.setdefault(holiday.observed_date, []).append(holiday)
        return tuple(
            replace(day, holidays=tuple(holidays_by_date.get(date.fromisoformat(day.iso_date), ())))
            for day in self.days
        )

    @cached_property
    def calendar_lanes(self):
        """Project holidays into the existing date cells and opted-in range lanes."""
        projected_lanes = []
        for lane in self.lanes:
            holiday_columns = tuple(
                index
                for index, day in enumerate(self.calendar_days, start=1)
                if day.holidays
                and (
                    not lane.period or any(holiday.closes(lane.period) for holiday in day.holidays)
                )
            )
            items = lane.items
            if lane.holiday_items:
                items = (
                    tuple(
                        CalendarItem(
                            index,
                            1,
                            "schedules/calendar_holiday_item.html",
                            (holiday,),
                            status="holiday",
                        )
                        for index, day in enumerate(self.calendar_days, start=1)
                        for holiday in day.holidays
                    )
                    + items
                )
            projected_lanes.append(replace(lane, items=items, holiday_columns=holiday_columns))
        return tuple(projected_lanes)


@dataclass(frozen=True)
class CalendarLattice:
    label: str
    columns: tuple[str, ...]
    weeks: tuple[CalendarWeek, ...]
    variant: str
    has_gutter: bool
    surface_classes: str = ""
    legend: tuple[CalendarStatusPresentation, ...] = ()
    finding_legend: tuple[CalendarStatusPresentation, ...] = ()
    precepting_legend: tuple[CalendarStatusPresentation, ...] = ()
    header_variant: str = "two-level"
    header_days: tuple[CalendarDay, ...] = ()
    header_status: CalendarStatusPresentation | None = None
    header_required_work_count: int = 0
    header_finding: CalendarFindingPresentation | None = None
    holiday_header_labels: bool = True

    @cached_property
    def calendar_header_days(self):
        if not self.header_days:
            return ()
        header_week = CalendarWeek("calendar header", self.header_days, ())
        return header_week.calendar_days


@dataclass(frozen=True)
class AssignmentSlotPresentation:
    display: str
    state: str
    accessible_label: str
    url: str = ""
    action_label: str = ""
    lock_label: str = ""
    changed: bool = False
    target: str = ""
    delta_label: str = "Changed"
    finding: CalendarFindingPresentation | None = None
    finding_tone: str = ""


def finding_tone_from_counts(hard_count, preference_count):
    """Return the shared visual precedence for a finding collection."""
    if hard_count:
        return "hard"
    if preference_count:
        return "preference"
    return ""


def finding_tone(findings):
    """Select the visible tone without changing an item's structural state."""
    severities = tuple(
        finding["severity"] if isinstance(finding, dict) else finding.severity
        for finding in findings
    )
    hard_count = severities.count("hard")
    return finding_tone_from_counts(hard_count, len(severities) - hard_count)


def slot_state_for_target(findings_by_target, target):
    """Return the compact calendar state for findings attributable to one slot."""
    findings = findings_by_target.get(target, ())
    tone = finding_tone(findings)
    if tone == "hard":
        return "conflict"
    if tone == "preference":
        return "preference"
    return "assigned"


@dataclass(frozen=True)
class FindingPresentation:
    message: str
    severity: str
    links: tuple[FindingLinkPresentation, ...] = ()
    physicians: tuple[FindingLinkPresentation, ...] = ()
    source_links: tuple[FindingLinkPresentation, ...] = ()


@dataclass(frozen=True)
class CalendarFindingPresentation:
    """A compact, source-free finding summary for the scheduling calendar."""

    severity: str
    symbol: str
    summary: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class FindingLinkPresentation:
    label: str
    url: str


@dataclass(frozen=True)
class AssignmentPresentation:
    period: str
    date_label: str
    slots: tuple[AssignmentSlotPresentation, ...]
    faculty_coverage_conflict: bool
    findings: tuple[FindingPresentation, ...] = ()
    compact_findings: bool = False
    session_finding: CalendarFindingPresentation | None = None
    session_finding_url: str = ""


@dataclass(frozen=True)
class VacationPresentation:
    physician_name: str
    coverage_label: str
    accessible_label: str
    url: str


@dataclass(frozen=True)
class AcademicEventPresentation:
    title: str
    coverage_label: str
    assigned_count: int
    required_count: int
    all_available_faculty: bool
    needs_participants: bool
    assignment_status: str
    url: str
    participant_detail: str = ""


@dataclass(frozen=True)
class InterviewFindingPresentation:
    message: str
    severity: str


@dataclass(frozen=True)
class InterviewPresentation:
    physician_name: str
    findings: tuple[InterviewFindingPresentation, ...]
    attention: bool
    url: str
    assignment_detail: str = ""
    finding_tone: str = ""
    closed_label: str = ""
    accessible_label: str = ""


@dataclass(frozen=True)
class CommunityOccurrencePresentation:
    physician_name: str
    period: str
    url: str


@dataclass(frozen=True)
class CommunityPresentation:
    occurrences: tuple[CommunityOccurrencePresentation, ...]
    closed_label: str = ""
    accessible_label: str = ""


@dataclass(frozen=True)
class RoundingNormalPresentation:
    title: str
    detail: str
    status: str
    url: str


@dataclass(frozen=True)
class RoundingCasualOccurrencePresentation:
    physician_name: str
    url: str
    date: date | None = None


@dataclass(frozen=True)
class RoundingCasualPresentation:
    rules: tuple[RoundingCasualOccurrencePresentation, ...]
