from django.conf import settings
from django.db import models

from .physicians import Physician


class SchedulePeriod(models.Model):
    """A persisted monthly Preceptor Schedule draft."""

    month = models.DateField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("month",)

    def __str__(self):
        return self.month.strftime("%B %Y")


class PreceptorSession(models.Model):
    class PeriodOfDay(models.TextChoices):
        AM = "AM", "AM"
        PM = "PM", "PM"

    period = models.ForeignKey(
        SchedulePeriod,
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    date = models.DateField()
    period_of_day = models.CharField(max_length=2, choices=PeriodOfDay.choices)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("period", "date", "period_of_day"),
                name="unique_preceptor_session",
            ),
        )
        ordering = ("date", "period_of_day")

    def __str__(self):
        return f"{self.date.isoformat()} {self.period_of_day}"


class PreceptorAssignment(models.Model):
    """One explicit responsibility slot in a draft preceptor session."""

    class Responsibility(models.TextChoices):
        PRIMARY = "primary", "Primary"
        SECONDARY = "secondary", "Secondary"

    session = models.ForeignKey(
        PreceptorSession, on_delete=models.CASCADE, related_name="assignments"
    )
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="preceptor_assignments"
    )
    responsibility = models.CharField(max_length=12, choices=Responsibility.choices)
    primary_position = models.PositiveSmallIntegerField(null=True, blank=True)
    is_locked = models.BooleanField(default=False)

    class Meta:
        ordering = ("responsibility", "primary_position", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("session", "physician"), name="unique_physician_per_preceptor_session"
            ),
            models.UniqueConstraint(
                fields=("session", "primary_position"),
                condition=models.Q(responsibility="primary"),
                name="unique_primary_position_per_preceptor_session",
            ),
            models.UniqueConstraint(
                fields=("session", "responsibility"),
                condition=models.Q(responsibility="secondary"),
                name="unique_secondary_per_preceptor_session",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        responsibility="primary",
                        primary_position__in=(1, 2),
                    )
                    | models.Q(
                        responsibility="secondary",
                        primary_position__isnull=True,
                    )
                ),
                name="valid_preceptor_assignment_slot",
            ),
        )

    def __str__(self):
        return f"{self.session}: {self.physician} ({self.get_responsibility_display()})"


class PreceptorAssignmentChange(models.Model):
    class Action(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        REMOVED = "removed", "Removed"
        LOCK_CHANGED = "lock_changed", "Lock changed"

    session = models.ForeignKey(
        PreceptorSession, on_delete=models.PROTECT, related_name="assignment_changes"
    )
    assignment_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_assignment_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def __str__(self):
        return f"{self.session} {self.get_action_display()} at {self.changed_at}"


__all__ = (
    "PreceptorAssignment",
    "PreceptorAssignmentChange",
    "PreceptorSession",
    "SchedulePeriod",
)
