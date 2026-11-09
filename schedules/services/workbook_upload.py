"""Bounded validation shared by synchronous Excel workbook imports."""

from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_WORKBOOK_ARCHIVE_ENTRIES = 2048
MAX_WORKBOOK_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


class WorkbookImportError(ValueError):
    """An uploaded workbook is unsafe or cannot be read as an XLSX archive."""


def validate_workbook_archive(source, source_name):
    """Reject archive shapes that are unsafe before openpyxl parses them."""
    position = None
    if hasattr(source, "tell"):
        try:
            position = source.tell()
        except OSError:
            position = None
    try:
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_WORKBOOK_ARCHIVE_ENTRIES:
                raise WorkbookImportError(
                    f"Workbook {Path(source_name).name} contains too many archive entries."
                )
            if sum(entry.file_size for entry in entries) > MAX_WORKBOOK_UNCOMPRESSED_BYTES:
                raise WorkbookImportError(
                    f"Workbook {Path(source_name).name} expands beyond the 64 MB safety limit."
                )
    except (BadZipFile, OSError) as error:
        raise WorkbookImportError(
            f"Cannot read Excel workbook {Path(source_name).name}: it is not a valid .xlsx archive."
        ) from error
    finally:
        if hasattr(source, "seek"):
            source.seek(position or 0)
