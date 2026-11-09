"""Semantic values for the read-only Master Schedule calendar."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .services.holidays import HolidayProjection


@dataclass(frozen=True)
class MasterScheduleFactPresentation:
    label: str
    detail: str
    attention: str = ""


@dataclass(frozen=True)
class MasterScheduleFindingPresentation:
    message: str
    severity: str
    sources: tuple[MasterScheduleFactPresentation, ...] = ()


@dataclass(frozen=True)
class MasterScheduleGridCellPresentation:
    facts: tuple[MasterScheduleFactPresentation, ...] = ()
    findings: tuple[MasterScheduleFindingPresentation, ...] = ()


@dataclass(frozen=True)
class MasterScheduleGridDayPresentation:
    iso_date: str
    label: str
    holidays: tuple[HolidayProjection, ...] = ()


@dataclass(frozen=True)
class MasterScheduleGridRowPresentation:
    key: str
    label: str
    cells: tuple[MasterScheduleGridCellPresentation, ...]
    is_within_focus: bool = True


@dataclass(frozen=True)
class MasterScheduleGridGroupPresentation:
    key: str
    label: str
    rows: tuple[MasterScheduleGridRowPresentation, ...]
    closed_day_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class MasterScheduleGridPresentation:
    label: str
    days: tuple[MasterScheduleGridDayPresentation, ...]
    groups: tuple[MasterScheduleGridGroupPresentation, ...]
    holiday_day_indexes: tuple[int, ...] = ()
