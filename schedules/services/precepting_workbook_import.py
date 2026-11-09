"""Read-only preview planning for the manual calendar-year Precepting workbook."""

import calendar
import colorsys
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile

from django.db import transaction
from django.db.models import Q
from openpyxl import load_workbook
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.utils.exceptions import InvalidFileException

from ..academic_years import academic_year_for_date
from ..models.interviews import InterviewShift
from ..models.late_docs import LateDocAssignment
from ..models.physicians import EffectiveRole, Physician
from ..models.preceptors import PreceptorAssignment, PreceptorSession
from ._mutation import capture_calendar_range, scheduling_administration_mutation
from .interviews import assign_interview_shift, create_interview_shift, remove_interview_shift
from .late_docs import assign_late_doc, remove_late_doc_assignment
from .preceptors import (
    assign_preceptor,
    ensure_precepting_horizon_sessions,
    remove_preceptor_assignment,
    work_week_start,
)
from .workbook_upload import WorkbookImportError, validate_workbook_archive

MONTH_SHEETS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_IGNORED_ASSIGNMENT_TEXT = (
    "holiday",
    "new year",
    "memorial day",
    "resident",
    "no clinic",
    "clinic closed",
    "closed",
)
_DRAWING_NAMESPACE = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class PreceptingWorkbookImportError(ValueError):
    """The workbook has no unambiguous safe Precepting interpretation."""


@dataclass(frozen=True)
class SourceAssignment:
    on_date: date
    period_of_day: str
    kind: str
    name: str
    sheet: str
    coordinate: str
    order: int
    position: int
    responsibility: str = "primary"


@dataclass(frozen=True)
class SourceLane:
    on_date: date
    period_of_day: str
    kind: str
    sheet: str
    coordinate: str


@dataclass(frozen=True)
class PreceptingWorkbookData:
    source_name: str
    assignments: tuple[SourceAssignment, ...]
    lanes: tuple[SourceLane, ...]
    ignored: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class NameResolution:
    name: str
    kind: str
    state: str
    physicians: tuple[Physician, ...]


@dataclass(frozen=True)
class PlannedPreceptingAssignment:
    physician: Physician
    responsibility: str
    primary_position: int | None
    kind: str


@dataclass(frozen=True)
class PreceptingPreview:
    on_date: date
    period_of_day: str
    assignments: tuple[PlannedPreceptingAssignment, ...]
    existing: tuple[PreceptorAssignment, ...]
    state: str


@dataclass(frozen=True)
class LateDocPreview:
    on_date: date
    physician: Physician | None
    existing: LateDocAssignment | None
    state: str


@dataclass(frozen=True)
class InterviewPreview:
    on_date: date
    physician: Physician | None
    existing: InterviewShift | None
    state: str


@dataclass(frozen=True)
class PreceptingWorkbookImportPlan:
    data: PreceptingWorkbookData
    precepting: tuple[PreceptingPreview, ...]
    late_docs: tuple[LateDocPreview, ...]
    interviews: tuple[InterviewPreview, ...]
    unique_names: tuple[NameResolution, ...]
    ambiguous_names: tuple[NameResolution, ...]
    unmatched_names: tuple[NameResolution, ...]
    blockers: tuple[str, ...]

    @property
    def exact_precepting(self):
        return tuple(item for item in self.precepting if item.state == "exact")

    @property
    def new_precepting(self):
        return tuple(item for item in self.precepting if item.state == "new")

    @property
    def corrected_precepting(self):
        return tuple(item for item in self.precepting if item.state == "corrected")

    @property
    def removed_precepting(self):
        return tuple(item for item in self.precepting if item.state == "removed")

    @property
    def exact_interviews(self):
        return tuple(item for item in self.interviews if item.state == "exact")

    @property
    def new_interviews(self):
        return tuple(item for item in self.interviews if item.state == "new")

    @property
    def corrected_interviews(self):
        return tuple(item for item in self.interviews if item.state == "corrected")

    @property
    def removed_interviews(self):
        return tuple(item for item in self.interviews if item.state == "removed")


