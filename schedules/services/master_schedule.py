"""Read-only, slot-first projection for one Master Schedule week."""

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Prefetch, Q

from ..academic_years import academic_year_bounds, academic_year_for_date
from ..models.academic_events import AcademicEvent
from ..models.interviews import InterviewShift
from ..models.late_docs import LateDocAssignment
from ..models.physicians import EffectiveRole, Physician
from ..models.preceptors import PreceptorAssignment, PreceptorSession
from ..models.rounding import CasualRounderRule
from .academic_events import academic_event_attention_active, academic_events_overlapping
from .community import community_preceptor_occurrences
from .holidays import holiday_projections_for_dates
from .interviews import _interview_findings_for_shift
from .operational_attention import organization_today
from .preceptors import _draft_findings_for_sessions, draft_finding_message
from .rounding import normal_rounder_statuses_for_weeks
from .slot_projection import RequestedSlot, slot_projection
from .vacations import vacations_overlapping


@dataclass(frozen=True)
class MasterSchedulePhysician:
    """A physician named by this projection, including retained references."""

    physician: Physician
    is_effective: bool
    is_within_focus: bool


@dataclass(frozen=True)
class MasterScheduleFact:
    """A traceable source fact that the presentation layer can link to its owner."""

    kind: str
    source_id: int | str | None
    record: object
    label: str
    explanation: str
    physician: Physician | None = None
    start_date: date | None = None
    end_date: date | None = None
    periods: tuple[str, ...] = ()
    attention: str = ""

    @property
    def source_type(self):
        return self.kind


@dataclass(frozen=True)
class MasterScheduleCandidate:
    """The compact Tier 0 state and separate current occupancy for one physician."""

    physician: Physician
    availability: str
    availability_sources: tuple[MasterScheduleFact, ...]
    occupancy: tuple[MasterScheduleFact, ...]


@dataclass(frozen=True)
class MasterScheduleCoverage:
    """One AM or PM coverage location inside a fixed operational slot cell."""

    period_of_day: str
    is_applicable: bool
    facts: tuple[MasterScheduleFact, ...]
    displayed_facts: tuple[MasterScheduleFact, ...]
    candidates: tuple[MasterScheduleCandidate, ...]
    displayed_candidates: tuple[MasterScheduleCandidate, ...]


@dataclass(frozen=True)
class MasterScheduleDay:
    on_date: date
    coverage: tuple[MasterScheduleCoverage, ...]


@dataclass(frozen=True)
class MasterScheduleSlot:
    key: str
    label: str
    days: tuple[MasterScheduleDay, ...]


@dataclass(frozen=True)
class MasterSchedulePositionGroup:
    """A stable clinic period group with positions as its row axis."""

    key: str
    label: str
    positions: tuple[MasterScheduleSlot, ...]


@dataclass(frozen=True)
class MasterScheduleAvailability:
    """One Faculty physician's effective state for one weekday AM or PM."""

    period_of_day: str
    availability: str | None
    sources: tuple[MasterScheduleFact, ...]


@dataclass(frozen=True)
class MasterScheduleAvailabilityDay:
    on_date: date
    periods: tuple[MasterScheduleAvailability, ...]


@dataclass(frozen=True)
class MasterScheduleFacultyAvailability:
    """A Faculty availability row that stays present when the global focus narrows."""

    physician: MasterSchedulePhysician
    days: tuple[MasterScheduleAvailabilityDay, ...]


@dataclass(frozen=True)
class MasterScheduleSupportingGroup:
    """Independently owned supporting facts arranged by their source schedule."""

    key: str
    label: str
    facts: tuple[MasterScheduleFact, ...]


@dataclass(frozen=True)
class MasterScheduleFindingTarget:
    """One presentation-neutral location affected by a live finding."""

    group_key: str
    row_key: str
    on_date: date
    period_of_day: str | None = None


@dataclass(frozen=True)
class MasterScheduleFinding:
    """An existing operational finding and every location it affects here."""

    code: str
    severity: str
    message: str
    physicians: tuple[Physician, ...]
    sources: tuple[MasterScheduleFact, ...]
    targets: tuple[MasterScheduleFindingTarget, ...]


