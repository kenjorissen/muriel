"""Muriel's scheduler account workspace."""

import logging
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..forms.accounts import SchedulerAccountForm, SchedulerInvitationForm
from ..models.accounts import AccountChange, SchedulerInvitation
from ..services._mutation import require_scheduling_administrator
from ..services.accounts import (
    SCHEDULING_ADMINISTRATORS_GROUP,
    accept_scheduler_invitation,
    create_scheduler_invitation,
    invitation_for_token,
    resend_scheduler_invitation,
    revoke_scheduler_invitation,
    update_scheduler_account,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from ._invitations import deliver_invitation_response

logger = logging.getLogger("muriel")


def account_administration_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        require_scheduling_administrator(request.user)
        return view(request, *args, **kwargs)

    return wrapped


def _account_initial(user):
    return {
        "username": user.username,
        "email": user.email,
        "is_active": user.is_active,
        "scheduling_administrator": user.groups.filter(
            name=SCHEDULING_ADMINISTRATORS_GROUP
        ).exists(),
        "physician": getattr(user, "physician_identity", None),
    }


@login_required
@account_administration_required
def account_workspace(request):
    accounts = list(
        get_user_model()
        .objects.select_related("physician_identity")
        .prefetch_related("groups")
        .order_by(Lower("username"), "username", "pk")
    )
    for account in accounts:
        account.is_django_administrator = account.is_staff or account.is_superuser
        account.is_scheduling_administrator = any(
            group.name == SCHEDULING_ADMINISTRATORS_GROUP for group in account.groups.all()
        )
        account.latest_invitation = SchedulerInvitation.objects.filter(account=account).first()
        if account.latest_invitation:
            account.invitation_status = account.latest_invitation.get_status_display()
            if (
                account.latest_invitation.status == SchedulerInvitation.Status.PENDING
                and account.latest_invitation.expires_at <= timezone.now()
            ):
                account.invitation_status = "Expired"
    return render(request, "schedules/account_workspace.html", {"accounts": accounts})


@login_required
@account_administration_required
def account_invite(request):
    form = SchedulerInvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            invitation, token = create_scheduler_invitation(
                scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return deliver_invitation_response(request, invitation, token, "Invitation")
    return render(request, "schedules/account_invite_form.html", {"form": form})


@login_required
@account_administration_required
def account_invitation_resend(request, account_id):
    if request.method != "POST":
        raise PermissionDenied("Invitation delivery requires a form submission.")
    account = get_object_or_404(get_user_model(), pk=account_id)
    try:
        invitation, token = resend_scheduler_invitation(
            account=account, scheduler=request.user, reason=request.POST.get("reason", "")
        )
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        return redirect("account-workspace")
    return deliver_invitation_response(request, invitation, token, "Invitation resent")


@login_required
@account_administration_required
def account_invitation_revoke(request, account_id):
    if request.method != "POST":
        raise PermissionDenied("Invitation revocation requires a form submission.")
    account = get_object_or_404(get_user_model(), pk=account_id)
    try:
        revoke_scheduler_invitation(
            account=account, scheduler=request.user, reason=request.POST.get("reason", "")
        )
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, "Invitation revoked. The account remains available to re-invite.")
    return redirect("account-workspace")


def scheduler_invitation_accept(request, token):
    try:
        invitation = invitation_for_token(token)
    except ValidationError:
        logger.warning("invitation_unavailable request_id=%s", request.request_id)
        return render(request, "registration/scheduler_invitation_invalid.html")
    form = SetPasswordForm(invitation.account, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            accept_scheduler_invitation(token=token, password=form.cleaned_data["new_password1"])
        except ValidationError:
            logger.warning("invitation_unavailable request_id=%s", request.request_id)
            return render(request, "registration/scheduler_invitation_invalid.html")
        logger.info("invitation_accepted request_id=%s", request.request_id)
        return redirect("scheduler-invitation-complete")
    return render(request, "registration/scheduler_invitation_accept.html", {"form": form})


def scheduler_invitation_complete(request):
    return render(request, "registration/scheduler_invitation_complete.html")


@login_required
@account_administration_required
def account_edit(request, account_id):
    account = get_object_or_404(
        get_user_model().objects.select_related("physician_identity").prefetch_related("groups"),
        pk=account_id,
    )
    if account.is_staff or account.is_superuser:
        if request.method == "POST":
            raise PermissionDenied("Django administrator accounts are managed in Django Admin.")
        return render(request, "schedules/account_read_only.html", {"account": account})

    form = SchedulerAccountForm(
        request.POST or None,
        account=account,
        initial=_account_initial(account),
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_scheduler_account(
                account=account,
                scheduler=request.user,
                **form.cleaned_data,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("account-workspace")
    return render(request, "schedules/account_form.html", {"account": account, "form": form})


@login_required
@account_administration_required
def account_history(request):
    return render(
        request,
        "schedules/account_history.html",
        {
            "account_history_table": _account_history_table(
                AccountChange.objects.select_related("account", "changed_by")
            )
        },
    )


def _account_history_table(changes):
    return DataTable(
        identity="account-history",
        label="Account change history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Account",
                "Recorded",
                "Actor",
                "Action",
                "Prior value",
                "Recorded value",
                "Note",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(change.account.get_username(), row_header=True),
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No account changes recorded.",
    )