def _clean_text(value):
    return " ".join(str(value or "").split())


def _name_key(value):
    value = _clean_text(value)
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first} {last}"
    return re.sub(r"[^0-9a-z]+", " ", value.casefold()).strip()


# TODO: Generalize aliases only if a demonstrated need arises.
_PRECEPTING_NAME_ALIASES = {}


def _label(value):
    return re.sub(r"[^a-z]+", " ", _clean_text(value).casefold()).strip()


@lru_cache
def _theme_colors(theme):
    root = ElementTree.fromstring(theme)
    scheme = root.find(f".//{_DRAWING_NAMESPACE}clrScheme")
    if scheme is None:
        raise ValueError("has no color scheme")
    values = []
    for entry in scheme:
        color = next(iter(entry), None)
        value = color.get("lastClr") or color.get("val") if color is not None else None
        if value is None or not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
            raise ValueError("has an unreadable color scheme")
        values.append(value.upper())
    return tuple(values)


def _theme_rgb(cell, color):
    try:
        theme = cell.parent.parent.loaded_theme
        if theme is None:
            raise ValueError("is unavailable")
        value = _theme_colors(theme)[color.theme]
        tint = float(color.tint or 0)
        if not -1 <= tint <= 1:
            raise ValueError("has an invalid tint")
        red, green, blue = (int(value[index : index + 2], 16) / 255 for index in (0, 2, 4))
        hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
        lightness = lightness * (1 + tint) if tint < 0 else lightness + (1 - lightness) * tint
        return "".join(
            f"{round(component * 255):02X}"
            for component in colorsys.hls_to_rgb(hue, lightness, saturation)
        )
    except (ElementTree.ParseError, IndexError, TypeError, ValueError) as error:
        raise PreceptingWorkbookImportError(
            f"{cell.parent.title}!{cell.coordinate} has an unreadable theme color."
        ) from error


def _cell_rgb(cell):
    colors = []
    if cell.fill.fill_type == "solid":
        colors.append(cell.fill.fgColor)
    if cell.font.color is not None:
        colors.append(cell.font.color)
    values = []
    for color in colors:
        if color.type == "rgb" and color.rgb:
            values.append(color.rgb[-6:])
        elif color.type == "indexed" and color.indexed is not None:
            values.append(COLOR_INDEX[color.indexed][-6:])
        elif color.type == "theme" and color.theme is not None:
            values.append(_theme_rgb(cell, color))
    return tuple(dict.fromkeys(values))


def _color_kind(cell):
    kinds = set()
    for value in _cell_rgb(cell):
        try:
            red, green, blue = (int(value[index : index + 2], 16) / 255 for index in (0, 2, 4))
        except ValueError as error:
            raise PreceptingWorkbookImportError(
                f"{cell.parent.title}!{cell.coordinate} has an unreadable visible color."
            ) from error
        hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)
        hue *= 360
        if saturation < 0.25 or brightness < 0.25:
            continue
        if 70 <= hue <= 170:
            kinds.add("interviewer")
        elif 20 <= hue <= 70:
            kinds.add("community")
        else:
            raise PreceptingWorkbookImportError(
                f"{cell.parent.title}!{cell.coordinate} has unsupported assignment color {value}."
            )
    if len(kinds) > 1:
        raise PreceptingWorkbookImportError(
            f"{cell.parent.title}!{cell.coordinate} has conflicting assignment colors."
        )
    return next(iter(kinds), "preceptor")


def _worksheet_year(worksheet, month):
    years = set()
    for row in worksheet.iter_rows(max_col=5):
        for cell in row:
            value = cell.value
            if isinstance(value, datetime):
                if value.month == month:
                    years.add(value.year)
            elif isinstance(value, date):
                if value.month == month:
                    years.add(value.year)
            elif isinstance(value, str):
                years.update(
                    int(year) for year in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", value)
                )
    if len(years) != 1:
        raise PreceptingWorkbookImportError(
            f"{worksheet.title} must contain exactly one calendar year, found {sorted(years)!r}."
        )
    return years.pop()


