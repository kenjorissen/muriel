"""Session-backed physician presentation scope for schedule workspaces."""

from urllib.parse import parse_qsl, urlsplit

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models.physicians import Physician

SESSION_KEY = "physician_presentation_scope"


def physician_scope(request):
    """Return validated selected physicians and remove stale session values."""
    raw_ids = request.session.get(SESSION_KEY, ())
    if not isinstance(raw_ids, (list, tuple)):
        raw_ids = ()
    ids = []
    for value in raw_ids:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            and value not in ids
        ):
            ids.append(value)
    physicians_by_id = {
        physician.pk: physician for physician in Physician.objects.filter(pk__in=ids)
    }
    selected = tuple(
        physicians_by_id[identifier] for identifier in ids if identifier in physicians_by_id
    )
    validated_ids = [physician.pk for physician in selected]
    if validated_ids:
        if list(raw_ids) != validated_ids:
            request.session[SESSION_KEY] = validated_ids
    elif SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
    return selected


def physician_scope_ids(request):
    return {physician.pk for physician in physician_scope(request)}


def context(request):
    if not request.user.is_authenticated:
        return {}
    selected = physician_scope(request)
    return {
        "physician_scope_choices": Physician.objects.all(),
        "physician_scope_selected": selected,
        "physician_scope_selected_ids": {physician.pk for physician in selected},
        "physician_scope_return": _safe_current_return(request),
    }


def _safe_return(request, value):
    if not value or not isinstance(value, str):
        return reverse("monthly-workspace")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or not url_has_allowed_host_and_scheme(
            value, {request.get_host()}, require_https=request.is_secure()
        )
    ):
        return reverse("monthly-workspace")
    if any("://" in item or item.startswith("//") for _, item in parse_qsl(parsed.query)):
        return reverse("monthly-workspace")
    try:
        resolve(parsed.path)
    except Exception:  # Invalid local paths should not become a redirect destination.
        return reverse("monthly-workspace")
    return value


def _safe_current_return(request):
    value = request.get_full_path()
    return value if _safe_return(request, value) == value else request.path


@login_required
@require_POST
def set_physician_scope(request):
    selected_ids = []
    values = () if request.POST.get("reset") else request.POST.getlist("physicians")
    for value in values:
        try:
            identifier = int(value)
        except TypeError, ValueError:
            continue
        if str(identifier) != value or identifier <= 0 or identifier in selected_ids:
            continue
        selected_ids.append(identifier)
    existing_ids = set(Physician.objects.filter(pk__in=selected_ids).values_list("pk", flat=True))
    selected_ids = [identifier for identifier in selected_ids if identifier in existing_ids]
    if selected_ids:
        request.session[SESSION_KEY] = selected_ids
    else:
        request.session.pop(SESSION_KEY, None)
    return redirect(_safe_return(request, request.POST.get("return")))
