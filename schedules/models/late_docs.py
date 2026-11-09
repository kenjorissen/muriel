from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .physicians import Physician


class LateDocAssignment(models.Model):
    """The one current Faculty Late Doc assignment for a Monday."""

    date = models.DateField(unique=True)
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="late_doc_assignments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("date", "pk")
        constraints = (
            models.CheckConstraint(
                condition=models.Q(date__week_day=2),
                name="late_doc_assignments_are_mondays",
            ),
        )

    def clean(self):
        super().clean()
        if self.date and self.date.weekday() != 0:
            raise ValidationError({"date": "Late Doc assignments must be on a Monday."})

    def __str__(self):
        return f"Late Doc {self.date.isoformat()}: {self.physician}"


class LateDocAssignmentChange(models.Model):
    class Action(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        REMOVED = "removed", "Removed"

    assignment = models.ForeignKey(
        LateDocAssignment,
        on_delete=models.SET_NULL,
        related_name="changes",
        null=True,
        blank=True,
    )
    assignment_id_at_recording = models.PositiveBigIntegerField()
    date = models.DateField()
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_late_doc_assignment_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError("Late Doc assignment changes are retained and cannot be deleted.")


__all__ = ("LateDocAssignment", "LateDocAssignmentChange")