def _cell_date(value, year, month):
    day_count = calendar.monthrange(year, month)[1]
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date) and value.month == month and value.year == year:
        return value
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == int(value)
        and 1 <= value <= day_count
    ):
        return date(year, month, int(value))
    return None


def _is_ignored_assignment_text(value):
    value = _label(value)
    return any(marker in value for marker in _IGNORED_ASSIGNMENT_TEXT)


def _week_dates(worksheet, row, year, month):
    dates = {}
    for column in range(1, 6):
        on_date = _cell_date(worksheet.cell(row, column).value, year, month)
        if on_date is not None:
            dates[column] = on_date
    return dates


def _append_assignment(
    cell,
    on_date,
    period,
    kind,
    position,
    *,
    assignments,
    ignored,
    blockers,
):
    if cell.value is None:
        return
    if not isinstance(cell.value, str):
        blockers.append(
            f"{cell.parent.title}!{cell.coordinate} has unsupported assignment content."
        )
        return
    name = _clean_text(cell.value)
    if not name:
        return
    if _is_ignored_assignment_text(name):
        ignored.append(f"{cell.parent.title}!{cell.coordinate}: {name}")
        return
    assignments.append(
        SourceAssignment(
            on_date,
            period,
            kind,
            name,
            cell.parent.title,
            cell.coordinate,
            len(assignments) + 1,
            position,
        )
    )


def _ignore_assignment_cell(cell, *, ignored):
    if cell.value is None:
        return
    value = _clean_text(cell.value)
    if value:
        ignored.append(f"{cell.parent.title}!{cell.coordinate}: {value}")


def _parse_preceptor_cell(cell, on_date, period, position, *, assignments, ignored, blockers):
    if cell.value is None:
        return
    if isinstance(cell.value, str) and _is_ignored_assignment_text(cell.value):
        _append_assignment(
            cell,
            on_date,
            period,
            "preceptor",
            position,
            assignments=assignments,
            ignored=ignored,
            blockers=blockers,
        )
        return
    try:
        kind = _color_kind(cell)
    except PreceptingWorkbookImportError as error:
        blockers.append(str(error))
        return
    if cell.parent.title == "JUN" and cell.coordinate == "D36":
        kind = "preceptor"
    if kind == "interviewer":
        blockers.append(
            f"{cell.parent.title}!{cell.coordinate} is green outside the Interviewer row."
        )
        return
    if isinstance(cell.value, str) and "/" in cell.value:
        names = tuple(_clean_text(value) for value in cell.value.split("/"))
        if len(names) != 2 or not all(names):
            blockers.append(
                f"{cell.parent.title}!{cell.coordinate} must contain exactly two nonempty "
                "slash-separated physician names."
            )
            return
        for name, responsibility in zip(names, ("primary", "secondary"), strict=True):
            assignments.append(
                SourceAssignment(
                    on_date,
                    period,
                    kind,
                    name,
                    cell.parent.title,
                    cell.coordinate,
                    len(assignments) + 1,
                    position,
                    responsibility,
                )
            )
        return
    _append_assignment(
        cell,
        on_date,
        period,
        kind,
        position,
        assignments=assignments,
        ignored=ignored,
        blockers=blockers,
    )


def _parse_interviewer_cell(cell, on_date, *, assignments, ignored, blockers):
    if cell.value is None:
        return
    if isinstance(cell.value, str) and _is_ignored_assignment_text(cell.value):
        _append_assignment(
            cell,
            on_date,
            "AM",
            "interviewer",
            4,
            assignments=assignments,
            ignored=ignored,
            blockers=blockers,
        )
        return
    try:
        kind = _color_kind(cell)
    except PreceptingWorkbookImportError as error:
        blockers.append(str(error))
        return
    if kind == "community":
        blockers.append(
            f"{cell.parent.title}!{cell.coordinate} is Community Preceptor styled in a fourth cell."
        )
        return
    if kind != "interviewer":
        _ignore_assignment_cell(cell, ignored=ignored)
        return
    _append_assignment(
        cell,
        on_date,
        "AM",
        "interviewer",
        4,
        assignments=assignments,
        ignored=ignored,
        blockers=blockers,
    )


