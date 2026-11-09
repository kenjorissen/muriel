"""Django settings used only by the test suite."""

import multiprocessing

from .settings import *  # noqa: F403

# Django's SQLite test database cloning requires fork on Python 3.14.
multiprocessing.set_start_method("fork")

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
