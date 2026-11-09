"""Retained acknowledgements for live, computed scheduling findings."""

import json
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from ..models import FindingAcknowledgement, InterviewShift, PreceptorSession
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)


@dataclass(frozen=True)
class FindingReadiness:
    """The exception and work status of one current computed finding."""

    finding: object
    acknowledgement: FindingAcknowledgement | None

    @property
    def classification(self):
        if getattr(self.finding, "severity", None) != "hard":
            return "non-blocking"
        return "acknowledged" if self.acknowledgement else "unacknowledged"

    @property
    def is_blocking(self):
        return self.classification == "unacknowledged"


@dataclass(frozen=True)
class FindingReadinessResult:
    """One acknowledgement-aware classification shared by readiness consumers."""

    entries: tuple[FindingReadiness, ...]

    @property
    def unacknowledged_hard_findings(self):
        return tuple(entry.finding for entry in self.entries if entry.is_blocking)

    @property
    def acknowledged_hard_findings(self):
        return tuple(
            entry.finding for entry in self.entries if entry.classification == "acknowledged"
        )

    @property
    def has_blocking_findings(self):
        return any(entry.is_blocking for entry in self.entries)

    def acknowledgement_for(self, finding):
        return next(
            (entry.acknowledgement for entry in self.entries if entry.finding is finding), None
        )

    def entries_for(self, findings):
        finding_ids = {id(finding) for finding in findings}
        return tuple(entry for entry in self.entries if id(entry.finding) in finding_ids)


def readiness_for_findings(findings):
    """Classify live findings from their retained acknowledgement contract."""
    findings = tuple(findings)
    acknowledgements = acknowledgements_for_findings(findings)
    return FindingReadinessResult(
        tuple(
            FindingReadiness(
                finding,
                (
                    acknowledgements.get(finding_identity(finding).key)
                    if getattr(finding, "severity", None) == "hard"
                    else None
                ),
            )
            for finding in findings
        )
    )


@dataclass(frozen=True)
class FindingIdentity:
    """The non-presentational identity of one finding occurrence."""

    owner: dict
    occurrence: dict
    code: str
    material_values: dict

    def as_dict(self):
        return {
            "owner": self.owner,
            "occurrence": self.occurrence,
            "code": self.code,
            "material_values": self.material_values,
        }

    @property
    def key(self):
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), default=str)


def finding_identity(finding):
    """Return a structured identity without depending on rendered finding prose."""
    if _is_draft_finding(finding):
        return _draft_finding_identity(finding)
    if _is_interview_finding(finding):
        return _interview_finding_identity(finding)
    raise ValidationError("Only scheduling findings can be acknowledged.")


def acknowledgement_for_finding(finding):
    """Return the retained acknowledgement only when this live hard finding has one."""
    if getattr(finding, "severity", None) != "hard":
        return None
    identity = finding_identity(finding)
    return FindingAcknowledgement.objects.filter(
        identity_key=identity.key, no_longer_applies_at__isnull=True
    ).first()


def acknowledgements_for_findings(findings):
    """Return current acknowledgement records keyed by stable finding identity."""
    identity_keys = {
        finding_identity(finding).key
        for finding in findings
        if getattr(finding, "severity", None) == "hard"
    }
    records = FindingAcknowledgement.objects.filter(
        identity_key__in=identity_keys, no_longer_applies_at__isnull=True
    )
    return {record.identity_key: record for record in records}


@scheduling_mutation
def acknowledge_finding(*, finding, scheduler, reason):
    """Retain one current hard finding acknowledgement, idempotently."""
    require_scheduler(scheduler)
    reason = normalize_note(reason)
    if not reason:
        raise ValidationError({"reason": "A nonblank reason is required."})
    if getattr(finding, "severity", None) != "hard":
        raise ValidationError("Only current hard findings can be acknowledged.")

    if _is_draft_finding(finding):
        if finding.session is not None:
            capture_calendar_range(finding.session.date)
        else:
            capture_calendar_range(finding.week, finding.week + timedelta(days=4))
    requested_identity = finding_identity(finding)
    current_finding = _current_finding_for_identity(requested_identity)
    if current_finding is None:
        raise ValidationError("This finding no longer applies and cannot be acknowledged.")

    acknowledgement, _ = FindingAcknowledgement.objects.get_or_create(
        identity_key=requested_identity.key,
        no_longer_applies_at__isnull=True,
        defaults={
            "identity": requested_identity.as_dict(),
            "reason": reason,
            "acknowledged_by": scheduler,
        },
    )
    return acknowledgement


