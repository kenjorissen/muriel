"""Short-lived browser tokens for Academic Event import previews."""

from datetime import date

from django.conf import settings
from django.core import signing

from .academic_event_definition_import import (
    DefinitionImportData,
    SourceDefinition,
    SourceMonthlyRule,
    UnsupportedSourceDefinition,
)

_SIGNING_SALT = "schedules.academic-event-import-preview"


class AcademicEventImportPreviewError(ValueError):
    """A browser preview cannot safely be applied."""


def _source_payload(source):
    return {
        "row_number": source.row_number,
        "base_name": source.base_name,
        "name_key": source.name_key,
        "expected_quarter": source.expected_quarter,
        "default_duration_days": source.default_duration_days,
        "typically_scheduled": source.typically_scheduled,
        "scheduling_mode": source.scheduling_mode,
        "default_precepting_impact": source.default_precepting_impact,
        "all_available_faculty": source.all_available_faculty,
        "monthly_rule": (
            {
                "weekday": source.monthly_rule.weekday,
                "ordinals": source.monthly_rule.ordinals,
                "effective_start": source.monthly_rule.effective_start.isoformat(),
                "effective_end": (
                    source.monthly_rule.effective_end.isoformat()
                    if source.monthly_rule.effective_end
                    else None
                ),
            }
            if source.monthly_rule
            else None
        ),
        "start_dates": [value.isoformat() for value in source.start_dates],
        "end_dates": [value.isoformat() for value in source.end_dates],
        "date_ranges": [[start.isoformat(), end.isoformat()] for start, end in source.date_ranges],
        "faculty": source.faculty,
        "residents": source.residents,
        "suffix": source.suffix,
        "note": source.note,
        "period": source.period,
    }


def _source_from_payload(payload):
    monthly_rule = payload["monthly_rule"]
    return SourceDefinition(
        row_number=payload["row_number"],
        base_name=payload["base_name"],
        name_key=payload["name_key"],
        expected_quarter=payload["expected_quarter"],
        default_duration_days=payload["default_duration_days"],
        typically_scheduled=payload["typically_scheduled"],
        scheduling_mode=payload["scheduling_mode"],
        default_precepting_impact=payload["default_precepting_impact"],
        all_available_faculty=payload["all_available_faculty"],
        monthly_rule=(
            SourceMonthlyRule(
                weekday=monthly_rule["weekday"],
                ordinals=tuple(monthly_rule["ordinals"]),
                effective_start=date.fromisoformat(monthly_rule["effective_start"]),
                effective_end=(
                    date.fromisoformat(monthly_rule["effective_end"])
                    if monthly_rule["effective_end"]
                    else None
                ),
            )
            if monthly_rule
            else None
        ),
        start_dates=tuple(date.fromisoformat(value) for value in payload["start_dates"]),
        end_dates=tuple(date.fromisoformat(value) for value in payload["end_dates"]),
        date_ranges=tuple(
            (date.fromisoformat(start), date.fromisoformat(end))
            for start, end in payload["date_ranges"]
        ),
        faculty=payload["faculty"],
        residents=payload["residents"],
        suffix=payload["suffix"],
        note=payload["note"],
        period=payload["period"],
    )


def _data_payload(data):
    return {
        "source_name": data.source_name,
        "academic_year": data.academic_year,
        "definitions": [_source_payload(source) for source in data.definitions],
        "unsupported": [
            {"source": _source_payload(item.source), "reason": item.reason}
            for item in data.unsupported
        ],
    }


def _data_from_payload(payload):
    return DefinitionImportData(
        source_name=payload["source_name"],
        academic_year=payload["academic_year"],
        definitions=tuple(_source_from_payload(source) for source in payload["definitions"]),
        unsupported=tuple(
            UnsupportedSourceDefinition(_source_from_payload(item["source"]), item["reason"])
            for item in payload["unsupported"]
        ),
    )


def create_academic_event_import_preview_token(*, data, user, workflow):
    """Sign normalized workbook input without retaining the uploaded workbook."""
    return signing.dumps(
        {"user_id": user.pk, "workflow": workflow, "data": _data_payload(data)},
        salt=_SIGNING_SALT,
        compress=True,
    )


def load_academic_event_import_preview_token(*, token, user, workflow):
    """Restore a token only for its original user and import workflow."""
    try:
        payload = signing.loads(
            token,
            salt=_SIGNING_SALT,
            max_age=settings.ACADEMIC_EVENT_IMPORT_PREVIEW_MAX_AGE,
        )
        if payload["user_id"] != user.pk or payload["workflow"] != workflow:
            raise AcademicEventImportPreviewError
        return _data_from_payload(payload["data"])
    except (
        signing.BadSignature,
        KeyError,
        TypeError,
        ValueError,
        AcademicEventImportPreviewError,
    ):
        raise AcademicEventImportPreviewError(
            "This preview is no longer valid. Preview the workbook again."
        ) from None