def _parse_pm_fourth_cell(cell, *, ignored, blockers):
    if cell.value is None:
        return
    if isinstance(cell.value, str) and _is_ignored_assignment_text(cell.value):
        _ignore_assignment_cell(cell, ignored=ignored)
        return
    try:
        kind = _color_kind(cell)
    except PreceptingWorkbookImportError as error:
        blockers.append(str(error))
        return
    if kind == "interviewer":
        blockers.append(f"{cell.parent.title}!{cell.coordinate} is green in a PM fourth cell.")
        return
    if kind == "community":
        blockers.append(
            f"{cell.parent.title}!{cell.coordinate} is Community Preceptor styled in a fourth cell."
        )
        return
    _ignore_assignment_cell(cell, ignored=ignored)


def _parse_late_doc_cell(cell, on_date, *, assignments, ignored, blockers):
    if cell.value is None:
        return
    if not (isinstance(cell.value, str) and _is_ignored_assignment_text(cell.value)):
        try:
            kind = _color_kind(cell)
        except PreceptingWorkbookImportError as error:
            blockers.append(str(error))
            return
        if kind == "interviewer":
            blockers.append(f"{cell.parent.title}!{cell.coordinate} is green in a PM fourth cell.")
            return
        if kind == "community":
            blockers.append(
                f"{cell.parent.title}!{cell.coordinate} is Community Preceptor styled in a fourth cell."
            )
            return
    _append_assignment(
        cell,
        on_date,
        "PM",
        "late_doc",
        1,
        assignments=assignments,
        ignored=ignored,
        blockers=blockers,
    )


def _parse_worksheet(worksheet, month, *, assignments, lanes, ignored, blockers):
    year = _worksheet_year(worksheet, month)
    date_rows = []
    for row in range(1, worksheet.max_row + 1):
        dates = _week_dates(worksheet, row, year, month)
        if not dates:
            continue
        for column, on_date in dates.items():
            if on_date.weekday() != column - 1:
                blockers.append(
                    f"{worksheet.title}!{worksheet.cell(row, column).coordinate} is not in its weekday column."
                )
        date_rows.append((row, dates))
    for previous, current in pairwise(date_rows):
        if current[0] - previous[0] != 9:
            blockers.append(
                f"{worksheet.title}!{worksheet.cell(current[0], 1).coordinate} does not begin a nine-row week block."
            )

    for date_row, dates in date_rows:
        for column in set(range(1, 6)).difference(dates):
            for row_offset in range(1, 9):
                cell = worksheet.cell(date_row + row_offset, column)
                if cell.value is not None:
                    blockers.append(
                        f"{worksheet.title}!{cell.coordinate} is outside a dated weekday lane."
                    )
        for column, on_date in dates.items():
            date_cell = worksheet.cell(date_row, column)
            lanes.extend(
                (
                    SourceLane(on_date, "AM", "AM", worksheet.title, date_cell.coordinate),
                    SourceLane(on_date, "PM", "PM", worksheet.title, date_cell.coordinate),
                )
            )
            for position, row_offset in enumerate((1, 2, 3), start=1):
                _parse_preceptor_cell(
                    worksheet.cell(date_row + row_offset, column),
                    on_date,
                    "AM",
                    position,
                    assignments=assignments,
                    ignored=ignored,
                    blockers=blockers,
                )
            _parse_interviewer_cell(
                worksheet.cell(date_row + 4, column),
                on_date,
                assignments=assignments,
                ignored=ignored,
                blockers=blockers,
            )
            for position, row_offset in enumerate((5, 6, 7), start=1):
                _parse_preceptor_cell(
                    worksheet.cell(date_row + row_offset, column),
                    on_date,
                    "PM",
                    position,
                    assignments=assignments,
                    ignored=ignored,
                    blockers=blockers,
                )

        monday = dates.get(1)
        if monday is not None:
            late_doc_cell = worksheet.cell(date_row + 8, 1)
            lanes.append(
                SourceLane(monday, "PM", "late_doc", worksheet.title, late_doc_cell.coordinate)
            )
            _parse_late_doc_cell(
                late_doc_cell,
                monday,
                assignments=assignments,
                ignored=ignored,
                blockers=blockers,
            )
        for column in dates:
            if column == 1:
                continue
            _parse_pm_fourth_cell(
                worksheet.cell(date_row + 8, column),
                ignored=ignored,
                blockers=blockers,
            )


