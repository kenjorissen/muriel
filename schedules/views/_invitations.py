from django.contrib import messages
from django.shortcuts import redirect, render

from ..services.accounts import deliver_scheduler_invitation


def deliver_invitation_response(request, invitation, token, title):
    delivery = deliver_scheduler_invitation(invitation=invitation, token=token, request=request)
    if delivery.mode == "manual":
        return render(
            request,
            "schedules/account_invitation_delivery.html",
            {"delivery": delivery, "title": title},
        )
    if delivery.delivered:
        messages.success(request, f"{title} sent through SMTP.")
    else:
        messages.error(
            request, f"{title} was saved, but SMTP delivery failed. Resend it after fixing mail."
        )
    return redirect("account-workspace")
