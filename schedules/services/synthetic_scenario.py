"""Reusable deterministic inputs for the synthetic scheduling scenario."""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction

from schedules.academic_years import academic_year_for_date, expected_quarter_start
from schedules.management.commands.import_faculty_template_roster import (
    _apply_plan,
    _build_plan,
    _load_roster,
)
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    AcademicEventQuarter,
    CustomAvailabilityCategory,
    EffectiveRole,
    InterviewShift,
    LateDocAssignment,
    NormalRounder,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    StipendRate,
)
from schedules.services._mutation import (
    require_scheduling_administrator,
    serialized_scheduling_state,
)
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    initialize_monthly_academic_event_occurrences,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.holidays import (
    holiday_closure_for,
    holiday_occurrences_for,
    schedule_holiday_occurrence,
)
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.late_docs import assign_late_doc, remove_late_doc_assignment
from schedules.services.lead_times import ensure_lead_time_defaults, lead_time_months
from schedules.services.operational_attention import organization_today
from schedules.services.physicians import add_role, create_physician
from schedules.services.preceptors import (
    _draft_findings_for_sessions,
    assign_preceptor,
    initialize_preceptor_session_range,
    remove_preceptor_assignment,
    work_week_start,
)
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.slot_projection import RequestedSlot, slot_projection
from schedules.services.stipend_rates import append_stipend_rate, set_initial_stipend_rate
from schedules.services.vacations import create_vacation
from schedules.services.work_remaining import shift_date_months

BASELINE_START = date(2020, 7, 1)
PRECEPTING_VALIDATION_BATCH_WEEKS = 50
SCHEDULE_BOT_PROBE_MONTHS = 6
FACULTY_ROSTER_PATH = Path(__file__).resolve().parents[2] / "data" / "faculty_template_roster.csv"
COMMUNITY_PRECEPTOR_NAMES = (
    # TEXT-CONTRACT: synthetic-community-preceptor-identities — the accepted
    # synthetic baseline and its focused test use these stable generated identity
    # keys. ASCII capitalization, spaces, and the digits 1 through 3 are exact;
    # docs/design.md "Accepted synthetic scenario baseline" and
    # schedules/tests/test_synthetic_scenario.py protect that contract.
    "Fake Community Doc 1",
    "Fake Community Doc 2",
    "Fake Community Doc 3",
)
EXCLUDED_EMPTY_DOMAIN_MODELS = frozenset(
    {
        "schedules.AccountChange",
        "schedules.LeadTimeChange",
        "schedules.LeadTimeSetting",
        "schedules.SchedulerInvitation",
        "schedules.SchedulingMutationAuthority",
    }
)


@dataclass(frozen=True)
class SyntheticScenarioBaseline:
    faculty_physicians: tuple[Physician, ...]
    community_preceptors: tuple[Physician, ...]


