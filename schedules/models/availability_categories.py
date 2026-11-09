from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower, Trim

from .physicians import EffectiveRole, Physician


class CustomAvailabilityCategory(models.Model):
    """A clinic-defined availability label with one fixed scheduling impact."""

    class Impact(models.TextChoices):
        AVAILABLE = "available", "Available"
        BACKUP_ONLY = "backup_only", "Backup only"
        NOT_AVAILABLE = "not_available", "Not available"

    class SystemKey(models.TextChoices):
        ADMIN = "admin", "Admin"

    name = models.CharField(max_length=120)
    impact = models.CharField(max_length=16, choices=Impact.choices)
    is_active = models.BooleanField(default=True)
    system_key = models.CharField(max_length=24, choices=SystemKey.choices, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("is_active", Lower("name"), "name", "pk")
        constraints = (
            models.UniqueConstraint(
                Lower(Trim("name")), name="custom_availability_category_usable_name_unique"
            ),
            models.UniqueConstraint(
                fields=("system_key",),
                condition=models.Q(system_key__isnull=False),
                name="custom_availability_category_system_key_unique",
            ),
        )

    def clean(self):
        super().clean()
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Schedule option name cannot be blank."})
        self._validate_admin_immutability()

    def _validate_admin_immutability(self):
        if self.system_key == self.SystemKey.ADMIN:
            if self.name != "Admin":
                raise ValidationError({"name": "The Admin system option name cannot be changed."})
            if self.impact != self.Impact.NOT_AVAILABLE:
                raise ValidationError(
                    {"impact": "The Admin system option impact cannot be changed."}
                )
            if not self.is_active:
                raise ValidationError(
                    {"is_active": "The Admin system option cannot be deactivated."}
                )
        if self.pk:
            stored = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("system_key", "name", "impact", "is_active")
                .first()
            )
            if stored and stored["system_key"] == self.SystemKey.ADMIN:
                if self.system_key != self.SystemKey.ADMIN:
                    raise ValidationError(
                        {"system_key": "The Admin system identity cannot be changed."}
                    )
                if self.name != stored["name"]:
                    raise ValidationError(
                        {"name": "The Admin system option name cannot be changed."}
                    )
                if self.impact != stored["impact"]:
                    raise ValidationError(
                        {"impact": "The Admin system option impact cannot be changed."}
                    )
                if self.is_active != stored["is_active"]:
                    raise ValidationError(
                        {"is_active": "The Admin system option cannot be deactivated."}
                    )

    def save(self, *args, **kwargs):
        self._validate_admin_immutability()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Schedule options are retained and cannot be deleted.")

    def __str__(self):
        return self.name


class CustomAvailabilityCategoryAssignment(models.Model):
    """A retained physician applicability record for one custom category."""

    category = models.ForeignKey(
        CustomAvailabilityCategory, on_delete=models.PROTECT, related_name="assignments"
    )
    physician = models.ForeignKey(
        Physician, on_delete=models.PROTECT, related_name="custom_availability_category_assignments"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("category", "physician")
        constraints = (
            models.UniqueConstraint(
                fields=("category", "physician"), name="custom_availability_category_one_assignment"
            ),
        )

    def clean(self):
        super().clean()
        if not self.is_active:
            return
        if not self.category.is_active:
            raise ValidationError({"category": "Inactive schedule options cannot be assigned."})
        if not EffectiveRole.objects.filter(
            physician=self.physician, role=EffectiveRole.Role.FACULTY
        ).exists():
            raise ValidationError(
                {"physician": "Schedule options can be assigned only to Faculty physicians."}
            )

    def delete(self, *args, **kwargs):
        raise ValidationError("Schedule option assignments are retained and cannot be deleted.")


class CustomAvailabilityCategoryChange(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CORRECTED = "corrected", "Corrected"
        DEACTIVATED = "deactivated", "Deactivated"
        ASSIGNED = "assigned", "Assigned to physician"
        UNASSIGNED = "unassigned", "Unassigned from physician"

    category = models.ForeignKey(
        CustomAvailabilityCategory, on_delete=models.PROTECT, related_name="changes"
    )
    physician = models.ForeignKey(
        Physician,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="custom_category_changes",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_custom_availability_category_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def delete(self, *args, **kwargs):
        raise ValidationError("Schedule option change history is retained and cannot be deleted.")


__all__ = (
    "CustomAvailabilityCategory",
    "CustomAvailabilityCategoryAssignment",
    "CustomAvailabilityCategoryChange",
)
