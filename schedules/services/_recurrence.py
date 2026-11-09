"""Shared normalization for the identical ordinal recurrence input."""

from django.core.exceptions import ValidationError


def normalize_ordinals(ordinals):
    if ordinals == "all":
        return [1, 2, 3, 4, 5]
    if isinstance(ordinals, (list, tuple, set)) and "all" in ordinals:
        return [1, 2, 3, 4, 5]
    if isinstance(ordinals, str):
        ordinals = [ordinals]
    try:
        values = [int(value) for value in ordinals]
    except TypeError, ValueError:
        raise ValidationError(
            {"ordinals": "Choose one or more of the first through fifth occurrences."}
        ) from None
    return sorted(set(values))
