from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ClinicHoliday(models.TextChoices):
    NEW_YEARS_DAY = "new_years_day", "New Year's Day"
    MEMORIAL_DAY = "memorial_day", "Memorial Day"
    INDEPENDENCE_DAY = "independence_day", "Independence Day"
    LABOR_DAY = "labor_day", "Labor Day"
    THANKSGIVING_DAY = "thanksgiving_day", "Thanksgiving Day"
    CHRISTMAS_EVE = "christmas_eve", "Christmas Eve"
    CHRISTMAS_DAY = "christmas_day", "Christmas Day"


class HolidayOccurrence(models.Model):
    """One retained calendar-year resolution of a fixed clinic holiday."""

    class Coverage(models.TextChoices):
        WHOLE_DAY = "whole_day", "AM and PM"
        PM = "PM", "PM only"

    class State(models.TextChoices):
        COMPUTED = "computed", "Computed"
        SCHEDULED = "scheduled", "Scheduled"
        UNSCHEDULED = "unscheduled", "Unscheduled"
        NOT_OBSERVED = "not_observed", "Not observed"

    holiday = models.CharField(max_length=20, choices=ClinicHoliday.choices)
    calendar_year = models.PositiveIntegerField()
    name = models.CharField(max_length=40)
    nominal_date = models.DateField()
    observed_date = models.DateField(null=True, blank=True)
    coverage = models.CharField(max_length=9, choices=Coverage.choices)
    state = models.CharField(max_length=12, choices=State.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("calendar_year", "nominal_date", "holiday", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("holiday", "calendar_year"), name="one_holiday_occurrence_per_year"
            ),
        )

    def clean(self):
        super().clean()
        if self.nominal_date and self.nominal_date.year != self.calendar_year:
            raise ValidationError(
                {"nominal_date": "Nominal date must fall in the selected calendar year."}
            )
        if self.state in (self.State.COMPUTED, self.State.SCHEDULED) and not self.observed_date:
            raise ValidationError({"observed_date": "An observed date is required."})
        if self.state in (self.State.UNSCHEDULED, self.State.NOT_OBSERVED) and self.observed_date:
            raise ValidationError(
                {"observed_date": "This occurrence cannot have an observed date."}
            )

    @property
    def is_manually_schedulable(self):
        return self.holiday in {
            ClinicHoliday.NEW_YEARS_DAY,
            ClinicHoliday.INDEPENDENCE_DAY,
            ClinicHoliday.CHRISTMAS_DAY,
        }

    @property
    def source(self):
        return "Scheduled" if self.state == self.State.SCHEDULED else "Computed"

    def delete(self, *args, **kwargs):
        raise ValidationError("Holiday occurrences are retained and cannot be deleted.")

    def __str__(self):
        return f"{self.name}: {self.calendar_year}"


class HolidayOccurrenceChange(models.Model):
    class Action(models.TextChoices):
        MATERIALIZED = "materialized", "Materialized"
        SCHEDULED = "scheduled", "Scheduled"
        CORRECTED = "corrected", "Corrected"

    occurrence = models.ForeignKey(
        HolidayOccurrence, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    occurrence_id_at_recording = models.PositiveBigIntegerField()
    action = models.CharField(max_length=12, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_holiday_occurrence_changes",
        null=True,
        blank=True,
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError("Holiday occurrence changes are retained and cannot be deleted.")

    def __str__(self):
        return f"Holiday occurrence {self.get_action_display()} at {self.changed_at}"


__all__ = ("ClinicHoliday", "HolidayOccurrence", "HolidayOccurrenceChange")
