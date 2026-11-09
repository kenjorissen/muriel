"""Production-only handling for local reverse-proxy security headers."""


class UnixSocketProxySSLHeaderMiddleware:
    """Accept proxy headers only from Gunicorn's owned Unix-socket transport."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Gunicorn represents an accepted Unix-socket peer with an empty address.
        if request.META.get("REMOTE_ADDR") != "":
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
            request.META.pop("HTTP_X_MURIEL_ADMIN", None)
        return self.get_response(request)
