"""Compact, shared presentation for operational Precepting findings."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError

from .calendar_presentation import CalendarFindingPresentation, CalendarStatusPresentation
from .services.finding_acknowledgements import acknowledgements_for_findings, finding_identity


@dataclass(frozen=True)
class PreceptingFindingTarget:
    """One calendar target and its deduplicated compact finding marker."""

    date: date
    period: str
    position: str | None
    marker: CalendarFindingPresentation


_MARKERS = {
    "hard": ("!", "Hard finding"),
    "exception": ("!", "Acknowledged exception"),
    "preference": ("?", "Preference finding"),
}

_PRECEPTING_CALENDAR_LEGEND = (
    CalendarStatusPresentation(
        "hard",
        "!",
        "Hard finding",
        "Requires resolution.",
        "calendar-finding-indicator--hard",
    ),
    CalendarStatusPresentation(
        "exception",
        "!",
        "Acknowledged exception",
        "The rule remains unmet but this specific finding is acknowledged.",
        "calendar-finding-indicator--exception",
    ),
    CalendarStatusPresentation(
        "preference",
        "?",
        "Preference finding",
        "Needs review but does not block scheduling.",
        "calendar-finding-indicator--preference",
    ),
)


def precepting_finding_targets(session, findings):
    """Project operational findings once for their day, period, and position targets."""
    findings = tuple(findings)
    try:
        acknowledgements = acknowledgements_for_findings(findings)
    except AttributeError, ValidationError:
        # Presentation fixtures may intentionally use partial, non-persisted findings.
        acknowledgements = {}
    findings_by_position = {}
    for finding in findings:
        findings_by_position.setdefault(finding.explanation.get("position"), []).append(finding)
    return tuple(
        PreceptingFindingTarget(
            session.date,
            session.period_of_day,
            position,
            marker_for_findings(findings_for_target, acknowledgements),
        )
        for position, findings_for_target in findings_by_position.items()
    )


def marker_for_position(targets, position):
    """Return the already-projected marker for a particular assignment position."""
    return next((target.marker for target in targets if target.position == position), None)


def precepting_calendar_legend():
    """Return the complete ordered legend used by both Precepting calendar views."""
    return _PRECEPTING_CALENDAR_LEGEND


def marker_for_findings(findings, acknowledgements=None):
    """Return one compact marker that preserves acknowledgement status."""

    findings = tuple(findings)
    if acknowledgements is None:
        try:
            acknowledgements = acknowledgements_for_findings(findings)
        except AttributeError, ValidationError:
            # Presentation fixtures may intentionally use partial, non-persisted findings.
            acknowledgements = {}

    def is_acknowledged(finding):
        return bool(acknowledgements) and finding_identity(finding).key in acknowledgements

    unacknowledged_hard_findings = tuple(
        finding
        for finding in findings
        if finding.severity == "hard" and not is_acknowledged(finding)
    )
    acknowledged_hard_findings = tuple(
        finding for finding in findings if finding.severity == "hard" and is_acknowledged(finding)
    )
    severity = (
        "hard"
        if unacknowledged_hard_findings
        else "exception"
        if acknowledged_hard_findings
        else "preference"
    )
    labels = tuple(
        dict.fromkeys(
            (
                # TEXT-CONTRACT: acknowledged-hard-finding-overridden-suffix —
                # Compact calendar popovers distinguish acknowledged hard findings with
                # the exact ` — overridden` suffix on their labels. Its punctuation and
                # whitespace matter; the finding label before it is not part of this contract.
                # Protected by schedules/tests/test_scheduling_exceptions.py:
                # PreceptingFindingMarkerPresentationTests.test_compact_marker_marks_only_acknowledged_hard_labels_as_overridden.
                f"{finding.label} — overridden"
                if finding in acknowledged_hard_findings
                else finding.label
            )
            for finding in findings
        )
    )
    tones = tuple(
        dict.fromkeys(
            _MARKERS["exception" if finding in acknowledged_hard_findings else finding.severity][1]
            for finding in findings
        )
    )
    symbol, _ = _MARKERS[severity]
    return CalendarFindingPresentation(
        severity,
        symbol,
        f"{' and '.join(tones)}: {', '.join(labels)}.",
        labels,
    )