@dataclass(frozen=True)
class MasterScheduleWeek:
    week_start: date
    week_end: date
    physicians: tuple[MasterSchedulePhysician, ...]
    slots: tuple[MasterScheduleSlot, ...]
    supporting_facts: tuple[MasterScheduleFact, ...]
    clinic_groups: tuple[MasterSchedulePositionGroup, ...]
    faculty_availability: tuple[MasterScheduleFacultyAvailability, ...]
    supporting_groups: tuple[MasterScheduleSupportingGroup, ...]
    findings: tuple[MasterScheduleFinding, ...]


@dataclass(frozen=True)
class _SlotDefinition:
    key: str
    label: str
    periods: tuple[str, ...]
    schedule: str
    primary_position: int | None = None
    responsibility: str | None = None


_SLOT_DEFINITIONS = (
    _SlotDefinition(
        "precepting-primary-1",
        "Precepting primary 1",
        ("AM", "PM"),
        "precepting",
        primary_position=1,
        responsibility=PreceptorAssignment.Responsibility.PRIMARY,
    ),
    _SlotDefinition(
        "precepting-primary-2",
        "Precepting primary 2",
        ("AM", "PM"),
        "precepting",
        primary_position=2,
        responsibility=PreceptorAssignment.Responsibility.PRIMARY,
    ),
    _SlotDefinition(
        "precepting-secondary",
        "Precepting secondary",
        ("AM", "PM"),
        "precepting",
        responsibility=PreceptorAssignment.Responsibility.SECONDARY,
    ),
    _SlotDefinition("late-doc", "Late Doc", ("PM",), "late_doc"),
    _SlotDefinition("interviews", "Interviews", ("AM",), "interviews"),
)

_INTERVIEW_SEASON_MONTHS = frozenset((10, 11, 12, 1))


def master_schedule_week(on_date: date, *, physician_focus_ids=()) -> MasterScheduleWeek:
    """Project Monday through Sunday without creating, locking, or changing source records."""
    week_start = on_date - timedelta(days=on_date.weekday())
    today = organization_today()
    operational_week = week_start >= today - timedelta(days=today.weekday())
    dates = tuple(week_start + timedelta(days=offset) for offset in range(7))
    requested_slots = tuple(
        RequestedSlot(day, period)
        for day in dates
        for period in PreceptorSession.PeriodOfDay.values
    )
    rows = slot_projection(requested_slots)
    rows_by_slot = {
        (row.slot.on_date, row.slot.period_of_day, row.physician.pk): row for row in rows
    }
    physicians = tuple(dict.fromkeys(row.physician for row in rows))
    focus_ids = frozenset(physician_focus_ids)
    interview_shifts = tuple(
        InterviewShift.objects.filter(
            date__range=(week_start, week_start + timedelta(days=4)), is_active=True
        )
        .select_related("physician")
        .order_by("date", "pk")
    )
    interviews_by_date = {shift.date: shift for shift in interview_shifts}
    late_docs = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(
            date__range=(week_start, week_start)
        ).select_related("physician")
    }

    slots = tuple(
        MasterScheduleSlot(
            definition.key,
            definition.label,
            tuple(
                MasterScheduleDay(
                    day,
                    tuple(
                        _coverage(
                            definition,
                            day,
                            period,
                            physicians,
                            rows_by_slot,
                            late_docs.get(day),
                            interviews_by_date.get(day),
                            focus_ids,
                        )
                        for period in definition.periods
                    ),
                )
                for day in dates
            ),
        )
        for definition in _SLOT_DEFINITIONS
    )
    holidays = holiday_projections_for_dates(dates)
    supporting_start = week_start - timedelta(days=1)
    supporting_end = week_start + timedelta(days=5)
    supporting_dates = tuple(
        supporting_start + timedelta(days=offset)
        for offset in range((supporting_end - supporting_start).days + 1)
    )
    supporting_facts = supporting_schedule_facts_for_week(
        supporting_start,
        supporting_end,
        holidays=holiday_projections_for_dates(supporting_dates),
        normal_rounding_week_start=week_start,
        operational_week=operational_week,
    )
    physicians = tuple(
        dict.fromkeys(
            (
                *physicians,
                *(fact.physician for fact in supporting_facts if fact.physician is not None),
            )
        )
    )
    effective_ids = {row.physician.pk for row in rows if row.effective_roles}
    referenced_ids = {
        fact.physician.pk
        for slot in slots
        for day in slot.days
        for coverage in day.coverage
        for fact in coverage.facts
        if fact.physician is not None
    } | {fact.physician.pk for fact in supporting_facts if fact.physician is not None}
    included_ids = effective_ids | referenced_ids
    master_physicians = tuple(
        MasterSchedulePhysician(
            physician,
            physician.pk in effective_ids,
            not focus_ids or physician.pk in focus_ids,
        )
        for physician in physicians
        if physician.pk in included_ids
    )
    slots_by_key = {slot.key: slot for slot in slots}
    morning_positions = (
        "precepting-primary-1",
        "precepting-primary-2",
        "precepting-secondary",
    )
    if _interview_row_is_useful(week_start):
        morning_positions += ("interviews",)
    clinic_groups = (
        MasterSchedulePositionGroup(
            "clinic-morning",
            "Morning",
            tuple(_weekday_position(slots_by_key[key], "AM") for key in morning_positions),
        ),
        MasterSchedulePositionGroup(
            "clinic-afternoon",
            "Afternoon",
            tuple(
                _weekday_position(slots_by_key[key], "PM")
                for key in (
                    "precepting-primary-1",
                    "precepting-primary-2",
                    "precepting-secondary",
                    "late-doc",
                )
            ),
        ),
    )
    faculty_availability = _faculty_availability(dates[:5], master_physicians, rows_by_slot)
    supporting_groups = tuple(
        MasterScheduleSupportingGroup(
            key, label, tuple(fact for fact in supporting_facts if fact.kind in kinds)
        )
        for key, label, kinds in (
            ("rounding", "Rounding", {"normal_rounder", "casual_rounder_rule"}),
            ("community-precepting", "Community Precepting", {"community_preceptor_rule"}),
            ("vacation", "Vacation", {"vacation"}),
            ("academic-events", "Academic Events", {"academic_event"}),
        )
    )
    return MasterScheduleWeek(
        week_start=week_start,
        week_end=dates[-1],
        physicians=master_physicians,
        slots=slots,
        supporting_facts=supporting_facts,
        clinic_groups=clinic_groups,
        faculty_availability=faculty_availability,
        supporting_groups=supporting_groups,
        findings=_operational_findings(
            week_start,
            rows_by_slot,
            interview_shifts,
            holidays,
            operational_week=operational_week,
        ),
    )


