"""WSGI configuration for the preceptor scheduling project."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "preceptor_project.settings")

application = get_wsgi_application()
