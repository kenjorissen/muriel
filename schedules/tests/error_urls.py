from django.urls import path

from preceptor_project.security import bad_request, page_not_found, permission_denied, server_error
from preceptor_project.urls import urlpatterns as application_urlpatterns


def broken_view(request):
    raise RuntimeError("secret-token-must-not-reach-the-response")


urlpatterns = [*application_urlpatterns, path("broken/", broken_view)]

handler400 = bad_request
handler403 = permission_denied
handler404 = page_not_found
handler500 = server_error
