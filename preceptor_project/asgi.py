"""ASGI configuration for the preceptor scheduling project."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "preceptor_project.settings")

application = get_asgi_application()
