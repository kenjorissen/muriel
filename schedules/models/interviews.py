from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .physicians import Physician


class InterviewShift(models.Model):
    """One retained AM interview date, with at most one current Faculty assignment."""

    date = models.DateField()
    physician = models.ForeignKey(
        Physician,
        on_delete=models.PROTECT,
        related_name="interview_shifts",
        null=True,
        blank=True,
    )
    note = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("date", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("date",),
                condition=Q(is_active=True),
                name="one_active_interview_shift_per_date",
            ),
        )

    def clean(self):
        super().clean()
        if not self.is_active and self.physician_id:
            raise ValidationError("An inactive Interview shift cannot retain an assignment.")

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Interview shifts are retained scheduling records and cannot be deleted."
        )

    def __str__(self):
        return f"Interview AM {self.date.isoformat()}"


class InterviewShiftChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        ASSIGNED = "assigned", "Assigned"
        UNASSIGNED = "unassigned", "Unassigned"
        NOTE_CHANGED = "note_changed", "Note changed"
        REMOVED = "removed", "Removed"

    shift = models.ForeignKey(
        InterviewShift, on_delete=models.SET_NULL, related_name="changes", null=True, blank=True
    )
    shift_id_at_recording = models.PositiveBigIntegerField()
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_interview_shift_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError("Interview shift changes are retained and cannot be deleted.")


__all__ = ("InterviewShift", "InterviewShiftChange")
