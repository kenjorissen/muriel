from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .physicians import Physician


class VacationQuerySet(models.QuerySet):
    def overlapping(self, start_date, end_date):
        if end_date < start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})
        return self.filter(start_date__lte=end_date, end_date__gte=start_date)


class Vacation(models.Model):
    """A manually recorded Vacation Schedule source fact, independent of roles."""

    class PeriodOfDay(models.TextChoices):
        WHOLE_DAY = "whole_day", "Whole day"
        AM = "AM", "AM only"
        PM = "PM", "PM only"

    physician = models.ForeignKey(Physician, on_delete=models.PROTECT, related_name="vacations")
    start_date = models.DateField()
    end_date = models.DateField()
    period_of_day = models.CharField(
        max_length=9, choices=PeriodOfDay.choices, default=PeriodOfDay.WHOLE_DAY
    )
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = VacationQuerySet.as_manager()

    class Meta:
        ordering = ("start_date", "end_date", "physician__display_name", "pk")

    def clean(self):
        super().clean()
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})

    def covers(self, on_date, period_of_day):
        if period_of_day not in (self.PeriodOfDay.AM, self.PeriodOfDay.PM):
            raise ValidationError({"period_of_day": "Period must be AM or PM."})
        return self.start_date <= on_date <= self.end_date and (
            self.period_of_day == self.PeriodOfDay.WHOLE_DAY or self.period_of_day == period_of_day
        )

    def __str__(self):
        return f"{self.physician}: {self.start_date} through {self.end_date} ({self.get_period_of_day_display()})"


class VacationChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Edited"
        REMOVED = "removed", "Removed"

    vacation = models.ForeignKey(
        Vacation, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    vacation_id_at_recording = models.PositiveBigIntegerField()
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="vacation_changes"
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    previous_note = models.TextField(null=True, blank=True)
    recorded_note = models.TextField(null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_vacation_changes"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def __str__(self):
        return f"{self.physician} vacation {self.get_action_display()} at {self.changed_at}"


__all__ = ("Vacation", "VacationChange")
