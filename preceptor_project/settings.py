"""Django settings for the preceptor scheduling project."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-development-only-key"
DEBUG = True

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1")


def development_allowed_hosts(value=None):
    if value is None:
        value = os.environ.get("PRECEPTOR_ALLOWED_HOSTS")
    if value is None:
        return list(DEFAULT_ALLOWED_HOSTS)

    hosts = [host.strip() for host in value.split(",") if host.strip()]
    if not hosts:
        raise ValueError("PRECEPTOR_ALLOWED_HOSTS must name at least one host")
    if "*" in hosts:
        raise ValueError("PRECEPTOR_ALLOWED_HOSTS cannot include a wildcard host")
    return hosts


def environment_boolean(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def environment_integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


ALLOWED_HOSTS = development_allowed_hosts()

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "schedules",
]

MIDDLEWARE = [
    "preceptor_project.security.RequestCorrelationMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "preceptor_project.security.PublicBoundaryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "preceptor_project.security.ProductionErrorPageMiddleware",
]

ROOT_URLCONF = "preceptor_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "schedules" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "schedules.environment.context",
                "schedules.account_administration.context",
                "schedules.physician_scope.context",
            ],
        },
    },
]

WSGI_APPLICATION = "preceptor_project.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTHENTICATION_BACKENDS = ["schedules.authentication.UsernameOrEmailBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "sign-in"
LOGIN_REDIRECT_URL = "master-schedule"
CSRF_FAILURE_VIEW = "preceptor_project.security.csrf_failure"

EMAIL_BACKEND = os.environ.get(
    "PRECEPTOR_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("PRECEPTOR_DEFAULT_FROM_EMAIL", "muriel@localhost")
EMAIL_HOST = os.environ.get("PRECEPTOR_EMAIL_HOST", "")
EMAIL_PORT = environment_integer("PRECEPTOR_EMAIL_PORT", 587, 1, 65535)
EMAIL_HOST_USER = os.environ.get("PRECEPTOR_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("PRECEPTOR_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = environment_boolean("PRECEPTOR_EMAIL_USE_TLS", True)
EMAIL_USE_SSL = environment_boolean("PRECEPTOR_EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = environment_integer("PRECEPTOR_EMAIL_TIMEOUT", 10, 1, 60)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ValueError("PRECEPTOR_EMAIL_USE_TLS and PRECEPTOR_EMAIL_USE_SSL cannot both be enabled")

SCHEDULER_INVITATION_MAX_AGE = environment_integer(
    "PRECEPTOR_SCHEDULER_INVITATION_MAX_AGE", 7 * 24 * 60 * 60, 60, 31 * 24 * 60 * 60
)
ACADEMIC_EVENT_IMPORT_PREVIEW_MAX_AGE = environment_integer(
    "PRECEPTOR_ACADEMIC_EVENT_IMPORT_PREVIEW_MAX_AGE", 10 * 60, 60, 60 * 60
)
PRECEPTING_WORKBOOK_IMPORT_PREVIEW_MAX_AGE = environment_integer(
    "PRECEPTOR_PRECEPTING_WORKBOOK_IMPORT_PREVIEW_MAX_AGE", 10 * 60, 60, 60 * 60
)
SCHEDULER_INVITATION_DELIVERY_MODE = os.environ.get(
    "PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE", "manual"
).lower()
if SCHEDULER_INVITATION_DELIVERY_MODE not in {"manual", "smtp"}:
    raise ValueError("PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE must be manual or smtp")

USE_X_FORWARDED_HOST = environment_boolean("PRECEPTOR_TRUST_PROXY", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if USE_X_FORWARDED_HOST else None
