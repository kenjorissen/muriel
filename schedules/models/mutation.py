from django.db import models


class SchedulingMutationAuthority(models.Model):
    """The single row shared by scheduling writes and coherent scheduling reads."""

    singleton = models.BooleanField(default=True, unique=True, editable=False)


__all__ = ("SchedulingMutationAuthority",)
