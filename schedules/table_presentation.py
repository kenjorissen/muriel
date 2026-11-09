"""Typed, safe presentation values for ordinary HTML data tables."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from django.utils import timezone

CellKind = Literal[
    "text", "capacity", "date", "datetime", "metadata", "code", "link", "links", "status", "tags"
]
TableTone = Literal["", "positive", "attention", "danger"]
TableWidth = Literal["", "bounded", "compact", "wide"]

_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*")


def _plain_text(value: str) -> str:
    """Discard Django's safe-string marker before template autoescaping."""
    if not isinstance(value, str):
        raise TypeError("Table presentation values must be strings.")
    return value.encode("utf-8").decode("utf-8")


@dataclass(frozen=True)
class DataTableLink:
    label: str
    url: str
    emphasized: bool = False

    def __post_init__(self):
        if not isinstance(self.emphasized, bool):
            raise TypeError("Table link emphasis must be a boolean.")
        object.__setattr__(self, "label", _plain_text(self.label))
        object.__setattr__(self, "url", _plain_text(self.url))


@dataclass(frozen=True)
class DataTableCell:
    value: str
    kind: CellKind = "text"
    row_header: bool = False
    machine_value: str = ""
    detail: str = ""
    url: str = ""
    links: tuple[DataTableLink, ...] = ()
    tags: tuple[str, ...] = ()
    tone: TableTone = ""
    accessible_label: str = ""

    def __post_init__(self):
        if self.kind not in {
            "text",
            "capacity",
            "date",
            "datetime",
            "metadata",
            "code",
            "link",
            "links",
            "status",
            "tags",
        }:
            raise ValueError("Unknown data-table cell kind.")
        if self.tone not in {"", "positive", "attention", "danger"}:
            raise ValueError("Unknown data-table cell tone.")
        if self.kind == "link" and not self.url:
            raise ValueError("A link cell needs a URL.")
        if self.kind == "links" and not self.links:
            raise ValueError("A links cell needs at least one link.")
        if self.kind == "metadata" and bool(self.detail) != bool(self.machine_value):
            raise ValueError("Timestamp metadata needs both display and machine values.")
        if any(not isinstance(link, DataTableLink) for link in self.links):
            raise TypeError("Table links must use DataTableLink values.")
        if not isinstance(self.tags, tuple):
            raise TypeError("Table tags must be an immutable tuple of strings.")
        if self.kind == "tags" and not self.tags:
            raise ValueError("A tags cell needs at least one tag.")
        object.__setattr__(self, "value", _plain_text(self.value))
        object.__setattr__(self, "machine_value", _plain_text(self.machine_value))
        object.__setattr__(self, "detail", _plain_text(self.detail))
        object.__setattr__(self, "url", _plain_text(self.url))
        object.__setattr__(self, "accessible_label", _plain_text(self.accessible_label))
        tags = tuple(_plain_text(tag) for tag in self.tags)
        if any(not tag for tag in tags):
            raise ValueError("Table tags must not be empty.")
        object.__setattr__(self, "tags", tags)


@dataclass(frozen=True)
class DataTableColumn:
    label: str

    def __post_init__(self):
        object.__setattr__(self, "label", _plain_text(self.label))


@dataclass(frozen=True)
class DataTableRow:
    cells: tuple[DataTableCell, ...]
    tone: TableTone = ""

    def __post_init__(self):
        if self.tone not in {"", "positive", "attention", "danger"}:
            raise ValueError("Unknown data-table row tone.")
        if any(not isinstance(cell, DataTableCell) for cell in self.cells):
            raise TypeError("Table rows must use DataTableCell values.")


@dataclass(frozen=True)
class DataTable:
    identity: str
    label: str
    columns: tuple[DataTableColumn, ...]
    rows: tuple[DataTableRow, ...]
    empty_state: str
    footer_rows: tuple[DataTableRow, ...] = ()
    width: TableWidth = ""

    def __post_init__(self):
        if not _IDENTIFIER.fullmatch(self.identity):
            raise ValueError("A data-table identity must be a lowercase hyphenated identifier.")
        if not self.columns:
            raise ValueError("A data table needs at least one column.")
        if self.width not in {"", "bounded", "compact", "wide"}:
            raise ValueError("Unknown data-table width.")
        if any(not isinstance(column, DataTableColumn) for column in self.columns):
            raise TypeError("Table columns must use DataTableColumn values.")
        if any(not isinstance(row, DataTableRow) for row in (*self.rows, *self.footer_rows)):
            raise TypeError("Table rows must use DataTableRow values.")
        for row in (*self.rows, *self.footer_rows):
            if len(row.cells) != len(self.columns):
                raise ValueError("Each data-table row must have one cell per column.")
        empty_state = _plain_text(self.empty_state)
        if not empty_state:
            raise ValueError("A data table needs empty-state content.")
        object.__setattr__(self, "label", _plain_text(self.label))
        object.__setattr__(self, "empty_state", empty_state)


def retained_value(value: object) -> str:
    """Render retained structured values as readable, deterministic code text."""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def history_timestamp(value) -> DataTableCell:
    """Present retained change timestamps consistently in the organization time zone."""
    return DataTableCell(
        timezone.localtime(value).strftime("%Y-%m-%d %H:%M"),
        kind="datetime",
        machine_value=value.isoformat(),
    )