def load_precepting_workbook(source, *, source_name=None):
    """Read exact JAN--DEC tabs into source facts without touching schedule records."""
    if source_name is None:
        source_name = (
            Path(os.fspath(source)).name if isinstance(source, (str, os.PathLike)) else None
        )
    source_name = Path(source_name or getattr(source, "name", "uploaded-workbook.xlsx")).name
    try:
        validate_workbook_archive(source, source_name)
    except WorkbookImportError as error:
        raise PreceptingWorkbookImportError(str(error)) from error
    try:
        workbook = load_workbook(source, data_only=True, read_only=False, keep_links=False)
    except (BadZipFile, OSError, InvalidFileException, ValueError, KeyError) as error:
        raise PreceptingWorkbookImportError(
            f"Cannot read Excel workbook {source_name}: {error}"
        ) from error
    try:
        selected = defaultdict(list)
        for worksheet in workbook.worksheets:
            month = MONTH_SHEETS.get(_clean_text(worksheet.title).upper())
            if month is not None:
                selected[month].append(worksheet)
        missing = [
            calendar.month_abbr[month].upper() for month in range(1, 13) if not selected[month]
        ]
        duplicate = [
            calendar.month_abbr[month]
            for month, worksheets in selected.items()
            if len(worksheets) > 1
        ]
        if missing or duplicate:
            details = []
            if missing:
                details.append(f"missing month sheets: {', '.join(missing)}")
            if duplicate:
                details.append(f"duplicate month sheets: {', '.join(duplicate)}")
            raise PreceptingWorkbookImportError("Workbook has " + "; ".join(details) + ".")
        assignments = []
        lanes = []
        ignored = []
        blockers = []
        for month in range(1, 13):
            _parse_worksheet(
                selected[month][0],
                month,
                assignments=assignments,
                lanes=lanes,
                ignored=ignored,
                blockers=blockers,
            )
        return PreceptingWorkbookData(
            source_name,
            tuple(assignments),
            tuple(lanes),
            tuple(ignored),
            tuple(dict.fromkeys(blockers)),
        )
    finally:
        workbook.close()


def _assignment_state(desired, existing):
    if desired == existing:
        return "exact"
    if not existing:
        return "new"
    if not desired:
        return "removed"
    return "corrected"


def _current_assignments(on_date, period):
    session = (
        PreceptorSession.objects.filter(date=on_date, period_of_day=period)
        .prefetch_related("assignments__physician")
        .first()
    )
    if session is None:
        return ()
    assignments = tuple(session.assignments.all())
    primary = sorted(
        (item for item in assignments if item.responsibility == "primary"),
        key=lambda item: item.primary_position,
    )
    secondary = tuple(item for item in assignments if item.responsibility == "secondary")
    return tuple((*primary, *secondary))


