"""Template context for the shared Accounts authority boundary."""

from .services._mutation import can_manage_scheduling_administration


def context(request):
    return {"can_manage_accounts": can_manage_scheduling_administration(request.user)}
