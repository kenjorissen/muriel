"""Authentication views that record non-sensitive security events."""

import logging

from django.contrib.auth.views import LoginView, PasswordResetView

logger = logging.getLogger("muriel")


class MurielLoginView(LoginView):
    def form_invalid(self, form):
        logger.warning("sign_in_failed request_id=%s", self.request.request_id)
        return super().form_invalid(form)

    def form_valid(self, form):
        logger.info("sign_in_succeeded request_id=%s", self.request.request_id)
        return super().form_valid(form)


class MurielPasswordResetView(PasswordResetView):
    def form_valid(self, form):
        # Do not log the submitted address: reset requests must remain non-enumerating.
        logger.info("password_reset_requested request_id=%s", self.request.request_id)
        return super().form_valid(form)