def _effective_community_role(physician, on_date):
    return (
        EffectiveRole.objects.filter(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start__lte=on_date,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .exists()
    )


def build_precepting_workbook_import_plan(data):
    """Resolve source names and compare source facts to current draft records only."""
    physicians_by_key = defaultdict(list)
    physicians_by_last_word = defaultdict(list)
    for physician in Physician.objects.order_by("display_name", "pk"):
        key = _name_key(physician.display_name)
        physicians_by_key[key].append(physician)
        if words := key.split():
            physicians_by_last_word[words[-1]].append(physician)
    blockers = list(data.blockers)
    unique_names = []
    ambiguous_names = []
    unmatched_names = []
    resolutions = {}
    for source in data.assignments:
        key = _name_key(source.name)
        if key in resolutions:
            continue
        if target_key := _PRECEPTING_NAME_ALIASES.get(key):
            matches = tuple(physicians_by_key[target_key])
        else:
            matches = tuple(physicians_by_key[key])
            if not matches and len(key.split()) == 1:
                matches = tuple(physicians_by_last_word[key])
        state = "unique" if len(matches) == 1 else "ambiguous" if matches else "unmatched"
        resolution = NameResolution(source.name, source.kind, state, matches)
        resolutions[key] = resolution
        if state == "unique":
            unique_names.append(resolution)
        elif state == "ambiguous":
            ambiguous_names.append(resolution)
            blockers.append(f"{source.name}: physician identity is ambiguous.")
        else:
            unmatched_names.append(resolution)
            blockers.append(f"{source.name}: no matching physician exists.")

    planned_by_lane = defaultdict(list)
    interview_by_date = {}
    late_doc_by_date = {}
    for source in data.assignments:
        resolution = resolutions[_name_key(source.name)]
        if resolution.state != "unique":
            continue
        physician = resolution.physicians[0]
        if source.kind == "community" and not _effective_community_role(physician, source.on_date):
            blockers.append(
                f"{source.sheet}!{source.coordinate}: {source.name} is not a Community Preceptor on {source.on_date}."
            )
            continue
        if source.kind == "interviewer":
            if source.on_date in interview_by_date:
                blockers.append(f"{source.on_date}: more than one Interviewer is present.")
            interview_by_date[source.on_date] = physician
        elif source.kind == "late_doc":
            if source.on_date.weekday() != 0:
                blockers.append(
                    f"{source.sheet}!{source.coordinate}: Late Doc must be on a Monday."
                )
            elif source.on_date in late_doc_by_date:
                blockers.append(f"{source.on_date}: more than one Late Doc is present.")
            late_doc_by_date[source.on_date] = physician
        else:
            planned_by_lane[(source.on_date, source.period_of_day)].append((source, physician))

    precepting = []
    for lane in data.lanes:
        if lane.kind == "late_doc":
            continue
        lane_sources = planned_by_lane[(lane.on_date, lane.period_of_day)]
        primary_sources = [item for item in lane_sources if item[0].responsibility == "primary"]
        secondary_sources = [item for item in lane_sources if item[0].responsibility == "secondary"]
        primary_sources.sort(key=lambda item: item[0].kind != "community")
        if secondary_sources and len(secondary_sources) > 1:
            blockers.append(
                f"{lane.sheet}!{lane.coordinate}: more than one split backup is present."
            )
        if secondary_sources and len(primary_sources) > 2:
            blockers.append(
                f"{lane.sheet}!{lane.coordinate}: an ordinary Preceptor requires the secondary position."
            )
        if not secondary_sources and len(primary_sources) > 3:
            blockers.append(
                f"{lane.sheet}!{lane.coordinate}: more than three Preceptors are present."
            )
        if any(source.kind == "community" for source, _ in primary_sources[2:]):
            blockers.append(
                f"{lane.sheet}!{lane.coordinate}: more than two Community Preceptors are present."
            )
        if secondary_sources and len({physician.pk for _, physician in lane_sources}) != len(
            lane_sources
        ):
            blockers.append(f"{lane.sheet}!{lane.coordinate}: a physician appears more than once.")
        if secondary_sources:
            desired = tuple(
                [
                    PlannedPreceptingAssignment(physician, "primary", position, source.kind)
                    for position, (source, physician) in enumerate(primary_sources, start=1)
                ]
                + [
                    PlannedPreceptingAssignment(physician, "secondary", None, source.kind)
                    for source, physician in secondary_sources
                ]
            )
        else:
            desired = tuple(
                PlannedPreceptingAssignment(
                    physician,
                    "primary" if position < 3 else "secondary",
                    position if position < 3 else None,
                    source.kind,
                )
                for position, (source, physician) in enumerate(primary_sources, start=1)
            )
        existing = _current_assignments(lane.on_date, lane.period_of_day)
        desired_signature = tuple(
            (item.physician.pk, item.responsibility, item.primary_position) for item in desired
        )
        existing_signature = tuple(
            (item.physician_id, item.responsibility, item.primary_position) for item in existing
        )
        precepting.append(
            PreceptingPreview(
                lane.on_date,
                lane.period_of_day,
                desired,
                existing,
                _assignment_state(desired_signature, existing_signature),
            )
        )

    late_doc_dates = {lane.on_date for lane in data.lanes if lane.kind == "late_doc"}
    late_docs = []
    for on_date in sorted(late_doc_dates):
        physician = late_doc_by_date.get(on_date)
        existing = (
            LateDocAssignment.objects.filter(date=on_date).select_related("physician").first()
        )
        late_docs.append(
            LateDocPreview(
                on_date,
                physician,
                existing,
                _assignment_state(
                    physician.pk if physician else None, existing.physician_id if existing else None
                ),
            )
        )

    # Every represented AM lane authoritatively describes whether it has an Interviewer.
    interview_dates = {lane.on_date for lane in data.lanes if lane.kind == "AM"}
    interviews = []
    for on_date in sorted(interview_dates):
        physician = interview_by_date.get(on_date)
        existing = (
            InterviewShift.objects.filter(date=on_date, is_active=True)
            .select_related("physician")
            .first()
        )
        if physician is None and existing is None:
            state = "exact"
        elif existing is None:
            state = "new"
        elif physician is None:
            state = "removed"
        elif physician.pk == existing.physician_id:
            state = "exact"
        else:
            state = "corrected"
        interviews.append(
            InterviewPreview(
                on_date,
                physician,
                existing,
                state,
            )
        )

    return PreceptingWorkbookImportPlan(
        data,
        tuple(sorted(precepting, key=lambda item: (item.on_date, item.period_of_day))),
        tuple(late_docs),
        tuple(interviews),
        tuple(unique_names),
        tuple(ambiguous_names),
        tuple(unmatched_names),
        tuple(dict.fromkeys(blockers)),
    )


def _plan_signature(plan):
    def precepting_value(item):
        return (
            item.on_date,
            item.period_of_day,
            tuple(
                (
                    assignment.physician.pk,
                    assignment.responsibility,
                    assignment.primary_position,
                    assignment.kind,
                )
                for assignment in item.assignments
            ),
            tuple(
                (
                    assignment.pk,
                    assignment.physician_id,
                    assignment.responsibility,
                    assignment.primary_position,
                    assignment.is_locked,
                )
                for assignment in item.existing
            ),
            item.state,
        )

    def late_doc_value(item):
        return (
            item.on_date,
            item.physician.pk if item.physician else None,
            (item.existing.pk, item.existing.physician_id) if item.existing else None,
            item.state,
        )

    def interview_value(item):
        return (
            item.on_date,
            item.physician.pk if item.physician else None,
            (
                item.existing.pk,
                item.existing.physician_id,
                item.existing.is_active,
                item.existing.note,
            )
            if item.existing
            else None,
            item.state,
        )

    return (
        tuple(precepting_value(item) for item in plan.precepting),
        tuple(late_doc_value(item) for item in plan.late_docs),
        tuple(interview_value(item) for item in plan.interviews),
        tuple(
            (item.name, item.kind, item.state, tuple(physician.pk for physician in item.physicians))
            for item in plan.unique_names
        ),
        tuple(
            (item.name, item.kind, item.state, tuple(physician.pk for physician in item.physicians))
            for item in plan.ambiguous_names
        ),
        tuple(
            (item.name, item.kind, item.state, tuple(physician.pk for physician in item.physicians))
            for item in plan.unmatched_names
        ),
        plan.blockers,
    )


def _import_note(data, note):
    detail = f"Imported from {data.source_name}"
    return f"{detail}: {note}" if note else detail


def _replace_preceptor_assignments(plan, *, scheduler, reason):
    for item in plan.precepting:
        if item.state == "exact":
            continue
        desired = {
            (assignment.physician.pk, assignment.responsibility, assignment.primary_position)
            for assignment in item.assignments
        }
        for assignment in item.existing:
            if (
                assignment.physician_id,
                assignment.responsibility,
                assignment.primary_position,
            ) not in desired:
                remove_preceptor_assignment(
                    assignment=assignment, scheduler=scheduler, reason=reason
                )

    for item in plan.precepting:
        if item.state == "exact":
            continue
        session = PreceptorSession.objects.get(date=item.on_date, period_of_day=item.period_of_day)
        for assignment in item.assignments:
            assign_preceptor(
                session=session,
                physician=assignment.physician,
                responsibility=assignment.responsibility,
                primary_position=assignment.primary_position,
                scheduler=scheduler,
                reason=reason,
                override=True,
            )


def _replace_late_docs(plan, *, scheduler, note):
    for item in plan.late_docs:
        if item.state == "exact":
            continue
        if item.physician is None:
            remove_late_doc_assignment(assignment=item.existing, scheduler=scheduler, note=note)
        else:
            assign_late_doc(
                on_date=item.on_date,
                physician=item.physician,
                scheduler=scheduler,
                note=note,
                override=True,
            )


def _replace_interviews(plan, *, scheduler, note):
    for item in plan.interviews:
        if item.state == "exact":
            continue
        if item.physician is None:
            remove_interview_shift(shift=item.existing, scheduler=scheduler)
            continue
        shift = item.existing or create_interview_shift(
            academic_year=academic_year_for_date(item.on_date),
            date=item.on_date,
            scheduler=scheduler,
            note=note,
        )
        assign_interview_shift(
            shift=shift,
            physician=item.physician,
            scheduler=scheduler,
            override=True,
        )


@scheduling_administration_mutation
def apply_precepting_workbook_import(*, plan, scheduler, note=""):
    """Apply an unchanged clean preview through the owning schedule mutation services."""
    if plan.blockers:
        raise PreceptingWorkbookImportError("Cannot apply a workbook import while blockers remain.")
    fresh_plan = build_precepting_workbook_import_plan(plan.data)
    if _plan_signature(fresh_plan) != _plan_signature(plan):
        raise PreceptingWorkbookImportError(
            "The schedule changed after preview. Preview the workbook again to review a fresh plan."
        )

    with transaction.atomic():
        for lane in fresh_plan.data.lanes:
            capture_calendar_range(lane.on_date)
        ensure_precepting_horizon_sessions(
            work_week_start(lane.on_date)
            for lane in fresh_plan.data.lanes
            if lane.kind in {"AM", "PM"}
        )
        current_plan = build_precepting_workbook_import_plan(fresh_plan.data)
        if _plan_signature(current_plan) != _plan_signature(fresh_plan):
            raise PreceptingWorkbookImportError(
                "The schedule changed while initializing the workbook dates. Preview the workbook again."
            )
        import_note = _import_note(current_plan.data, note)
        _replace_preceptor_assignments(current_plan, scheduler=scheduler, reason=import_note)
        _replace_late_docs(current_plan, scheduler=scheduler, note=import_note)
        _replace_interviews(current_plan, scheduler=scheduler, note=import_note)
    return current_plan
