from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .physicians import Physician


class CommunityPreceptorRule(models.Model):
    """One physician's AM/PM ordinal weekday Community Preceptor commitment."""

    class PeriodOfDay(models.TextChoices):
        AM = "AM", "AM"
        PM = "PM", "PM"

    WEEKDAY_CHOICES = (
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    )

    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="community_preceptor_rules"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    ordinals = models.JSONField()
    periods = models.JSONField(default=list)
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("physician__display_name", "weekday", "effective_start", "pk")

    def clean(self):
        super().clean()
        if self.weekday not in range(7):
            raise ValidationError({"weekday": "Choose one day of the week."})
        try:
            ordinal_values = set(self.ordinals)
        except TypeError, AttributeError:
            ordinal_values = set()
        if (
            not isinstance(self.ordinals, list)
            or not self.ordinals
            or ordinal_values - set(range(1, 6))
        ):
            raise ValidationError(
                {"ordinals": "Choose one or more of the first through fifth occurrences."}
            )
        if len(ordinal_values) != len(self.ordinals):
            raise ValidationError({"ordinals": "Each occurrence can be selected only once."})
        try:
            period_values = set(self.periods)
        except TypeError, AttributeError:
            period_values = set()
        valid_periods = set(self.PeriodOfDay.values)
        if not isinstance(self.periods, list) or not self.periods or period_values - valid_periods:
            raise ValidationError({"periods": "Select AM, PM, or both periods."})
        if len(period_values) != len(self.periods):
            raise ValidationError({"periods": "Each period can be selected only once."})
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})

        overlaps = CommunityPreceptorRule.objects.filter(
            physician=self.physician, weekday=self.weekday
        ).exclude(pk=self.pk)
        if self.effective_end:
            overlaps = overlaps.filter(
                Q(effective_start__isnull=True) | Q(effective_start__lte=self.effective_end)
            )
        if self.effective_start:
            overlaps = overlaps.filter(
                Q(effective_end__isnull=True) | Q(effective_end__gte=self.effective_start)
            )
        if overlaps.exists():
            raise ValidationError(
                "Community Preceptor rules for this physician and weekday cannot overlap."
            )

    def matches(self, on_date, period_of_day=None):
        date_matches = (
            on_date.weekday() == self.weekday
            and _weekday_ordinal_in_month(on_date) in self.ordinals
            and (not self.effective_start or self.effective_start <= on_date)
            and (not self.effective_end or on_date <= self.effective_end)
        )
        return date_matches and (period_of_day is None or period_of_day in self.periods)

    @property
    def ordinal_display(self):
        labels = ("First", "Second", "Third", "Fourth", "Fifth")
        return ", ".join(labels[ordinal - 1] for ordinal in self.ordinals)

    @property
    def periods_display(self):
        if set(self.periods) == set(self.PeriodOfDay.values):
            return "AM and PM"
        return " and ".join(period for period in self.PeriodOfDay.values if period in self.periods)

    def __str__(self):
        return f"{self.physician}: {self.get_weekday_display()} ({self.periods_display})"


class CommunityPreceptorScheduleChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        REMOVED = "removed", "Removed"

    community_preceptor_rule = models.ForeignKey(
        CommunityPreceptorRule,
        on_delete=models.SET_NULL,
        related_name="changes",
        null=True,
        blank=True,
    )
    source_id_at_recording = models.PositiveBigIntegerField()
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="community_preceptor_schedule_changes"
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_community_preceptor_schedule_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    @property
    def rule(self):
        return self.community_preceptor_rule

    @property
    def community_preceptor_rule_id_at_recording(self):
        return self.source_id_at_recording

    def __str__(self):
        return f"{self.physician} Community Preceptor Schedule {self.get_action_display()} at {self.changed_at}"


def _weekday_ordinal_in_month(on_date):
    return (on_date.day - 1) // 7 + 1


__all__ = ("CommunityPreceptorRule", "CommunityPreceptorScheduleChange")
