from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    UserChangeForm,
    UserCreationForm,
    UsernameField,
)
from django.db.models import Q

from ..models.physicians import Physician
from .shared import optional_note_field


class UniqueEmailValidationMixin:
    """Keep supported Django Admin account forms from accepting ambiguous emails."""

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if (
            email
            and get_user_model()
            .objects.filter(email__iexact=email)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("An account already uses this email address.")
        return email


class AccountChangeForm(UniqueEmailValidationMixin, UserChangeForm):
    pass


class AccountCreationForm(UniqueEmailValidationMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        fields = ("username", "email")


class UsernameOrEmailAuthenticationForm(AuthenticationForm):
    username = UsernameField(label="Username or email")


class SchedulerAccountForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField(label="Official email")
    is_active = forms.BooleanField(required=False, label="Account is active")
    scheduling_administrator = forms.BooleanField(required=False, label="Scheduling Administrator")
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.none(), required=False, label="Linked physician"
    )
    reason = optional_note_field()

    def __init__(self, *args, account, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        self.fields["physician"].queryset = Physician.objects.filter(
            Q(user__isnull=True) | Q(user=account)
        ).order_by("display_name", "pk")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if (
            get_user_model()
            .objects.filter(email__iexact=email)
            .exclude(pk=self.account.pk)
            .exists()
        ):
            raise forms.ValidationError("An account already uses this email address.")
        return email


class SchedulerInvitationForm(UniqueEmailValidationMixin, forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField(label="Official email")
    scheduling_administrator = forms.BooleanField(required=False, label="Scheduling Administrator")
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.none(), required=False, label="Linked physician"
    )
    reason = optional_note_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["physician"].queryset = Physician.objects.filter(user__isnull=True).order_by(
            "display_name", "pk"
        )

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already uses this email address.")
        return email
