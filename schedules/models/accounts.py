from django.conf import settings
from django.db import models


class AccountChange(models.Model):
    class Action(models.TextChoices):
        UPDATED = "updated", "Updated"
        INVITED = "invited", "Invited"
        RESENT = "resent", "Invitation resent"
        REVOKED = "revoked", "Invitation revoked"
        ACCEPTED = "accepted", "Invitation accepted"
        DELIVERED = "delivered", "Invitation delivered"
        DELIVERY_FAILED = "delivery_failed", "Invitation delivery failed"

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="muriel_account_changes"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    previous_value = models.JSONField(null=True, blank=True)
    recorded_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_account_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def __str__(self):
        return f"{self.account.get_username()} {self.get_action_display().lower()} at {self.changed_at}"


class SchedulerInvitation(models.Model):
    """A non-secret record of a scheduler's invitation lifecycle."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        SUPERSEDED = "superseded", "Superseded"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        MANUAL = "manual", "Prepared for manual delivery"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="scheduler_invitations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_scheduler_invitations",
    )
    scheduling_administrator = models.BooleanField(default=False)
    secret_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    delivery_status = models.CharField(
        max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    delivery_error = models.CharField(max_length=200, blank=True, default="")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def __str__(self):
        return f"Invitation for {self.account.get_username()} ({self.get_status_display().lower()})"
