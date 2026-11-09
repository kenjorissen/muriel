"""Meaningful calendar values, shared by rendering and month edit recording.

Links, source identifiers, history notes and replacement choices are intentionally
outside these values. Findings still come from the ordinary scheduling policy.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AssignedIdentity:
    physician_id: int
    display_name: str
    is_locked: bool


def assigned_identity(assignment):
    return AssignedIdentity(
        assignment.physician_id,
        assignment.physician.display_name,
        getattr(assignment, "is_locked", False),
    )


def finding_source_content(source):
    """The detailed finding's source label, without internal record numbering."""
    if source.source_type in {"academic_event", "normal_rounder"}:
        return source.label
    label = {
        "faculty_template": "Faculty Schedule template",
        "monthly_availability_rule": "Monthly scheduling rule",
        "community_preceptor_rule": "Community Preceptor rule",
        "vacation": "Vacation",
        "external_patient_care": "External Patient Care role",
        "casual_rounder_rule": "Casual-rounder rule",
    }[source.source_type]
    if source.selection and "category_name" in source.selection:
        return f"{label}: {source.selection['category_name']}"
    return label


@dataclass(frozen=True)
class FindingContent:
    message: str
    severity: str
    physicians: tuple[str, ...]
    sources: tuple[str, ...]


def finding_content(finding):
    return FindingContent(
        finding.message,
        finding.severity,
        tuple(sorted(physician.display_name for physician in finding.physicians)),
        tuple(sorted({finding_source_content(source) for source in finding.sources})),
    )