def supporting_schedule_facts_for_week(
    week_start,
    week_end,
    *,
    holidays=None,
    normal_rounding_week_start=None,
    operational_week=None,
):
    """Compose independently owned supporting facts without changing core slot geometry."""
    dates = tuple(
        week_start + timedelta(days=offset) for offset in range((week_end - week_start).days + 1)
    )
    holidays = holiday_projections_for_dates(dates) if holidays is None else holidays
    facts = []

    for event in academic_events_overlapping(week_start, week_end):
        participants = tuple(event.participants.all())
        attention = (
            "incomplete"
            if event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
            and academic_event_attention_active(event)
            and not event.all_available_faculty
            and len(participants) < event.required_participant_count
            else ""
        )
        explanation = (
            "Calendar-only Academic Event."
            if event.precepting_impact == AcademicEvent.PreceptingImpact.NONE
            else f"{len(participants)} of {event.required_participant_count} required participants."
            if attention
            else "All available Faculty."
            if event.all_available_faculty
            else (
                "Soft Academic Event participation."
                if event.precepting_impact == AcademicEvent.PreceptingImpact.SOFT
                else "Academic Event participation."
            )
        )
        for physician in participants or (None,):
            for start_date, end_date in event.date_ranges:
                facts.append(
                    MasterScheduleFact(
                        "academic_event",
                        event.pk,
                        event,
                        event.title,
                        explanation,
                        physician,
                        start_date,
                        end_date,
                        _event_periods(event),
                        attention,
                    )
                )

    facts.extend(
        MasterScheduleFact(
            "vacation",
            vacation.pk,
            vacation,
            "Vacation",
            vacation.note or "Recorded vacation.",
            vacation.physician,
            vacation.start_date,
            vacation.end_date,
            _periods(vacation.period_of_day),
        )
        for vacation in vacations_overlapping(week_start, week_end)
    )

    facts.extend(
        _community_facts(
            community_preceptor_occurrences(week_start, week_end, holiday_projections=holidays)
        )
    )
    normal_rounding_week_start = normal_rounding_week_start or week_start
    facts.extend(
        _rounding_facts(
            normal_rounding_week_start,
            normal_rounding_week_start + timedelta(days=6),
            operational_week=operational_week,
        )
    )
    facts.extend(
        MasterScheduleFact(
            "holiday_closure",
            f"{holiday.observed_date.isoformat()}:{holiday.name}",
            holiday,
            holiday.name,
            f"Clinic closed {holiday.coverage_label.lower()}.",
            start_date=holiday.observed_date,
            end_date=holiday.observed_date,
            periods=("AM", "PM") if holiday.closes("AM") else ("PM",),
        )
        for holiday in holidays
    )
    return tuple(facts)


