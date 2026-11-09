"""Public-boundary, error, and logging helpers for production requests."""

import logging
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.db import connection
from django.http import HttpResponse
from django.template.loader import get_template
from django.test.utils import CaptureQueriesContext

logger = logging.getLogger("muriel")
logger.addHandler(logging.NullHandler())

PUBLIC_VIEW_NAMES = frozenset(
    {
        "health",
        "sign-in",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
        "scheduler-invitation-accept",
        "scheduler-invitation-complete",
    }
)


class RequestCorrelationMiddleware:
    """Attach an internally generated correlation identifier to every request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid4().hex
        try:
            response = self.get_response(request)
        except Exception:
            logger.error("unhandled_request request_id=%s", request.request_id)
            raise
        response["X-Request-ID"] = request.request_id
        return response


class StagingPerformanceProbeMiddleware:
    """Expose a query count only to an explicitly marked staging probe request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            getattr(settings, "STAGING_ENVIRONMENT_MARKER", None) != "STAGING — SYNTHETIC DATA"
            or request.META.get("HTTP_X_MURIEL_PERFORMANCE_PROBE") != "staging-performance-v1"
        ):
            return self.get_response(request)
        with CaptureQueriesContext(connection) as queries:
            response = self.get_response(request)
        existing = response.get("Server-Timing")
        query_timing = f'db-queries;desc="{len(queries)}"'
        response["Server-Timing"] = f"{existing}, {query_timing}" if existing else query_timing
        return response


class PublicBoundaryMiddleware:
    """Default-deny application routes except the few anonymous workflows."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if (
            request.resolver_match.view_name in PUBLIC_VIEW_NAMES
            or request.user.is_authenticated
            or (
                request.resolver_match.view_name.startswith("admin:")
                and request.META.get("HTTP_X_MURIEL_ADMIN") == "1"
            )
        ):
            return None
        logger.warning(
            "anonymous_request_denied request_id=%s route=%s",
            request.request_id,
            request.resolver_match.view_name,
        )
        return redirect_to_login(request.get_full_path())


class ProductionErrorPageMiddleware:
    """Replace explicit bad-request responses with the production-safe error page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            not settings.DEBUG
            and response.status_code == 400
            and not getattr(response, "_muriel_safe_error_page", False)
        ):
            return error_response(request, 400)
        return response


def error_response(request, status):
    level = logging.ERROR if status == 500 else logging.WARNING
    logger.log(
        level,
        "request_error status=%s request_id=%s route=%s",
        status,
        getattr(request, "request_id", "unavailable"),
        getattr(getattr(request, "resolver_match", None), "view_name", "unresolved"),
    )
    # Error handling can run before authentication middleware has set request.user.
    response = HttpResponse(get_template(f"errors/{status}.html").render(), status=status)
    response._muriel_safe_error_page = True
    return response


def bad_request(request, exception=None):
    return error_response(request, 400)


def permission_denied(request, exception=None):
    return error_response(request, 403)


def csrf_failure(request, reason=""):
    return error_response(request, 403)


def page_not_found(request, exception=None):
    return error_response(request, 404)


def server_error(request):
    return error_response(request, 500)
