from django.conf import settings
from django.db import models

from .physicians import Physician


class PreceptingSolverProposal(models.Model):
    """A retained, human-reviewed solver result, separate from the draft."""

    class Mode(models.TextChoices):
        FILL = "fill", "Fill open slots"
        REPAIR = "repair", "Repair findings"

    mode = models.CharField(max_length=16, choices=Mode.choices)
    solve_start_date = models.DateField()
    solve_end_date = models.DateField()
    balance_start_date = models.DateField()
    balance_end_date = models.DateField()
    policy_version = models.CharField(max_length=100)
    solve_settings = models.JSONField(default=dict)
    input_fingerprint = models.CharField(max_length=64)
    input_snapshot = models.JSONField()
    proposed_assignments = models.JSONField()
    before_findings = models.JSONField()
    after_findings = models.JSONField()
    workload_effects = models.JSONField()
    compromises = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_precepting_solver_proposals",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="last_edited_precepting_solver_proposals",
        null=True,
        blank=True,
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="applied_precepting_solver_proposals",
        null=True,
        blank=True,
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-updated_at", "-pk")


class PreceptingSolverProposalEdit(models.Model):
    """One attributed manual choice layered over an immutable solver result."""

    proposal = models.ForeignKey(
        PreceptingSolverProposal,
        on_delete=models.CASCADE,
        related_name="edits",
    )
    assignment_type = models.CharField(max_length=16)
    date = models.DateField()
    period_of_day = models.CharField(max_length=8, blank=True)
    position = models.CharField(max_length=16)
    previous_value = models.JSONField(null=True, blank=True)
    resulting_value = models.JSONField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="edited_precepting_solver_proposals",
    )
    edited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("edited_at", "pk")


class PreceptingSolverLateDocReview(models.Model):
    """The required human disposition for one proposed Monday Late Doc choice."""

    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        REPLACED = "replaced", "Replaced"

    proposal = models.ForeignKey(
        PreceptingSolverProposal,
        on_delete=models.CASCADE,
        related_name="late_doc_reviews",
    )
    date = models.DateField()
    proposed_physician = models.ForeignKey(
        Physician,
        on_delete=models.PROTECT,
        related_name="proposed_late_doc_reviews",
    )
    decision = models.CharField(
        max_length=16,
        choices=Decision.choices,
        default=Decision.PENDING,
    )
    replacement_physician = models.ForeignKey(
        Physician,
        on_delete=models.PROTECT,
        related_name="replacement_late_doc_reviews",
        null=True,
        blank=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_precepting_solver_late_docs",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("date", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("proposal", "date"), name="unique_solver_proposal_late_doc_review"
            ),
        )


class PreceptingSolverProposalComparison(models.Model):
    """Read-only counterfactual evidence retained beside, never in, a proposal."""

    proposal = models.ForeignKey(
        PreceptingSolverProposal,
        on_delete=models.CASCADE,
        related_name="comparisons",
    )
    result = models.JSONField()
    proposal_revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_precepting_solver_proposal_comparisons",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")


class PreceptingSolverProposalReconciliation(models.Model):
    """Provenance for a proposal recreated from a newer ordinary draft."""

    proposal = models.OneToOneField(
        PreceptingSolverProposal,
        on_delete=models.CASCADE,
        related_name="reconciliation",
    )
    source_proposal = models.ForeignKey(
        PreceptingSolverProposal,
        on_delete=models.PROTECT,
        related_name="reconciled_proposals",
    )
    starting_input_fingerprint = models.CharField(max_length=64)
    current_input_fingerprint = models.CharField(max_length=64)
    choices = models.JSONField(default=list)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reconciled_precepting_solver_proposals",
    )
    reconciled_at = models.DateTimeField(auto_now_add=True)