def _community_facts(occurrences):
    return tuple(
        MasterScheduleFact(
            "community_preceptor_rule",
            occurrence.rule_id,
            occurrence.rule,
            "Community Precepting",
            "Recurring Community Precepting commitment.",
            occurrence.rule.physician,
            occurrence.date,
            occurrence.date,
            (occurrence.period,),
        )
        for occurrence in occurrences
    )


def _rounding_facts(week_start, week_end, *, operational_week=None):
    status = normal_rounder_statuses_for_weeks((week_start,))[0]
    attention = ""
    explanation = "Assigned normal rounder for this Monday-Sunday week."
    if operational_week is None:
        operational_week = week_start + timedelta(days=4) >= organization_today()
    if operational_week:
        if status.normal_rounder is None:
            attention = "missing"
            explanation = "Normal rounder required for this Monday-Sunday week."
        elif status.inconsistent:
            attention = "conflict"
            explanation = status.reason
    facts = []
    if status.normal_rounder or attention:
        facts.append(
            MasterScheduleFact(
                "normal_rounder",
                status.normal_rounder.pk if status.normal_rounder else None,
                status.normal_rounder,
                "Normal Rounding",
                explanation,
                status.normal_rounder_physician,
                week_start,
                week_end,
                ("AM", "PM"),
                attention,
            )
        )
    casual_rules = (
        CasualRounderRule.objects.filter(
            Q(effective_start__isnull=True) | Q(effective_start__lte=week_end)
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=week_start))
        .select_related("physician")
    )
    facts.extend(
        MasterScheduleFact(
            "casual_rounder_rule",
            rule.pk,
            rule,
            "Casual Rounding",
            "Recurring casual rounding commitment.",
            rule.physician,
            on_date,
            on_date,
            ("AM", "PM"),
        )
        for rule in casual_rules
        for on_date in (week_start + timedelta(days=offset) for offset in range(7))
        if rule.matches(on_date)
    )
    return tuple(facts)


def _periods(period_of_day):
    return (
        ("AM", "PM") if period_of_day == AcademicEvent.PeriodOfDay.WHOLE_DAY else (period_of_day,)
    )


def _event_periods(event):
    return (
        ()
        if event.precepting_impact == AcademicEvent.PreceptingImpact.NONE
        else _periods(event.period_of_day)
    )


def _coverage(
    definition, day, period, physicians, rows_by_slot, late_doc, interview_shift, focus_ids
):
    applicable = day.weekday() < 5 and not (
        definition.schedule == "late_doc" and day.weekday() != 0
    )
    if not applicable:
        return MasterScheduleCoverage(period, False, (), (), (), ())
    rows = tuple(rows_by_slot[day, period, physician.pk] for physician in physicians)
    facts = _direct_facts(definition, rows, late_doc, interview_shift)
    candidates = tuple(
        candidate
        for row in rows
        if _is_candidate(definition, row)
        if (candidate := _candidate(row)) is not None
    )
    return MasterScheduleCoverage(
        period,
        True,
        facts,
        _in_focus(facts, focus_ids),
        candidates,
        tuple(
            candidate for candidate in candidates if _in_focus((candidate.physician,), focus_ids)
        ),
    )