@scheduling_mutation
def remove_finding_acknowledgement(*, finding, acknowledgement_id, scheduler):
    """Retire exactly one live acknowledgement at a scheduler's direction."""
    require_scheduler(scheduler)
    if getattr(finding, "severity", None) != "hard":
        raise ValidationError("Only current hard finding overrides can be removed.")

    if _is_draft_finding(finding):
        if finding.session is not None:
            capture_calendar_range(finding.session.date)
        else:
            capture_calendar_range(finding.week, finding.week + timedelta(days=4))
    requested_identity = finding_identity(finding)
    if _current_finding_for_identity(requested_identity) is None:
        raise ValidationError("That override is no longer current and cannot be removed.")

    try:
        acknowledgement_id = int(acknowledgement_id)
    except TypeError, ValueError:
        raise ValidationError("That override is no longer current and cannot be removed.") from None

    acknowledgement = (
        FindingAcknowledgement.objects.select_for_update()
        .filter(
            pk=acknowledgement_id,
            identity_key=requested_identity.key,
            no_longer_applies_at__isnull=True,
        )
        .first()
    )
    if acknowledgement is None:
        raise ValidationError("That override is no longer current and cannot be removed.")

    removed_at = timezone.now()
    acknowledgement.no_longer_applies_at = removed_at
    acknowledgement.removed_by = scheduler
    acknowledgement.removed_at = removed_at
    acknowledgement.save(update_fields=("no_longer_applies_at", "removed_by", "removed_at"))
    return acknowledgement


def retire_inapplicable_acknowledgements():
    """Retain, rather than delete, acknowledgements whose live finding ended or changed."""
    for acknowledgement in FindingAcknowledgement.objects.filter(no_longer_applies_at__isnull=True):
        identity = _identity_from_dict(acknowledgement.identity)
        if _current_finding_for_identity(identity) is None:
            acknowledgement.no_longer_applies_at = timezone.now()
            acknowledgement.save(update_fields=("no_longer_applies_at",))


def _current_finding_for_identity(identity):
    for finding in _findings_for_owner(identity.owner):
        if getattr(finding, "severity", None) == "hard" and finding_identity(finding) == identity:
            return finding
    return None


def _findings_for_owner(owner):
    kind = owner["kind"]
    if kind == "preceptor_session":
        from .preceptors import draft_findings_for_session

        session = PreceptorSession.objects.filter(pk=owner["id"]).first()
        return () if session is None else draft_findings_for_session(session).findings
    if kind == "preceptor_week":
        from .preceptors import draft_findings_for_week

        return draft_findings_for_week(date.fromisoformat(owner["week"])).findings
    if kind == "interview_shift":
        from .interviews import interview_findings_for_shift

        shift = InterviewShift.objects.filter(pk=owner["id"]).first()
        return () if shift is None else interview_findings_for_shift(shift).findings
    raise ValidationError("Finding acknowledgement has an unknown schedule owner.")


def _draft_finding_identity(finding):
    if finding.session is None:
        owner = {"kind": "preceptor_week", "week": finding.week.isoformat()}
        occurrence = {"week_start": finding.week.isoformat()}
    else:
        owner = {"kind": "preceptor_session", "id": finding.session.pk}
        occurrence = {
            "date": finding.session.date.isoformat(),
            "period_of_day": finding.session.period_of_day,
            "position": finding.explanation.get("position"),
        }
    return FindingIdentity(
        owner=owner,
        occurrence=occurrence,
        code=finding.code,
        material_values=_draft_material_values(finding),
    )


def _draft_material_values(finding):
    explanation = finding.explanation
    if finding.code == "insufficient_faculty_equivalent_coverage":
        return finding.identity_values
    return (
        _material_values(
            explanation,
            "assignment_id",
            "community_preceptor_rule_id",
            "preceptor_assignment_id",
        )
        | _availability_source_values(finding)
        | _physician_values(finding)
    )


def _interview_finding_identity(finding):
    return FindingIdentity(
        owner={"kind": "interview_shift", "id": finding.shift.pk},
        occurrence={"date": finding.shift.date.isoformat(), "period_of_day": "AM"},
        code=finding.code,
        material_values=_material_values(
            finding.explanation, "physician_id", "preceptor_assignment_id"
        )
        | _availability_source_values(finding)
        | _physician_values(finding),
    )


def _material_values(explanation, *keys):
    return {
        key: explanation[key] for key in keys if key in explanation and explanation[key] is not None
    }


def _physician_values(finding):
    if not finding.physicians:
        return {}
    return {"physician_ids": sorted(physician.pk for physician in finding.physicians)}


def _availability_source_values(finding):
    """Identify the durable facts that make an assigned clinician unavailable."""
    if finding.code not in {"not_available_assignment", "not_available_late_doc"}:
        return {}
    return {
        "availability_sources": [
            {"source_type": source.source_type, "source_id": source.source_id}
            for source in finding.sources
        ]
    }


def _identity_from_dict(value):
    return FindingIdentity(
        owner=value["owner"],
        occurrence=value["occurrence"],
        code=value["code"],
        material_values=value["material_values"],
    )


def _is_draft_finding(finding):
    from .preceptors import DraftFinding

    return isinstance(finding, DraftFinding)


def _is_interview_finding(finding):
    from .interviews import InterviewFinding

    return isinstance(finding, InterviewFinding)
