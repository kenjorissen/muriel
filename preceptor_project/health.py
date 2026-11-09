"""Public application-readiness response."""

from django.db import connection
from django.http import HttpResponse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_http_methods


@require_http_methods(["GET", "HEAD"])
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        response = HttpResponse("unavailable\n", content_type="text/plain", status=503)
    else:
        response = HttpResponse("ok\n", content_type="text/plain")
    add_never_cache_headers(response)
    return response
