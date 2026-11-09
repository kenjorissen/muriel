"""Fail-closed settings for the documented production deployment."""

import os
from urllib.parse import urlsplit

from .settings import *  # noqa: F403
from .settings import BASE_DIR, MIDDLEWARE


def required_environment(name):
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def production_secret_key():
    value = required_environment("PRECEPTOR_PRODUCTION_SECRET_KEY")
    if value != value.strip() or len(value) < 50 or value.startswith("django-insecure-"):
        raise ValueError(
            "PRECEPTOR_PRODUCTION_SECRET_KEY must be a non-development secret of at least 50 characters"
        )
    return value


def exact_host(name):
    value = required_environment(name)
    unsafe_characters = "/,:;?@#[]"
    if (
        value != value.strip()
        or value.startswith(".")
        or any(character.isspace() or character in unsafe_characters for character in value)
        or "*" in value
    ):
        raise ValueError(f"{name} must be one exact host without wildcards")
    return value


def exact_https_origin(name, host):
    value = required_environment(name)
    parsed = urlsplit(value)
    if (
        value != f"https://{host}"
        or parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{name} must be the exact HTTPS origin for {host}")
    return value


def secondary_public_origin():
    host_name = "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST"
    origin_name = "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN"
    host_configured = host_name in os.environ
    origin_configured = origin_name in os.environ
    if host_configured != origin_configured:
        raise ValueError(f"{host_name} and {origin_name} must be configured together")
    if not host_configured:
        return None

    host = exact_host(host_name)
    origin = exact_https_origin(origin_name, host)
    return host, origin


POSTGRES_SOCKET_DIRECTORY = "/var/run/postgresql"
POSTGRES_PORT = "5432"


def production_postgres_connection():
    for name, expected in (
        ("PRECEPTOR_POSTGRES_HOST", POSTGRES_SOCKET_DIRECTORY),
        ("PRECEPTOR_POSTGRES_PORT", POSTGRES_PORT),
    ):
        value = os.environ.get(name)
        if value is not None and value != expected:
            raise ValueError(f"{name} must be {expected} for PostgreSQL 18/main")
    return {"HOST": POSTGRES_SOCKET_DIRECTORY, "PORT": POSTGRES_PORT}


DEBUG = False
SECRET_KEY = production_secret_key()
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

PRODUCTION_ALLOWED_HOST = exact_host("PRECEPTOR_PRODUCTION_ALLOWED_HOST")
PRODUCTION_CSRF_TRUSTED_ORIGIN = exact_https_origin(
    "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN", PRODUCTION_ALLOWED_HOST
)
SECONDARY_PUBLIC_ORIGIN = secondary_public_origin()
if SECONDARY_PUBLIC_ORIGIN and SECONDARY_PUBLIC_ORIGIN[0] == PRODUCTION_ALLOWED_HOST:
    raise ValueError("production primary and secondary allowed hosts must differ")
ALLOWED_HOSTS = [PRODUCTION_ALLOWED_HOST]
CSRF_TRUSTED_ORIGINS = [PRODUCTION_CSRF_TRUSTED_ORIGIN]
if SECONDARY_PUBLIC_ORIGIN:
    secondary_host, secondary_origin = SECONDARY_PUBLIC_ORIGIN
    ALLOWED_HOSTS.append(secondary_host)
    CSRF_TRUSTED_ORIGINS.append(secondary_origin)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required_environment("PRECEPTOR_POSTGRES_NAME"),
        "USER": required_environment("PRECEPTOR_POSTGRES_USER"),
        "OPTIONS": {"connect_timeout": 5},
        **production_postgres_connection(),
    }
}
if password := os.environ.get("PRECEPTOR_POSTGRES_PASSWORD"):
    DATABASES["default"]["PASSWORD"] = password

# Only Nginx's connection through Gunicorn's owned Unix socket may assert proxy headers.
MIDDLEWARE = [
    "preceptor_project.production_proxy.UnixSocketProxySSLHeaderMiddleware",
    *MIDDLEWARE,
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# The Funnel hostname is part of shared ts.net; these unsafe recommendations are deliberate.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_secrets": {"()": "preceptor_project.log_safety.SecretRedactionFilter"}},
    "formatters": {
        "muriel": {"format": "{levelname} muriel request_id={request_id} {message}", "style": "{"}
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["redact_secrets"],
            "formatter": "muriel",
        }
    },
    "loggers": {
        "muriel": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
