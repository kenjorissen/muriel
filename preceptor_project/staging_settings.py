"""Fail-closed settings for the isolated synthetic staging deployment."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from .settings import *  # noqa: F403
from .settings import MIDDLEWARE


def required_environment(name):
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def staging_secret_key():
    value = required_environment("PRECEPTOR_STAGING_SECRET_KEY")
    if value != value.strip() or len(value) < 50 or value.startswith("django-insecure-"):
        raise ValueError(
            "PRECEPTOR_STAGING_SECRET_KEY must be a non-development secret of at least 50 characters"
        )
    return value


def staging_host():
    value = required_environment("PRECEPTOR_STAGING_ALLOWED_HOST")
    unsafe_characters = "/,:;?@#[]"
    if (
        value != value.strip()
        or value.startswith(".")
        or any(character.isspace() or character in unsafe_characters for character in value)
        or "*" in value
    ):
        raise ValueError("PRECEPTOR_STAGING_ALLOWED_HOST must be one exact host without wildcards")
    return value


def staging_origin(host):
    value = required_environment("PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port != 10000
        or value != f"https://{host}:10000"
    ):
        raise ValueError(
            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN must be the exact HTTPS origin "
            "for PRECEPTOR_STAGING_ALLOWED_HOST on port 10000"
        )
    return value


def staging_postgres_connection():
    for name, expected in (
        ("PRECEPTOR_STAGING_POSTGRES_HOST", "/var/run/postgresql"),
        ("PRECEPTOR_STAGING_POSTGRES_PORT", "5432"),
    ):
        value = os.environ.get(name)
        if value is not None and value != expected:
            raise ValueError(f"{name} must be {expected} for PostgreSQL 18/main")
    return {"HOST": "/var/run/postgresql", "PORT": "5432"}


def staging_postgres_identity(name):
    value = required_environment(name)
    if value != "muriel_staging":
        raise ValueError(f"{name} must be muriel_staging for staging database isolation")
    return value


DEBUG = False
SECRET_KEY = staging_secret_key()
# TEXT-CONTRACT: staging-synthetic-environment-marker — docs/staging.md requires this
# visible identifier to distinguish synthetic staging data from production; scope:
# capitalization, U+2014 em dash, and single spaces surrounding it;
# protecting test: schedules.tests.test_staging_instance.StagingSettingsTests.test_staging_uses_its_own_identity_cookies_static_state_and_dummy_email.
STAGING_ENVIRONMENT_MARKER = "STAGING — SYNTHETIC DATA"
STATIC_ROOT = Path("/srv/muriel-staging/shared/static")
STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

STAGING_ALLOWED_HOST = staging_host()
ALLOWED_HOSTS = [STAGING_ALLOWED_HOST]
CSRF_TRUSTED_ORIGINS = [staging_origin(STAGING_ALLOWED_HOST)]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": staging_postgres_identity("PRECEPTOR_STAGING_POSTGRES_NAME"),
        "USER": staging_postgres_identity("PRECEPTOR_STAGING_POSTGRES_USER"),
        "OPTIONS": {"connect_timeout": 5},
        **staging_postgres_connection(),
    }
}

# Gunicorn listens only on loopback, where Tailscale Serve terminates HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_NAME = "muriel_staging_sessionid"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = "muriel_staging_csrftoken"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

# Staging never sends an external message, including from password-reset flows.
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "preceptor_project.security.StagingPerformanceProbeMiddleware",
    *MIDDLEWARE,
]