def _direct_facts(definition, rows, late_doc, interview_shift):
    facts = []
    if definition.schedule == "late_doc":
        if late_doc:
            facts.append(
                MasterScheduleFact(
                    "late_doc_assignment",
                    late_doc.pk,
                    late_doc,
                    "Late Doc assignment",
                    "Assigned as Monday PM Late Doc.",
                    late_doc.physician,
                )
            )
        return tuple(facts)
    if definition.schedule == "interviews" and interview_shift and not interview_shift.physician_id:
        facts.append(
            MasterScheduleFact(
                "interview_shift",
                interview_shift.pk,
                interview_shift,
                "Interview shift",
                "No physician is assigned to this AM Interview shift.",
            )
        )
    for row in rows:
        if definition.schedule == "precepting" and row.preceptor_assignment:
            assignment = row.preceptor_assignment
            if (
                assignment.responsibility == definition.responsibility
                and assignment.primary_position == definition.primary_position
            ):
                facts.append(_preceptor_fact(assignment, row.physician))
        elif definition.schedule == "interviews" and row.interview_shift:
            facts.append(
                MasterScheduleFact(
                    "interview_shift",
                    row.interview_shift.pk,
                    row.interview_shift,
                    "Interview shift",
                    "Assigned to this AM Interview shift.",
                    row.physician,
                )
            )
    return tuple(facts)


def _candidate(row):
    if row.availability is None:
        return None
    return MasterScheduleCandidate(
        row.physician,
        row.availability,
        _availability_sources(row),
        _occupancy(row),
    )


def _occupancy(row):
    facts = []
    if row.preceptor_assignment:
        facts.append(_preceptor_fact(row.preceptor_assignment, row.physician))
    if row.interview_shift:
        facts.append(
            MasterScheduleFact(
                "interview_shift",
                row.interview_shift.pk,
                row.interview_shift,
                "Interview shift",
                "Assigned to this AM Interview shift.",
                row.physician,
            )
        )
    displayed_sources = {(fact.kind, fact.source_id) for fact in facts}
    for conflict in row.precepting_conflicts:
        source = (conflict.source_type, conflict.source_id)
        if source not in displayed_sources:
            facts.append(
                MasterScheduleFact(
                    conflict.source_type,
                    conflict.source_id,
                    conflict.record,
                    conflict.label,
                    conflict.reason,
                    row.physician,
                )
            )
            displayed_sources.add(source)
    return tuple(facts)


def _preceptor_fact(assignment, physician):
    if assignment.responsibility == PreceptorAssignment.Responsibility.PRIMARY:
        position = f"Preceptor {assignment.primary_position}"
    else:
        position = "secondary position"
    return MasterScheduleFact(
        "preceptor_assignment",
        assignment.pk,
        assignment,
        "Precepting assignment",
        f"Assigned to Precepting {position}.",
        physician,
    )


def _is_candidate(definition, row):
    if definition.schedule == "precepting":
        if definition.responsibility == PreceptorAssignment.Responsibility.SECONDARY:
            return any(role.role == EffectiveRole.Role.FACULTY for role in row.effective_roles)
        return row.ordinarily_eligible_to_precept
    return any(role.role == EffectiveRole.Role.FACULTY for role in row.effective_roles)


def _weekday_position(slot, period):
    return MasterScheduleSlot(
        slot.key,
        slot.label,
        tuple(
            MasterScheduleDay(
                day.on_date,
                tuple(coverage for coverage in day.coverage if coverage.period_of_day == period),
            )
            for day in slot.days[:5]
        ),
    )


def _interview_row_is_useful(week_start):
    weekdays = tuple(week_start + timedelta(days=offset) for offset in range(5))
    if not any(on_date.month in _INTERVIEW_SEASON_MONTHS for on_date in weekdays):
        return False
    january_date = next((on_date for on_date in weekdays if on_date.month == 1), None)
    if january_date is None:
        return True
    academic_year_start, academic_year_end = academic_year_bounds(
        academic_year_for_date(january_date)
    )
    final_shift_date = (
        InterviewShift.objects.filter(
            date__range=(academic_year_start, academic_year_end), is_active=True
        )
        .order_by("-date", "-pk")
        .values_list("date", flat=True)
        .first()
    )
    return final_shift_date is None or final_shift_date >= week_start


