"""Template context for explicitly marked non-production environments."""

from django.conf import settings


def context(_request):
    return {"staging_environment_marker": getattr(settings, "STAGING_ENVIRONMENT_MARKER", "")}
