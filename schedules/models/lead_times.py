from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class LeadTimeSetting(models.Model):
    """One current organization-wide lead time for a projected-work family."""

    class Family(models.TextChoices):
        HOLIDAYS = "holidays", "Holidays"
        ACADEMIC_EVENTS = "academic_events", "Academic Events"
        PRECEPTING = "precepting", "Precepting"
        NORMAL_ROUNDING = "normal_rounding", "Normal Rounding"
        INTERVIEWS = "interviews", "Interviews"

    family = models.CharField(max_length=24, choices=Family.choices, unique=True)
    months = models.PositiveIntegerField()

    class Meta:
        ordering = ("family",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(months__gte=0),
                name="lead_time_months_are_non_negative",
            ),
        )

    def clean(self):
        super().clean()
        if self.family and self.family not in self.Family.values:
            raise ValidationError({"family": "Choose a valid lead-time family."})
        if self.months is None:
            raise ValidationError({"months": "A lead time in whole months is required."})
        if isinstance(self.months, bool) or not isinstance(self.months, int) or self.months < 0:
            raise ValidationError(
                {"months": "Lead time must be a non-negative whole number of months."}
            )

    def save(self, *args, **kwargs):
        if (
            self.pk
            and type(self).objects.filter(pk=self.pk).exists()
            and not getattr(self, "_lead_time_service_mutation", False)
        ):
            raise ValidationError(
                "Lead times must be changed through the lead-time policy service."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Lead-time settings cannot be deleted.")


class LeadTimeChange(models.Model):
    """An immutable record of one changed lead-time value."""

    setting = models.ForeignKey(LeadTimeSetting, on_delete=models.PROTECT, related_name="changes")
    family = models.CharField(max_length=24, choices=LeadTimeSetting.Family.choices)
    previous_months = models.PositiveIntegerField()
    resulting_months = models.PositiveIntegerField()
    note = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_lead_time_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")
        constraints = (
            models.CheckConstraint(
                condition=models.Q(previous_months__gte=0),
                name="lead_time_change_previous_months_are_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(resulting_months__gte=0),
                name="lead_time_change_resulting_months_are_non_negative",
            ),
        )

    def clean(self):
        super().clean()
        if self.setting_id and self.family != self.setting.family:
            raise ValidationError({"family": "History family must match its lead-time setting."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Lead-time history is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Lead-time history is retained and cannot be deleted.")


__all__ = ("LeadTimeChange", "LeadTimeSetting")