def _faculty_availability(dates, physicians, rows_by_slot):
    faculty = tuple(
        physician
        for physician in physicians
        if physician.is_effective
        and any(
            role.role == EffectiveRole.Role.FACULTY
            for on_date in dates
            for period in PreceptorSession.PeriodOfDay.values
            for role in rows_by_slot[on_date, period, physician.physician.pk].effective_roles
        )
    )
    return tuple(
        MasterScheduleFacultyAvailability(
            physician,
            tuple(
                MasterScheduleAvailabilityDay(
                    on_date,
                    tuple(
                        MasterScheduleAvailability(
                            period,
                            row.availability,
                            _availability_sources(row),
                        )
                        for period in PreceptorSession.PeriodOfDay.values
                        if (
                            row := rows_by_slot[on_date, period, physician.physician.pk]
                        ).effective_roles
                    ),
                )
                for on_date in dates
            ),
        )
        for physician in faculty
    )


def _availability_sources(row):
    return tuple(
        MasterScheduleFact(
            source.source_type,
            source.source_id,
            source.record,
            source.label,
            source.reason,
            row.physician,
        )
        for source in row.sources
    )


def _operational_findings(
    week_start, rows_by_slot, interview_shifts, holidays, *, operational_week
):
    if not operational_week:
        return ()
    sessions = tuple(
        PreceptorSession.objects.filter(date__range=(week_start, week_start + timedelta(days=4)))
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=PreceptorAssignment.objects.select_related("physician").order_by(
                    "responsibility", "primary_position", "pk"
                ),
            )
        )
        .order_by("date", "period_of_day", "pk")
    )
    findings = []
    if sessions:
        findings.extend(
            _master_preceptor_finding(finding)
            for finding in _draft_findings_for_sessions(
                sessions,
                weeks=(week_start,),
                rows_by_slot_and_physician=rows_by_slot,
                holidays=holidays,
                operational=True,
            ).findings
        )
    for shift in interview_shifts:
        findings.extend(
            _master_interview_finding(finding)
            for finding in _interview_findings_for_shift(
                shift,
                {
                    physician_id: row
                    for (on_date, period, physician_id), row in rows_by_slot.items()
                    if (on_date, period) == (shift.date, "AM")
                },
                operational=True,
            )
        )
    return tuple(findings)


def _master_preceptor_finding(finding):
    if finding.week:
        targets = (MasterScheduleFindingTarget("rounding", "normal-rounding", finding.week),)
    elif finding.code.endswith("late_doc") or finding.code in {
        "missing_late_doc",
        "role_ineligible_late_doc",
        "holiday_closed_late_doc_assignment",
    }:
        targets = (
            MasterScheduleFindingTarget("clinic-afternoon", "late-doc", finding.session.date, "PM"),
        )
    else:
        positions = _preceptor_finding_positions(finding)
        group = "clinic-morning" if finding.session.period_of_day == "AM" else "clinic-afternoon"
        targets = tuple(
            MasterScheduleFindingTarget(
                group, position, finding.session.date, finding.session.period_of_day
            )
            for position in positions
        )
    return MasterScheduleFinding(
        finding.code,
        finding.severity,
        draft_finding_message(finding.code, finding.explanation),
        finding.physicians,
        _finding_sources(finding.sources, finding.physicians),
        targets,
    )


def _preceptor_finding_positions(finding):
    if finding.code == "insufficient_faculty_equivalent_coverage":
        return ("precepting-primary-1", "precepting-primary-2", "precepting-secondary")
    position = finding.explanation.get("position")
    if position:
        return (f"precepting-{position}",)
    return ("precepting-primary-1", "precepting-primary-2", "precepting-secondary")


def _master_interview_finding(finding):
    return MasterScheduleFinding(
        finding.code,
        finding.severity,
        finding.message,
        finding.physicians,
        _finding_sources(finding.sources, finding.physicians),
        (MasterScheduleFindingTarget("clinic-morning", "interviews", finding.shift.date, "AM"),),
    )


def _finding_sources(sources, physicians):
    physician = physicians[0] if len(physicians) == 1 else None
    return tuple(
        MasterScheduleFact(
            source.source_type,
            source.source_id,
            source.record,
            source.label,
            source.reason,
            physician,
        )
        for source in sources
    )


def _in_focus(values, focus_ids):
    if not focus_ids:
        return tuple(values)
    return tuple(
        value
        for value in values
        if (value.physician if isinstance(value, MasterScheduleFact) else value).pk in focus_ids
    )
