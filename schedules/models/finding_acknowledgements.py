from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class FindingAcknowledgement(models.Model):
    """A retained acknowledgement for one live scheduling finding occurrence."""

    identity = models.JSONField()
    identity_key = models.TextField()
    reason = models.TextField()
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="finding_acknowledgements",
    )
    acknowledged_at = models.DateTimeField(auto_now_add=True)
    no_longer_applies_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="removed_finding_acknowledgements",
    )
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-acknowledged_at", "-pk")
        constraints = (
            models.UniqueConstraint(
                fields=("identity_key",),
                condition=Q(no_longer_applies_at__isnull=True),
                name="one_current_finding_acknowledgement",
            ),
        )

    @property
    def is_current(self):
        return self.no_longer_applies_at is None

    @property
    def was_deliberately_removed(self):
        return self.removed_at is not None

    def delete(self, *args, **kwargs):
        raise ValidationError("Finding acknowledgements are retained and cannot be deleted.")

    def __str__(self):
        return f"{self.identity.get('code', 'Finding')} acknowledged at {self.acknowledged_at}"
