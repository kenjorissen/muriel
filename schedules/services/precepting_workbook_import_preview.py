"""Short-lived browser tokens for Precepting workbook previews."""

from datetime import date

from django.conf import settings
from django.core import signing

from .precepting_workbook_import import (
    PreceptingWorkbookData,
    SourceAssignment,
    SourceLane,
)

_SIGNING_SALT = "schedules.precepting-workbook-import-preview"


class PreceptingWorkbookImportPreviewError(ValueError):
    """A browser preview cannot safely be applied."""


def _assignment_payload(item):
    return {
        "on_date": item.on_date.isoformat(),
        "period_of_day": item.period_of_day,
        "kind": item.kind,
        "name": item.name,
        "sheet": item.sheet,
        "coordinate": item.coordinate,
        "order": item.order,
        "position": item.position,
        "responsibility": item.responsibility,
    }


def _assignment_from_payload(payload):
    return SourceAssignment(
        on_date=date.fromisoformat(payload["on_date"]),
        period_of_day=payload["period_of_day"],
        kind=payload["kind"],
        name=payload["name"],
        sheet=payload["sheet"],
        coordinate=payload["coordinate"],
        order=payload["order"],
        position=payload["position"],
        responsibility=payload["responsibility"],
    )


def _lane_payload(item):
    return {
        "on_date": item.on_date.isoformat(),
        "period_of_day": item.period_of_day,
        "kind": item.kind,
        "sheet": item.sheet,
        "coordinate": item.coordinate,
    }


def _lane_from_payload(payload):
    return SourceLane(
        on_date=date.fromisoformat(payload["on_date"]),
        period_of_day=payload["period_of_day"],
        kind=payload["kind"],
        sheet=payload["sheet"],
        coordinate=payload["coordinate"],
    )


def _data_payload(data):
    return {
        "source_name": data.source_name,
        "assignments": [_assignment_payload(item) for item in data.assignments],
        "lanes": [_lane_payload(item) for item in data.lanes],
        "ignored": list(data.ignored),
        "blockers": list(data.blockers),
    }


def _data_from_payload(payload):
    return PreceptingWorkbookData(
        source_name=payload["source_name"],
        assignments=tuple(_assignment_from_payload(item) for item in payload["assignments"]),
        lanes=tuple(_lane_from_payload(item) for item in payload["lanes"]),
        ignored=tuple(payload["ignored"]),
        blockers=tuple(payload["blockers"]),
    )


def create_precepting_workbook_import_preview_token(*, data, user, workflow):
    """Sign normalized workbook input without retaining the uploaded workbook."""
    return signing.dumps(
        {"user_id": user.pk, "workflow": workflow, "data": _data_payload(data)},
        salt=_SIGNING_SALT,
        compress=True,
    )


def load_precepting_workbook_import_preview_token(*, token, user, workflow):
    """Restore a token only for its original Scheduling Administrator and workflow."""
    try:
        payload = signing.loads(
            token,
            salt=_SIGNING_SALT,
            max_age=settings.PRECEPTING_WORKBOOK_IMPORT_PREVIEW_MAX_AGE,
        )
        if payload["user_id"] != user.pk or payload["workflow"] != workflow:
            raise PreceptingWorkbookImportPreviewError
        return _data_from_payload(payload["data"])
    except (
        signing.BadSignature,
        KeyError,
        TypeError,
        ValueError,
        PreceptingWorkbookImportPreviewError,
    ):
        raise PreceptingWorkbookImportPreviewError(
            "This preview is no longer valid. Preview the workbook again."
        ) from None