class SyntheticScenario:
    """Own one seed's stable choices as later generators extend its end date."""

    def __init__(self, *, actor, seed):
        self.actor = actor
        self.seed = str(seed)

    def establish_baseline(self):
        require_scheduling_administrator(self.actor)
        with serialized_scheduling_state():
            _require_empty_scheduling_domain()
            rows = _load_roster(FACULTY_ROSTER_PATH)
            plan = _build_plan(rows, BASELINE_START)
            if plan.blockers:
                raise ValidationError(
                    "Synthetic scenario baseline is blocked: " + "; ".join(plan.blockers)
                )

            reason = f"Synthetic scenario baseline (seed {self.seed})."
            with transaction.atomic():
                _apply_plan(plan, self.actor, BASELINE_START, reason)
                community_preceptors = self._create_community_preceptors(reason)

        return SyntheticScenarioBaseline(
            faculty_physicians=tuple(
                Physician.objects.filter(display_name__in=[row.name for row in rows]).order_by(
                    "display_name", "pk"
                )
            ),
            community_preceptors=community_preceptors,
        )

    def generate_source_schedule_history(self, baseline, *, today=None):
        """Populate retained non-Precepting inputs through their attention horizons."""
        require_scheduling_administrator(self.actor)
        today = today or organization_today()
        if today < BASELINE_START:
            raise ValidationError("Synthetic scenario history cannot end before its baseline.")
        ensure_lead_time_defaults()
        history_end = _month_end(today)
        faculty = tuple(sorted(baseline.faculty_physicians, key=lambda physician: physician.pk))
        reason = f"Synthetic scenario source history (seed {self.seed})."

        self._schedule_holidays(today, reason)
        self._schedule_normal_rounding(faculty, today, reason)
        self._schedule_casual_rounding(baseline.community_preceptors, reason)
        self._schedule_academic_events(faculty, history_end, today, reason)
        self._schedule_vacations(faculty, history_end, reason)
        self._schedule_interviews(faculty, today, reason)
        self._schedule_stipend_rates(reason)

    def generate_precepting_history(self, baseline, *, today=None):
        """Complete Precepting through the current month and leave later work open."""
        require_scheduling_administrator(self.actor)
        today = today or organization_today()
        if today < BASELINE_START:
            raise ValidationError("Synthetic scenario history cannot end before its baseline.")

        horizon_end = work_week_start(
            shift_date_months(today, lead_time_months("precepting"))
        ) + timedelta(days=4)
        history_end = _month_end(today)
        reason = f"Synthetic scenario Precepting history (seed {self.seed})."

        with serialized_scheduling_state():
            self._schedule_vacations(
                tuple(sorted(baseline.faculty_physicians, key=lambda physician: physician.pk)),
                horizon_end,
                reason,
            )
            initialize_preceptor_session_range(BASELINE_START, horizon_end)
            self._clear_future_precepting(history_end, horizon_end, reason)
            for on_date in _weekdays_between(BASELINE_START, history_end):
                self._fill_precepting_session(on_date, reason)
                if on_date.weekday() == 0:
                    self._fill_late_doc(on_date, reason)

            completed_weeks = tuple(
                week
                for week in _week_starts_between(BASELINE_START, horizon_end)
                if week >= BASELINE_START and week + timedelta(days=4) <= history_end
            )
            self._validate_precepting_weeks(completed_weeks)
            self._validate_initial_partial_week(history_end)
            self._validate_final_partial_week(history_end)
            self._validate_future_work_remaining(today, history_end, horizon_end)

    def _clear_future_precepting(self, history_end, horizon_end, reason):
        for assignment in PreceptorAssignment.objects.filter(
            session__date__gt=history_end, session__date__lte=horizon_end
        ).order_by("session__date", "session__period_of_day", "pk"):
            remove_preceptor_assignment(assignment=assignment, scheduler=self.actor, reason=reason)
        for assignment in LateDocAssignment.objects.filter(
            date__gt=history_end, date__lte=horizon_end
        ).order_by("date", "pk"):
            remove_late_doc_assignment(assignment=assignment, scheduler=self.actor, note=reason)

    def _fill_precepting_session(self, on_date, reason):
        for period_of_day in PreceptorSession.PeriodOfDay.values:
            if holiday_closure_for(on_date, period_of_day):
                continue
            session = PreceptorSession.objects.get(date=on_date, period_of_day=period_of_day)
            rows = tuple(slot_projection((RequestedSlot(on_date, period_of_day),)))
            rows_by_physician = {row.physician.pk: row for row in rows}
            assignments = tuple(
                session.assignments.select_related("physician").order_by(
                    "responsibility", "primary_position", "pk"
                )
            )
            assigned_ids = {assignment.physician_id for assignment in assignments}
            primary_positions = {
                assignment.primary_position
                for assignment in assignments
                if assignment.responsibility == PreceptorAssignment.Responsibility.PRIMARY
            }

            community = next(
                (
                    row
                    for row in self._ordered_available_rows(
                        rows, on_date, period_of_day, "community"
                    )
                    if row.is_expected_community_commitment and row.physician.pk not in assigned_ids
                ),
                None,
            )
            if community is not None and len(primary_positions) < 2:
                position = next(
                    position for position in (1, 2) if position not in primary_positions
                )
                assign_preceptor(
                    session=session,
                    physician=community.physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=position,
                    scheduler=self.actor,
                    reason=reason,
                )
                assignments += (
                    session.assignments.select_related("physician").get(primary_position=position),
                )
                assigned_ids.add(community.physician.pk)
                primary_positions.add(position)

            while len(primary_positions) < 2:
                physician = self._next_available_faculty(
                    rows, assigned_ids, on_date, period_of_day, len(primary_positions) + 1
                )
                position = next(
                    position for position in (1, 2) if position not in primary_positions
                )
                assign_preceptor(
                    session=session,
                    physician=physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=position,
                    scheduler=self.actor,
                    reason=reason,
                )
                assignments += (
                    session.assignments.select_related("physician").get(primary_position=position),
                )
                assigned_ids.add(physician.pk)
                primary_positions.add(position)

            faculty_equivalent_count = sum(
                rows_by_physician[assignment.physician_id].faculty_equivalent
                for assignment in assignments
            )
            while faculty_equivalent_count < 2:
                physician = self._next_available_faculty(
                    rows, assigned_ids, on_date, period_of_day, "secondary"
                )
                assign_preceptor(
                    session=session,
                    physician=physician,
                    responsibility=PreceptorAssignment.Responsibility.SECONDARY,
                    primary_position=None,
                    scheduler=self.actor,
                    reason=reason,
                )
                assignments += (
                    session.assignments.select_related("physician").get(
                        responsibility=PreceptorAssignment.Responsibility.SECONDARY
                    ),
                )
                assigned_ids.add(physician.pk)
                faculty_equivalent_count += rows_by_physician[physician.pk].faculty_equivalent

    def _fill_late_doc(self, on_date, reason):
        if holiday_closure_for(on_date, "PM"):
            return
        if LateDocAssignment.objects.filter(date=on_date).exists():
            return
        if assignable := self._next_available_faculty(
            slot_projection((RequestedSlot(on_date, "PM"),)), set(), on_date, "late-doc", "late-doc"
        ):
            assign_late_doc(
                on_date=on_date,
                physician=assignable,
                scheduler=self.actor,
                note=reason,
            )

    def _ordered_available_rows(self, rows, on_date, period_of_day, position):
        return tuple(
            sorted(
                (
                    row
                    for row in rows
                    if row.availability in ("available", "backup_only")
                    and not row.precepting_conflicts
                ),
                key=lambda row: (
                    row.availability != "available",
                    self._stable_number(
                        "precepting-assignment",
                        row.physician.display_name,
                        f"{on_date.isoformat()}:{period_of_day}:{position}",
                    ),
                    row.physician.pk,
                ),
            )
        )

    def _next_available_faculty(self, rows, assigned_ids, on_date, period_of_day, position):
        physician = next(
            (
                row.physician
                for row in self._ordered_available_rows(rows, on_date, period_of_day, position)
                if row.physician.pk not in assigned_ids
                and any(role.role == EffectiveRole.Role.FACULTY for role in row.effective_roles)
            ),
            None,
        )
        if physician is None:
            raise ValidationError(
                f"Synthetic Precepting history cannot fill {on_date.isoformat()} {period_of_day}."
            )
        return physician

    def _validate_precepting_weeks(self, weeks):
        for start in range(0, len(weeks), PRECEPTING_VALIDATION_BATCH_WEEKS):
            batch = weeks[start : start + PRECEPTING_VALIDATION_BATCH_WEEKS]
            sessions = PreceptorSession.objects.filter(
                date__in=[week + timedelta(days=offset) for week in batch for offset in range(5)]
            ).order_by("date", "period_of_day", "pk")
            findings = _draft_findings_for_sessions(sessions, weeks=batch, operational=True)
            hard_findings = tuple(
                finding for finding in findings.findings if finding.severity == "hard"
            )
            if hard_findings:
                raise ValidationError(
                    "Synthetic Precepting history has unresolved findings: "
                    + "; ".join(
                        f"{finding.session.date if finding.session else finding.week}: {finding.message}"
                        for finding in hard_findings
                    )
                )

    def _validate_initial_partial_week(self, history_end):
        initial_week_end = work_week_start(BASELINE_START) + timedelta(days=4)
        if initial_week_end > history_end:
            return
        sessions = PreceptorSession.objects.filter(
            date__range=(BASELINE_START, initial_week_end)
        ).order_by("date", "period_of_day", "pk")
        findings = _draft_findings_for_sessions(sessions, weeks=(), operational=True)
        hard_findings = tuple(
            finding for finding in findings.findings if finding.severity == "hard"
        )
        if hard_findings:
            raise ValidationError(
                "Synthetic initial Precepting week has unresolved findings: "
                + "; ".join(finding.message for finding in hard_findings)
            )

    def _validate_final_partial_week(self, history_end):
        week = work_week_start(history_end)
        if week < BASELINE_START or history_end == week + timedelta(days=4):
            return
        sessions = PreceptorSession.objects.filter(date__range=(week, history_end)).order_by(
            "date", "period_of_day", "pk"
        )
        findings = _draft_findings_for_sessions(sessions, weeks=(week,), operational=True)
        hard_findings = tuple(
            finding for finding in findings.findings if finding.severity == "hard"
        )
        if hard_findings:
            raise ValidationError(
                "Synthetic final partial Precepting week has unresolved findings: "
                + "; ".join(finding.message for finding in hard_findings)
            )

    def _validate_future_work_remaining(self, today, history_end, horizon_end):
        from schedules.services.work_remaining import global_work_remaining

        expected_keys = {
            f"precepting-week-{week:%Y-%m-%d}"
            for week in _week_starts_between(work_week_start(today), horizon_end)
            if week + timedelta(days=4) > history_end
        }
        actual_keys = {item.key for item in global_work_remaining().items}
        if actual_keys != expected_keys:
            raise ValidationError(
                "Synthetic future Precepting does not match global Work Remaining."
            )

    def _schedule_holidays(self, today, reason):
        horizon_end = shift_date_months(today, lead_time_months("holidays"))
        for year in range(BASELINE_START.year, horizon_end.year + 1):
            for occurrence in holiday_occurrences_for(year):
                if (
                    occurrence.is_manually_schedulable
                    and BASELINE_START <= occurrence.nominal_date <= horizon_end
                ):
                    schedule_holiday_occurrence(
                        occurrence=occurrence,
                        observed_date=occurrence.nominal_date,
                        scheduler=self.actor,
                        note=reason,
                    )

    def _schedule_normal_rounding(self, faculty, today, reason):
        week = BASELINE_START + timedelta(days=(7 - BASELINE_START.weekday()) % 7)
        horizon_end = _month_end(
            shift_date_months(
                today,
                max(
                    lead_time_months("normal_rounding"),
                    lead_time_months("precepting"),
                    SCHEDULE_BOT_PROBE_MONTHS,
                ),
            )
        )
        while week <= horizon_end:
            if NormalRounder.objects.filter(week=week).exists():
                week += timedelta(days=7)
                continue
            slots = tuple(
                RequestedSlot(week + timedelta(days=offset), period)
                for offset in range(5)
                for period in ("AM", "PM")
            )
            available_by_slot = {slot: set() for slot in slots}
            for row in slot_projection(slots):
                if row.faculty_equivalent and row.availability == "available":
                    available_by_slot[row.slot].add(row.physician.pk)
            candidates = tuple(
                physician
                for physician in faculty
                if all(
                    len(available_physician_ids - {physician.pk}) >= 2
                    for available_physician_ids in available_by_slot.values()
                )
            )
            physician = self.choice(
                domain="normal-rounding", physician="faculty", period=week, choices=candidates
            )
            record_normal_rounder(
                week=week, physician=physician, scheduler=self.actor, reason=reason
            )
            week += timedelta(days=7)

    def _schedule_casual_rounding(self, community_preceptors, reason):
        casual_rounder = next(
            physician
            for physician in community_preceptors
            if physician.roles.filter(role=EffectiveRole.Role.CASUAL_ROUNDER).exists()
        )
        create_casual_rounder_rule(
            physician=casual_rounder,
            weekday=1,
            ordinals=(1, 3, 5),
            effective_start=BASELINE_START,
            scheduler=self.actor,
            reason=reason,
        )

    def _schedule_academic_events(self, faculty, history_end, today, reason):
        definitions = []
        for name, quarter, duration, period, participants in (
            (
                "Faculty Orientation",
                AcademicEventQuarter.Q1,
                2,
                AcademicEvent.PeriodOfDay.WHOLE_DAY,
                2,
            ),
            ("Teaching Workshop", AcademicEventQuarter.Q2, 1, AcademicEvent.PeriodOfDay.PM, 1),
            (
                "Residency Recruitment",
                AcademicEventQuarter.Q3,
                2,
                AcademicEvent.PeriodOfDay.WHOLE_DAY,
                2,
            ),
            ("Curriculum Retreat", AcademicEventQuarter.Q4, 1, AcademicEvent.PeriodOfDay.AM, 1),
        ):
            definition = AcademicEventDefinition.objects.filter(
                base_name=name, start_academic_year=BASELINE_START.year
            ).first()
            if definition is None:
                definition = create_academic_event_definition(
                    base_name=name,
                    start_academic_year=BASELINE_START.year,
                    expected_quarter=quarter,
                    default_duration_days=duration,
                    default_period_of_day=period,
                    default_required_participant_count=participants,
                    scheduler=self.actor,
                )
            definitions.append(definition)
        monthly_individual = AcademicEventDefinition.objects.filter(
            base_name="Monthly Faculty Workshop",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
        ).first()
        if monthly_individual is None:
            monthly_individual = create_academic_event_definition(
                base_name="Monthly Faculty Workshop",
                scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
                start_academic_year=BASELINE_START.year,
                default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
                default_required_participant_count=2,
                default_participants=faculty[:2],
                scheduler=self.actor,
            )
            create_academic_event_monthly_rule(
                definition=monthly_individual,
                weekday=3,
                ordinals=(1, 3),
                effective_start=BASELINE_START,
                scheduler=self.actor,
                reason=reason,
            )
        monthly_collective = AcademicEventDefinition.objects.filter(
            base_name="Monthly Faculty Forum",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
        ).first()
        if monthly_collective is None:
            monthly_collective = create_academic_event_definition(
                base_name="Monthly Faculty Forum",
                scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
                start_academic_year=BASELINE_START.year,
                default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
                all_available_faculty=True,
                scheduler=self.actor,
            )
            create_academic_event_monthly_rule(
                definition=monthly_collective,
                weekday=4,
                ordinals=(1, 3),
                effective_start=BASELINE_START,
                scheduler=self.actor,
                reason=reason,
            )
        initialize_monthly_academic_event_occurrences(
            start_date=BASELINE_START,
            end_date=history_end,
            scheduler=self.actor,
        )
        final_year = academic_year_for_date(
            max(history_end, shift_date_months(today, lead_time_months("academic_events")))
        )
        for academic_year in range(BASELINE_START.year, final_year + 1):
            for definition in definitions:
                event_start = self._event_start(academic_year, definition.expected_quarter)
                if event_start > history_end and event_start > shift_date_months(today, 6):
                    continue
                event_end = event_start + timedelta(days=definition.default_duration_days - 1)
                participants = self._event_participants(
                    faculty, definition, academic_year, event_start, event_end
                )
                create_academic_event_occurrence(
                    definition=definition,
                    academic_year=academic_year,
                    start_date=event_start,
                    end_date=event_end,
                    participants=participants,
                    period_of_day=definition.default_period_of_day,
                    scheduler=self.actor,
                    reason=reason,
                )

    def _event_start(self, academic_year, quarter):
        start = expected_quarter_start(academic_year, quarter)
        offset = self.choice(
            domain="academic-event-date", physician=quarter, period=start, choices=(4, 11, 18, 25)
        )
        start += timedelta(days=offset)
        while start.weekday() > 4 or holiday_closure_for(start, "AM"):
            start += timedelta(days=1)
        return start

    def _event_participants(self, faculty, definition, academic_year, event_start, event_end):
        ranked = sorted(
            faculty,
            key=lambda physician: (
                self._stable_number(
                    "academic-event-participant",
                    physician.display_name,
                    f"{definition.base_name}:{academic_year}:{event_start.isoformat()}",
                ),
                physician.pk,
            ),
        )
        participants = []
        for physician in ranked:
            if self._preserves_faculty_capacity(
                faculty,
                (*participants, physician),
                event_start,
                event_end,
                definition.default_period_of_day,
            ):
                participants.append(physician)
            if len(participants) == definition.default_required_participant_count:
                return participants
        raise ValidationError(
            "Synthetic Academic Event participants cannot preserve clinic coverage."
        )

    def _schedule_vacations(self, faculty, history_end, reason):
        for year in range(BASELINE_START.year, history_end.year + 1):
            for physician in faculty:
                week = self._vacation_week(physician, year)
                if week < BASELINE_START or week + timedelta(days=4) > history_end:
                    continue
                physician = self._vacation_physician(faculty, physician, week)
                if physician is None:
                    continue
                create_vacation(
                    physician=physician,
                    start_date=week,
                    end_date=week + timedelta(days=4),
                    scheduler=self.actor,
                    note=reason,
                )
                if self.choice(
                    domain="vacation-partial",
                    physician=physician.display_name,
                    period=date(year, 1, 1),
                    choices=(False, False, False, True),
                ):
                    partial_day = week + timedelta(days=7)
                    partial_period = self.choice(
                        domain="vacation-partial-period",
                        physician=physician.display_name,
                        period=partial_day,
                        choices=(AcademicEvent.PeriodOfDay.AM, AcademicEvent.PeriodOfDay.PM),
                    )
                    if partial_day <= history_end and self._preserves_faculty_capacity(
                        faculty, (physician,), partial_day, partial_day, partial_period
                    ):
                        create_vacation(
                            physician=physician,
                            start_date=partial_day,
                            end_date=partial_day,
                            period_of_day=partial_period,
                            scheduler=self.actor,
                            note=reason,
                        )

    def _vacation_week(self, physician, year):
        first = date(year, 1, 1)
        first += timedelta(days=(7 - first.weekday()) % 7)
        weeks = tuple(first + timedelta(days=7 * offset) for offset in range(52))
        return self.choice(
            domain="vacation-week",
            physician=physician.display_name,
            period=date(year, 1, 1),
            choices=weeks,
        )

    def _vacation_physician(self, faculty, preferred, week):
        ranked = sorted(
            faculty,
            key=lambda physician: (
                physician != preferred,
                self._stable_number("vacation-replacement", physician.display_name, week),
                physician.pk,
            ),
        )
        return next(
            (
                physician
                for physician in ranked
                if self._preserves_faculty_capacity(
                    faculty, (physician,), week, week + timedelta(days=4), "whole_day"
                )
            ),
            None,
        )

    def _preserves_faculty_capacity(self, faculty, absent, start_date, end_date, period_of_day):
        periods = ("AM", "PM") if period_of_day == "whole_day" else (period_of_day,)
        slots = tuple(
            RequestedSlot(on_date, period)
            for offset in range((end_date - start_date).days + 1)
            if (on_date := start_date + timedelta(days=offset)).weekday() < 5
            for period in periods
        )
        rows = slot_projection(slots)
        absent_ids = {physician.pk for physician in absent}
        available_counts = {slot: 0 for slot in slots}
        for row in rows:
            if (
                row.physician.pk not in absent_ids
                and row.faculty_equivalent
                and row.availability == "available"
            ):
                available_counts[row.slot] += 1
        return all(count >= 2 for count in available_counts.values())

    def _schedule_interviews(self, faculty, today, reason):
        for academic_year in range(BASELINE_START.year, academic_year_for_date(today) + 1):
            assigned = InterviewShift.objects.filter(
                date__range=(date(academic_year, 9, 1), date(academic_year + 1, 8, 31)),
                physician__isnull=False,
            ).count()
            on_date = date(academic_year, 9, 1)
            while assigned < 20:
                if InterviewShift.objects.filter(date=on_date).exists():
                    on_date += timedelta(days=3)
                    continue
                if on_date.weekday() < 5 and not holiday_closure_for(on_date, "AM"):
                    rows = {
                        row.physician.pk: row
                        for row in slot_projection((RequestedSlot(on_date, "AM"),))
                    }
                    candidates = tuple(
                        candidate
                        for candidate in faculty
                        if rows[candidate.pk].availability == "available"
                        and self._preserves_faculty_capacity(
                            faculty,
                            (candidate,),
                            on_date,
                            on_date,
                            AcademicEvent.PeriodOfDay.AM,
                        )
                    )
                    if candidates:
                        physician = self.choice(
                            domain="interview",
                            physician="faculty",
                            period=on_date,
                            choices=candidates,
                        )
                        shift = create_interview_shift(
                            academic_year=academic_year,
                            date=on_date,
                            scheduler=self.actor,
                            note=reason,
                        )
                        assign_interview_shift(
                            shift=shift, physician=physician, scheduler=self.actor
                        )
                        assigned += 1
                on_date += timedelta(days=3)

    def _schedule_stipend_rates(self, reason):
        for kind, initial, transitions in (
            (
                StipendRate.Kind.COMMUNITY_PRECEPTOR,
                Decimal("75.00"),
                (
                    (date(2022, 7, 1), Decimal("82.50")),
                    (date(2025, 1, 1), Decimal("90.00")),
                ),
            ),
            (
                StipendRate.Kind.LATE_DOC,
                Decimal("65.00"),
                (
                    (date(2023, 1, 1), Decimal("72.50")),
                    (date(2025, 7, 1), Decimal("80.00")),
                ),
            ),
        ):
            if StipendRate.objects.filter(kind=kind, retired_at__isnull=True).exists():
                continue
            set_initial_stipend_rate(
                kind=kind, hourly_rate=initial, scheduler=self.actor, note=reason
            )
            for transition_date, hourly_rate in transitions:
                append_stipend_rate(
                    kind=kind,
                    hourly_rate=hourly_rate,
                    transition_date=transition_date,
                    direction="later",
                    scheduler=self.actor,
                    note=reason,
                )

    def choice(self, *, domain, physician, period, choices):
        """Choose from a stable key, not generator order or a global random stream."""
        choices = tuple(choices)
        if not choices:
            raise ValueError("Synthetic scenario choices cannot be empty.")
        return choices[self._stable_number(domain, physician, period) % len(choices)]

    def _create_community_preceptors(self, reason):
        role_profiles = (
            (True, False),
            (False, False),
            (True, True),
        )
        recurrence_profiles = (
            (0, [1, 3], ["AM", "PM"]),
            (2, [2, 4], ["PM"]),
            (4, [1, 3, 5], ["AM"]),
        )
        period = BASELINE_START.isoformat()
        role_names = self._ranked_names("community-role-profile", period)
        recurrence_names = self._ranked_names("community-recurrence-profile", period)
        physicians = {}
        for name, (faculty_equivalent, casual_rounder) in zip(
            role_names, role_profiles, strict=True
        ):
            physicians[name] = create_physician(
                display_name=name,
                scheduler=self.actor,
                reason=reason,
            )
            add_role(
                physician=physicians[name],
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start=BASELINE_START,
                effective_end=None,
                faculty_equivalent=faculty_equivalent,
                scheduler=self.actor,
                reason=reason,
            )
            if casual_rounder:
                add_role(
                    physician=physicians[name],
                    role=EffectiveRole.Role.CASUAL_ROUNDER,
                    effective_start=BASELINE_START,
                    effective_end=None,
                    scheduler=self.actor,
                    reason=reason,
                )

        for name, (weekday, ordinals, periods) in zip(
            recurrence_names, recurrence_profiles, strict=True
        ):
            create_community_preceptor_rule(
                physician=physicians[name],
                weekday=weekday,
                ordinals=ordinals,
                periods=periods,
                effective_start=BASELINE_START,
                effective_end=None,
                scheduler=self.actor,
                reason=reason,
            )
        return tuple(physicians[name] for name in COMMUNITY_PRECEPTOR_NAMES)

    def _ranked_names(self, domain, period):
        return tuple(
            sorted(
                COMMUNITY_PRECEPTOR_NAMES,
                key=lambda name: (self._stable_number(domain, name, period), name),
            )
        )

    def _stable_number(self, domain, physician, period):
        key = "\x1f".join((self.seed, str(domain), str(physician), str(period))).encode()
        return int.from_bytes(hashlib.blake2b(key, digest_size=16).digest(), "big")


def _require_empty_scheduling_domain():
    populated_models = [
        model._meta.label
        for model in apps.get_app_config("schedules").get_models()
        if model._meta.label not in EXCLUDED_EMPTY_DOMAIN_MODELS
        and (
            model != CustomAvailabilityCategory
            or model.objects.exclude(system_key=CustomAvailabilityCategory.SystemKey.ADMIN).exists()
        )
        and model.objects.exists()
    ]
    if populated_models:
        raise ValidationError(
            "Synthetic scenario requires an empty scheduling domain; found "
            + ", ".join(sorted(populated_models))
            + "."
        )


def _month_end(on_date):
    first_next_month = (on_date.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next_month - timedelta(days=1)


def _weekdays_between(start_date, end_date):
    return tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
        if (start_date + timedelta(days=offset)).weekday() < 5
    )


def _week_starts_between(start_date, end_date):
    first_week = work_week_start(start_date)
    last_week = work_week_start(end_date)
    return tuple(
        first_week + timedelta(days=7 * offset)
        for offset in range((last_week - first_week).days // 7 + 1)
    )
