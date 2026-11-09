"""Muriel account administration mutations and invitations."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ..models.accounts import AccountChange, SchedulerInvitation
from ..models.physicians import Physician
from ._mutation import (
    normalize_note,
    require_scheduling_administrator,
    scheduling_administration_mutation,
)
from .physicians import create_physician, link_physician_user, unlink_physician_user

SCHEDULING_ADMINISTRATORS_GROUP = "Scheduling Administrators"
INVITATION_SIGNING_SALT = "schedules.scheduler-invitation"
PRODUCTION_LOGIN_ACCESS_CLEAR_REASON = "Production login access cleared."


@dataclass(frozen=True)
class InvitationDelivery:
    mode: str
    delivered: bool
    message: str | None = None
    setup_link: str | None = None


@transaction.atomic
def update_scheduler_account(
    *,
    account,
    username,
    email,
    is_active,
    scheduling_administrator,
    physician,
    scheduler,
    reason="",
):
    """Update the narrow scheduler fields and retain a redacted audit entry."""
    require_scheduling_administrator(scheduler)
    user = get_user_model().objects.select_for_update().get(pk=account.pk)
    _require_mutable_scheduler(user)
    if SchedulerInvitation.objects.filter(
        account=user, status=SchedulerInvitation.Status.PENDING
    ).exists():
        raise ValidationError(
            "Pending invitations can be resent, revoked, or re-invited, not edited."
        )

    email = email.strip()
    if not email:
        raise ValidationError({"email": "A scheduler account needs a configured email address."})
    if _email_in_use(email, exclude=user):
        raise ValidationError({"email": "An account already uses this email address."})

    current_physician = Physician.objects.select_for_update().filter(user=user).first()
    selected_physician = None
    if physician is not None:
        selected_physician = Physician.objects.select_for_update().get(pk=physician.pk)
        if selected_physician.user_id not in (None, user.pk):
            raise ValidationError(
                {"physician": "This physician is already linked to another account."}
            )

    previous_value = _account_value(user, current_physician)
    user.username = username
    user.email = email
    try:
        with transaction.atomic():
            user.full_clean(
                exclude=("password", "last_login", "date_joined", "groups", "user_permissions")
            )
            user.save(update_fields=("username", "email"))
    except IntegrityError as error:
        if _email_in_use(email, exclude=user):
            raise ValidationError(
                {"email": "An account already uses this email address."}
            ) from error
        raise ValidationError({"username": "An account already uses this username."}) from error

    if current_physician and current_physician != selected_physician:
        unlink_physician_user(physician=current_physician, scheduler=scheduler, reason=reason)
    if selected_physician and selected_physician != current_physician:
        link_physician_user(
            physician=selected_physician, user=user, scheduler=scheduler, reason=reason
        )

    user.is_active = is_active
    user.save(update_fields=("is_active",))
    scheduling_administrators, _ = Group.objects.get_or_create(name=SCHEDULING_ADMINISTRATORS_GROUP)
    if scheduling_administrator:
        user.groups.add(scheduling_administrators)
    else:
        user.groups.remove(scheduling_administrators)

    user.refresh_from_db()
    recorded_value = _account_value(
        user, Physician.objects.select_related("user").filter(user=user).first()
    )
    if previous_value != recorded_value:
        AccountChange.objects.create(
            account=user,
            action=AccountChange.Action.UPDATED,
            previous_value=previous_value,
            recorded_value=recorded_value,
            reason=normalize_note(reason),
            changed_by=scheduler,
        )
    return user


@transaction.atomic
def create_scheduler_invitation(
    *, username, email, scheduling_administrator, physician, scheduler, reason=""
):
    """Create an inactive account and its first invitation without retaining a credential."""
    require_scheduling_administrator(scheduler)
    email = email.strip()
    if not email:
        raise ValidationError({"email": "A scheduler account needs a configured email address."})
    if _email_in_use(email):
        raise ValidationError({"email": "An account already uses this email address."})

    selected_physician = None
    if physician is not None:
        selected_physician = Physician.objects.select_for_update().get(pk=physician.pk)
        if selected_physician.user_id is not None:
            raise ValidationError(
                {"physician": "This physician is already linked to another account."}
            )

    user = get_user_model()(username=username, email=email, is_active=False)
    user.set_unusable_password()
    try:
        with transaction.atomic():
            user.full_clean(exclude=("last_login", "date_joined", "groups", "user_permissions"))
            user.save()
    except IntegrityError as error:
        if _email_in_use(email):
            raise ValidationError(
                {"email": "An account already uses this email address."}
            ) from error
        raise ValidationError({"username": "An account already uses this username."}) from error

    if selected_physician:
        link_physician_user(
            physician=selected_physician, user=user, scheduler=scheduler, reason=reason
        )
    invitation, token = _new_invitation(
        account=user, scheduling_administrator=scheduling_administrator, scheduler=scheduler
    )
    _record_invitation_change(
        account=user,
        action=AccountChange.Action.INVITED,
        scheduler=scheduler,
        reason=reason,
        value=_invitation_value(invitation),
    )
    return invitation, token


@scheduling_administration_mutation
@transaction.atomic
def create_physician_with_invitation(
    *,
    display_name,
    email,
    scheduler,
    reason="",
    roles=None,
    role=None,
    effective_start=None,
    effective_end=None,
):
    """Create a physician and its ordinary account invitation as one administrative mutation."""
    email = email.strip().casefold()
    physician = create_physician(
        display_name=display_name,
        scheduler=scheduler,
        reason=reason,
        roles=roles,
        role=role,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    invitation, token = create_scheduler_invitation(
        username=email,
        email=email,
        scheduling_administrator=False,
        physician=physician,
        scheduler=scheduler,
        reason=reason,
    )
    return physician, invitation, token


@transaction.atomic
def resend_scheduler_invitation(*, account, scheduler, reason=""):
    require_scheduling_administrator(scheduler)
    user = get_user_model().objects.select_for_update().get(pk=account.pk)
    _require_mutable_scheduler(user)
    if user.is_active:
        raise ValidationError("Active accounts do not need an invitation.")
    prior = SchedulerInvitation.objects.select_for_update().filter(account=user).first()
    if not prior:
        raise ValidationError("This account has no invitation to resend.")
    SchedulerInvitation.objects.filter(
        account=user, status=SchedulerInvitation.Status.PENDING
    ).update(status=SchedulerInvitation.Status.SUPERSEDED)
    invitation, token = _new_invitation(
        account=user,
        scheduling_administrator=prior.scheduling_administrator,
        scheduler=scheduler,
    )
    _record_invitation_change(
        account=user,
        action=AccountChange.Action.RESENT,
        scheduler=scheduler,
        reason=reason,
        value=_invitation_value(invitation),
    )
    return invitation, token


@transaction.atomic
def revoke_scheduler_invitation(*, account, scheduler, reason=""):
    require_scheduling_administrator(scheduler)
    user = get_user_model().objects.select_for_update().get(pk=account.pk)
    _require_mutable_scheduler(user)
    invitation = SchedulerInvitation.objects.select_for_update().filter(account=user).first()
    if not invitation or invitation.status != SchedulerInvitation.Status.PENDING:
        raise ValidationError("This account has no pending invitation to revoke.")
    invitation.status = SchedulerInvitation.Status.REVOKED
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=("status", "revoked_at"))
    _record_invitation_change(
        account=user,
        action=AccountChange.Action.REVOKED,
        scheduler=scheduler,
        reason=reason,
        value=_invitation_value(invitation),
    )
    return invitation


def deliver_scheduler_invitation(*, invitation, token, request):
    """Deliver an already-persisted invitation, keeping token material transient."""
    setup_link = request.build_absolute_uri(reverse("scheduler-invitation-accept", args=(token,)))
    message = (
        "Muriel Physician Scheduling\n\n"
        "An account has been prepared for you. "
        f"Choose your password within {_invitation_lifetime_copy()}:\n{setup_link}\n\n"
        "If you did not expect this invitation, contact your Muriel administrator."
    )
    if settings.SCHEDULER_INVITATION_DELIVERY_MODE == "manual":
        _record_delivery(invitation, SchedulerInvitation.DeliveryStatus.MANUAL, "")
        return InvitationDelivery("manual", True, message, setup_link)
    try:
        sent = EmailMultiAlternatives(
            "Set up your Muriel account", message, to=[invitation.account.email]
        ).send()
    except Exception:
        _record_delivery(
            invitation,
            SchedulerInvitation.DeliveryStatus.FAILED,
            "Configured email delivery failed.",
        )
        return InvitationDelivery("smtp", False)
    if not sent:
        _record_delivery(
            invitation,
            SchedulerInvitation.DeliveryStatus.FAILED,
            "Configured email delivery failed.",
        )
        return InvitationDelivery("smtp", False)
    _record_delivery(invitation, SchedulerInvitation.DeliveryStatus.SENT, "")
    return InvitationDelivery("smtp", True)


@transaction.atomic
def accept_scheduler_invitation(*, token, password):
    invitation = _invitation_for_token(token, lock=True)
    user = get_user_model().objects.select_for_update().get(pk=invitation.account_id)
    if user.is_active or invitation.status != SchedulerInvitation.Status.PENDING:
        raise ValidationError("This invitation is no longer valid.")
    user.set_password(password)
    user.is_active = True
    user.save(update_fields=("password", "is_active"))
    if invitation.scheduling_administrator:
        scheduling_administrators, _ = Group.objects.get_or_create(
            name=SCHEDULING_ADMINISTRATORS_GROUP
        )
        user.groups.add(scheduling_administrators)
    invitation.status = SchedulerInvitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=("status", "accepted_at"))
    SchedulerInvitation.objects.filter(
        account=user, status=SchedulerInvitation.Status.PENDING
    ).exclude(pk=invitation.pk).update(status=SchedulerInvitation.Status.SUPERSEDED)
    _record_invitation_change(
        account=user,
        action=AccountChange.Action.ACCEPTED,
        scheduler=invitation.created_by,
        reason="",
        value={"scheduling_administrator": invitation.scheduling_administrator, "is_active": True},
    )
    return user


def invitation_for_token(token):
    """Return a valid invitation for recipient-page rendering without mutating it."""
    return _invitation_for_token(token, lock=False)


def production_recovery_account(username, *, lock=False):
    """Return the active superuser that authorizes a login-access clear."""
    user_model = get_user_model()
    accounts = user_model.objects
    if lock:
        accounts = accounts.select_for_update()
    try:
        account = accounts.get(**{user_model.USERNAME_FIELD: username})
    except user_model.DoesNotExist as error:
        raise ValidationError("Recovery account must name an existing active superuser.") from error
    if not account.is_active or not account.is_superuser:
        raise ValidationError("Recovery account must name an existing active superuser.")
    return account


@transaction.atomic
def clear_production_login_access(*, recovery_username):
    """Disable every account but the recovery superuser without deleting retained records."""
    recovery_account = production_recovery_account(recovery_username, lock=True)
    user_model = get_user_model()
    accounts = user_model.objects.select_for_update().exclude(pk=recovery_account.pk).order_by("pk")

    for account in accounts:
        physician = Physician.objects.select_for_update().filter(user=account).first()
        previous_value = _account_value(account, physician)
        if physician:
            unlink_physician_user(
                physician=physician,
                scheduler=recovery_account,
                reason=PRODUCTION_LOGIN_ACCESS_CLEAR_REASON,
            )
        for invitation in SchedulerInvitation.objects.select_for_update().filter(
            account=account, status=SchedulerInvitation.Status.PENDING
        ):
            invitation.status = SchedulerInvitation.Status.REVOKED
            invitation.revoked_at = timezone.now()
            invitation.save(update_fields=("status", "revoked_at"))
            _record_invitation_change(
                account=account,
                action=AccountChange.Action.REVOKED,
                scheduler=recovery_account,
                reason=PRODUCTION_LOGIN_ACCESS_CLEAR_REASON,
                value=_invitation_value(invitation),
            )
        if account.is_active:
            account.is_active = False
            account.save(update_fields=("is_active",))
            AccountChange.objects.create(
                account=account,
                action=AccountChange.Action.UPDATED,
                previous_value=previous_value,
                recorded_value=_account_value(account, None),
                reason=PRODUCTION_LOGIN_ACCESS_CLEAR_REASON,
                changed_by=recovery_account,
            )

    # Django's database session store has no account foreign key; remove all keys.
    Session.objects.all().delete()
    return recovery_account


def _new_invitation(*, account, scheduling_administrator, scheduler):
    secret = secrets.token_urlsafe(32)
    invitation = SchedulerInvitation.objects.create(
        account=account,
        created_by=scheduler,
        scheduling_administrator=scheduling_administrator,
        secret_digest=_secret_digest(secret),
        expires_at=timezone.now() + timedelta(seconds=settings.SCHEDULER_INVITATION_MAX_AGE),
    )
    token = signing.dumps(
        {"invitation": invitation.pk, "account": account.pk, "secret": secret},
        salt=INVITATION_SIGNING_SALT,
        compress=True,
    )
    return invitation, token


def _invitation_for_token(token, *, lock):
    try:
        payload = signing.loads(
            token, salt=INVITATION_SIGNING_SALT, max_age=settings.SCHEDULER_INVITATION_MAX_AGE
        )
        invitation_id = payload["invitation"]
        account_id = payload["account"]
        secret = payload["secret"]
    except signing.BadSignature, KeyError, TypeError:
        raise ValidationError("This invitation is no longer valid.") from None
    invitations = SchedulerInvitation.objects.select_related("account")
    if lock:
        invitations = invitations.select_for_update()
    try:
        invitation = invitations.get(pk=invitation_id, account_id=account_id)
    except SchedulerInvitation.DoesNotExist:
        raise ValidationError("This invitation is no longer valid.") from None
    if (
        invitation.status != SchedulerInvitation.Status.PENDING
        or invitation.expires_at <= timezone.now()
        or not secrets.compare_digest(invitation.secret_digest, _secret_digest(secret))
    ):
        raise ValidationError("This invitation is no longer valid.")
    return invitation


def _secret_digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _email_in_use(email, *, exclude=None):
    accounts = get_user_model().objects.select_for_update().filter(email__iexact=email)
    if exclude is not None:
        accounts = accounts.exclude(pk=exclude.pk)
    return accounts.exists()


def _invitation_lifetime_copy():
    seconds = settings.SCHEDULER_INVITATION_MAX_AGE
    for unit_seconds, unit in ((24 * 60 * 60, "day"), (60 * 60, "hour"), (60, "minute")):
        if seconds % unit_seconds == 0:
            count = seconds // unit_seconds
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _record_delivery(invitation, status, error):
    invitation.delivery_status = status
    invitation.delivery_error = error
    invitation.save(update_fields=("delivery_status", "delivery_error"))
    _record_invitation_change(
        account=invitation.account,
        action=(
            AccountChange.Action.DELIVERED
            if status != SchedulerInvitation.DeliveryStatus.FAILED
            else AccountChange.Action.DELIVERY_FAILED
        ),
        scheduler=invitation.created_by,
        reason="",
        value={"delivery_mode": settings.SCHEDULER_INVITATION_DELIVERY_MODE, "outcome": status},
    )


def _record_invitation_change(*, account, action, scheduler, reason, value):
    AccountChange.objects.create(
        account=account,
        action=action,
        previous_value=None,
        recorded_value=value,
        reason=normalize_note(reason),
        changed_by=scheduler,
    )


def _invitation_value(invitation):
    return {
        "invitation_status": invitation.status,
        "scheduling_administrator": invitation.scheduling_administrator,
        "expires_at": invitation.expires_at.isoformat(),
    }


def _require_mutable_scheduler(user):
    if user.is_staff or user.is_superuser:
        raise PermissionDenied("Django administrator accounts are managed in Django Admin.")


def _account_value(user, physician):
    """Retain only fields that Muriel is allowed to display and change."""
    return {
        "username": user.get_username(),
        "email": user.email,
        "is_active": user.is_active,
        "scheduling_administrator": user.groups.filter(
            name=SCHEDULING_ADMINISTRATORS_GROUP
        ).exists(),
        "physician": (
            {"id": physician.pk, "display_name": physician.display_name} if physician else None
        ),
    }
