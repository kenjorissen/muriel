from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    """Authenticate one active account by its username or configured email."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if username is None or password is None:
            return None

        UserModel = get_user_model()
        matches = UserModel._default_manager.filter(
            Q(username=username) | Q(email__iexact=username), is_active=True
        )
        if matches.count() != 1:
            # Match Django's default backend's password work for invalid credentials.
            UserModel().set_password(password)
            return None

        user = matches.get()
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
