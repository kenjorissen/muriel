"""Organization-local boundary for current operational scheduling attention."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

ORGANIZATION_TIME_ZONE = ZoneInfo("America/Chicago")
_effective_now = ContextVar("scheduling_effective_now", default=None)


@contextmanager
def captured_scheduling_clock():
    """Keep an operation's effective date fixed, including across local midnight."""
    now = _effective_now.get() or timezone.now()
    token = _effective_now.set(now)
    try:
        yield now
    finally:
        _effective_now.reset(token)


def organization_today():
    """Return today's calendar date at the organization, not at the caller."""
    now = _effective_now.get() or timezone.now()
    return now.astimezone(ORGANIZATION_TIME_ZONE).date()


def is_operational_date(on_date):
    return on_date >= organization_today()


def is_operational_date_range(start_date, end_date):
    return end_date >= organization_today()


def is_operational_preceptor_week(week_start):
    """A preceptor workweek remains current through its Friday sessions."""
    return week_start + timedelta(days=4) >= organization_today()
