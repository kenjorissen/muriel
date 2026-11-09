"""Academic-year boundaries and display labels for Academic Event workflows."""

from datetime import date, timedelta


def academic_year_for_date(on_date: date) -> int:
    """Return the starting calendar year for the July-through-June academic year."""
    return on_date.year if on_date.month >= 7 else on_date.year - 1


def academic_year_label(start_year: int) -> str:
    return f"{start_year}-{start_year + 1}"


def academic_year_bounds(start_year: int) -> tuple[date, date]:
    return date(start_year, 7, 1), date(start_year + 1, 6, 30)


def expected_quarter_start(academic_year: int, expected_quarter: str) -> date:
    """Return the first calendar date of a July-through-June academic quarter."""
    quarter_starts = {
        "Q1": (academic_year, 7, 1),
        "Q2": (academic_year, 10, 1),
        "Q3": (academic_year + 1, 1, 1),
        "Q4": (academic_year + 1, 4, 1),
    }
    try:
        return date(*quarter_starts[expected_quarter])
    except KeyError:
        raise ValueError("Expected quarter must be Q1, Q2, Q3, or Q4.") from None


def expected_quarter_date_range(
    academic_year: int, expected_quarter: str, duration_days: int
) -> tuple[date, date]:
    """Return the inclusive quarter-first range for an event duration."""
    start_date = expected_quarter_start(academic_year, expected_quarter)
    return start_date, start_date + timedelta(days=duration_days - 1)


def default_duration_fits_expected_quarter(expected_quarter: str, duration_days: int) -> bool:
    """Require defaults to fit even in an academic year without a leap day."""
    _, end_date = expected_quarter_date_range(2025, expected_quarter, duration_days)
    return academic_year_for_date(end_date) == 2025
