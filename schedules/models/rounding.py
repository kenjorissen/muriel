from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .physicians import EffectiveRole, Physician


class NormalRounder(models.Model):
    """The one normal owner of a Monday-Sunday rounding week."""

    week = models.DateField(unique=True)
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="normal_rounding_weeks"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("week",)

    def clean(self):
        super().clean()
        if self.week and self.week.weekday() != 0:
            raise ValidationError({"week": "Rounding weeks must start on a Monday."})
        if (
            self.week
            and self.physician_id
            and not _normal_rounder_role_covers_week(self.physician_id, self.week)
        ):
            week_end = self.week + timedelta(days=6)
            raise ValidationError(
                {
                    "physician": (
                        "A normal rounder must have Normal Rounder eligibility for "
                        f"every date from {self.week.isoformat()} through {week_end.isoformat()}."
                    )
                }
            )

    def __str__(self):
        return f"{self.week}: {self.physician}"


class CasualRounderRule(models.Model):
    """One physician's all-day ordinal weekday casual-rounding recurrence."""

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
        Physician, on_delete=models.PROTECT, related_name="casual_rounder_rules"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    ordinals = models.JSONField()
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("physician__display_name", "weekday", "effective_start", "pk")

    def clean(self):
        super().clean()
        if self.weekday not in range(7):
            raise ValidationError({"weekday": "Choose one day of the week."})
        if (
            not isinstance(self.ordinals, list)
            or not self.ordinals
            or set(self.ordinals) - set(range(1, 6))
        ):
            raise ValidationError(
                {"ordinals": "Choose one or more of the first through fifth occurrences."}
            )
        if len(set(self.ordinals)) != len(self.ordinals):
            raise ValidationError({"ordinals": "Each occurrence can be selected only once."})
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})
        overlaps = CasualRounderRule.objects.filter(
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
                "Casual-rounder rules for this physician and weekday cannot overlap."
            )

    def matches(self, on_date):
        return (
            on_date.weekday() == self.weekday
            and _weekday_ordinal_in_month(on_date) in self.ordinals
            and (not self.effective_start or self.effective_start <= on_date)
            and (not self.effective_end or on_date <= self.effective_end)
        )

    @property
    def ordinal_display(self):
        labels = ("First", "Second", "Third", "Fourth", "Fifth")
        return ", ".join(labels[ordinal - 1] for ordinal in self.ordinals)


class RoundingScheduleChange(models.Model):
    class Subject(models.TextChoices):
        NORMAL_ROUNDER = "normal_rounder", "Normal rounder"
        CASUAL_ROUNDER_RULE = "casual_rounder_rule", "Casual-rounder rule"

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        REMOVED = "removed", "Removed"

    normal_rounder = models.ForeignKey(
        NormalRounder, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    casual_rounder_rule = models.ForeignKey(
        CasualRounderRule, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    source_id_at_recording = models.PositiveBigIntegerField()
    subject = models.CharField(max_length=20, choices=Subject.choices)
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="rounding_schedule_changes"
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_rounding_schedule_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")


def _weekday_ordinal_in_month(on_date):
    return (on_date.day - 1) // 7 + 1


def _normal_rounder_role_covers_week(physician_id, week):
    for offset in range(7):
        on_date = week + timedelta(days=offset)
        if (
            not EffectiveRole.objects.filter(
                physician_id=physician_id,
                role__in=(EffectiveRole.Role.FACULTY, EffectiveRole.Role.COMMUNITY_ROUNDER),
                effective_start__lte=on_date,
            )
            .filter(models.Q(effective_end__isnull=True) | models.Q(effective_end__gte=on_date))
            .exists()
        ):
            return False
    return True


__all__ = ("CasualRounderRule", "NormalRounder", "RoundingScheduleChange")
