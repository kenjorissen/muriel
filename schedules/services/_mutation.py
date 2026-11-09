"""Narrow shared authority for serialized scheduling mutations."""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.db import transaction

from ..models.mutation import SchedulingMutationAuthority
from .operational_attention import captured_scheduling_clock

_scheduling_mutation_lock = threading.RLock()
_operation = ContextVar("scheduling_operation", default=None)


def capture_calendar_range(start, end=None):
    """Register explicit dated ownership before its first write, including old/new dates."""
    operation = _operation.get()
    if operation is None or operation["recorder"] is None:
        raise RuntimeError("Calendar ownership must be captured inside a scheduling operation.")
    operation["recorder"].capture_range(start, start if end is None else end)


@contextmanager
def _serialized_scheduling_mutation(*, record=False):
    """Serialize short scheduling writes across assignments and their source facts."""
    # SQLite ignores SELECT FOR UPDATE, so retain the same-process lock for development.
    with _scheduling_mutation_lock:
        with transaction.atomic():
            outermost = _operation.get() is None
            if outermost:
                authority, _ = SchedulingMutationAuthority.objects.get_or_create(singleton=True)
                SchedulingMutationAuthority.objects.select_for_update().get(pk=authority.pk)
            with captured_scheduling_clock() as now:
                operation = _operation.get() or {"recorder": None}
                token = _operation.set(operation)
                try:
                    if record and operation["recorder"] is None:
                        from .month_edits import MonthEditRecorder

                        operation["recorder"] = MonthEditRecorder(now)
                    yield
                    if outermost:
                        from .finding_acknowledgements import retire_inapplicable_acknowledgements

                        retire_inapplicable_acknowledgements()
                        if operation["recorder"] is not None:
                            operation["recorder"].record()
                finally:
                    _operation.reset(token)


@contextmanager
def scheduling_operation():
    """One atomic user operation spanning multiple scheduling service writes."""
    with _serialized_scheduling_mutation(record=True):
        yield


@contextmanager
def serialized_scheduling_state():
    """Read a coherent scheduling state while relevant writes are briefly serialized."""
    with _serialized_scheduling_mutation():
        yield


def scheduling_mutation(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        with scheduling_operation():
            return operation(*args, **kwargs)

    return wrapped


def require_scheduler(scheduler):
    if not scheduler or not scheduler.is_authenticated:
        raise PermissionDenied("A signed-in scheduler must record scheduling changes.")


def can_manage_scheduling_administration(scheduler):
    return bool(
        scheduler
        and scheduler.is_authenticated
        and scheduler.has_perm("schedules.manage_scheduling_administration")
    )


def require_scheduling_administrator(scheduler):
    require_scheduler(scheduler)
    if not can_manage_scheduling_administration(scheduler):
        raise PermissionDenied("Scheduling administrator permission is required.")


def scheduling_administration_mutation(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        require_scheduling_administrator(kwargs.get("scheduler"))
        with scheduling_operation():
            return operation(*args, **kwargs)

    return wrapped


def normalize_note(note):
    return note.strip() if note else ""
