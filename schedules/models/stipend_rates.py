from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class StipendRate(models.Model):
    """One current organization-wide hourly rate period for a stipend type."""

    class Kind(models.TextChoices):
        COMMUNITY_PRECEPTOR = "community_preceptor", "Community Preceptor"
        LATE_DOC = "late_doc", "Late Doc"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2)
    effective_start = models.DateField(null=True, blank=True)
    effective_end = models.DateField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("kind", "effective_start", "pk")

    def clean(self):
        super().clean()
        if isinstance(self.hourly_rate, Decimal) and self.hourly_rate <= 0:
            raise ValidationError({"hourly_rate": "Hourly rate must be greater than zero."})
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError({"effective_end": "End date cannot be before start date."})
        if not self.kind:
            return
        rates = getattr(self, "_validation_timeline_rates", None)
        if rates is None:
            rates = list(
                StipendRate.objects.filter(kind=self.kind, retired_at__isnull=True).exclude(
                    pk=self.pk
                )
            )
            rates.append(self)
        validate_stipend_rate_timeline(rates)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stipend rates are retained and cannot be deleted.")


class StipendRateChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        RETIRED = "retired", "Retired"

    rate = models.ForeignKey(StipendRate, on_delete=models.PROTECT, related_name="changes")
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_stipend_rate_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError("Stipend rate history is retained and cannot be deleted.")


def validate_stipend_rate_timeline(rates):
    """Reject configured timelines that cannot supply exactly one rate on every date."""
    rates = list(rates)
    if not rates:
        return
    rates.sort(
        key=lambda rate: (rate.effective_start is not None, rate.effective_start, rate.pk or 0)
    )
    if len(rates) == 1:
        rate = rates[0]
        if rate.effective_start is not None or rate.effective_end is not None:
            raise ValidationError("A single stipend rate must be unbounded in both directions.")
        return
    if rates[0].effective_start is not None:
        raise ValidationError("The first stipend rate must be unbounded into the past.")
    if rates[-1].effective_end is not None:
        raise ValidationError("The last stipend rate must be unbounded into the future.")
    for previous, current in pairwise(rates):
        if previous.effective_end is None or current.effective_start is None:
            raise ValidationError("Only the outer stipend rates may be unbounded.")
        if current.effective_start != previous.effective_end + timedelta(days=1):
            raise ValidationError("Stipend rate periods must be gapless and cannot overlap.")


__all__ = ("StipendRate", "StipendRateChange", "validate_stipend_rate_timeline")
