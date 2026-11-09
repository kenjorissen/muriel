from datetime import date
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from ..models.availability_categories import CustomAvailabilityCategory
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    float_schedule_input_mode,
    has_automatic_admin_option,
)
from ..services.operational_attention import organization_today
from .shared import optional_note_field, ordinal_label


def _schedule_input_context_date(form, retained_fact=None):
    """Use the date being edited, falling back to today's operational context."""
    raw_start = form.data.get("effective_start") if form.is_bound else None
    if raw_start:
        try:
            return date.fromisoformat(raw_start)
        except ValueError:
            pass
    if retained_fact and retained_fact.effective_start:
        return retained_fact.effective_start
    return form.initial.get("effective_start") or organization_today()


def _schedule_option_label(category, *, automatic_admin, include_impact=False):
    """Present the built-in Admin option according to its scheduling context."""
    if category.system_key == CustomAvailabilityCategory.SystemKey.ADMIN:
        return "Admin - Float" if automatic_admin else "Admin - Not available"
    if include_impact:
        return f"{category.name} - {category.get_impact_display()}"
    return category.name


class PhysicianForm(forms.Form):
    display_name = forms.CharField(max_length=200, label="Displayed name")
    create_login_and_invitation = forms.BooleanField(
        required=False,
        label="Create login and invitation",
    )
    official_email = forms.EmailField(required=False, label="Official email")
    role = forms.ChoiceField(
        choices=(("", "No initial role"), *EffectiveRole.Role.choices),
        required=False,
        label="Role",
    )
    effective_start = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Role start",
    )
    effective_end = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Role end",
    )
    faculty_equivalent = forms.BooleanField(required=False, label="Faculty-equivalent")
    faculty_leadership = forms.ChoiceField(
        choices=EffectiveRole.FacultyLeadership.choices,
        required=False,
        label="Faculty leadership",
    )
    reason = optional_note_field()

    def __init__(self, *args, include_initial_role=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not include_initial_role:
            for field_name in (
                "role",
                "effective_start",
                "effective_end",
                "faculty_equivalent",
                "faculty_leadership",
            ):
                self.fields.pop(field_name)

    def clean(self):
        cleaned_data = super().clean()
        official_email = cleaned_data.get("official_email")
        if cleaned_data.get("create_login_and_invitation"):
            if not official_email:
                submitted_email = self.data.get(self.add_prefix("official_email"), "").strip()
                if not submitted_email:
                    self.add_error(
                        "official_email", "An official email is required to create a login."
                    )
            if official_email:
                official_email = official_email.strip().casefold()
                user_model = get_user_model()
                if user_model.objects.filter(email__iexact=official_email).exists():
                    self.add_error("official_email", "An account already uses this email address.")
                elif user_model.objects.filter(username=official_email).exists():
                    self.add_error("official_email", "An account already uses this username.")
                cleaned_data["official_email"] = official_email
        else:
            cleaned_data["official_email"] = ""
        role = cleaned_data.get("role")
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if role and not start:
            self.add_error("effective_start", "An initial role needs an effective start date.")
        if not role:
            # The creation form pre-fills its optional start date for convenience.
            cleaned_data["effective_start"] = None
            cleaned_data["effective_end"] = None
        if role != EffectiveRole.Role.COMMUNITY_PRECEPTOR:
            cleaned_data["faculty_equivalent"] = False
        if role != EffectiveRole.Role.FACULTY:
            cleaned_data["faculty_leadership"] = ""
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data


class PhysicianAccountChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        state = "active" if user.is_active else "inactive"
        return f"{user.email} ({user.get_username()}, {state})"


class PhysicianAccountForm(forms.Form):
    user = PhysicianAccountChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        label="Linked account",
        help_text="Select an existing account or leave blank to remove the current link.",
    )
    reason = optional_note_field()

    def __init__(self, *args, physician, **kwargs):
        super().__init__(*args, **kwargs)
        users = list(get_user_model().objects.order_by("email", "username", "pk"))
        email_counts = {}
        for user in users:
            email = user.email.strip().casefold()
            if email:
                email_counts[email] = email_counts.get(email, 0) + 1
        eligible_ids = [
            user.pk
            for user in users
            if user.email.strip()
            and email_counts[user.email.strip().casefold()] == 1
            and (
                not hasattr(user, "physician_identity")
                or user.physician_identity.pk == physician.pk
            )
        ]
        if physician.user_id:
            eligible_ids.append(physician.user_id)
        self.fields["user"].queryset = (
            get_user_model().objects.filter(pk__in=eligible_ids).order_by("email", "username", "pk")
        )


class FacultyPreceptingCapacityOverrideForm(forms.Form):
    use_override = forms.BooleanField(
        required=False, label="Use custom Faculty Precepting capacity"
    )
    capacity_override = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_value=Decimal("10"),
        decimal_places=1,
        max_digits=3,
        label="Weekly capacity",
        help_text="Enter 0 through 10 weekly shifts. This current setting applies to a new solve's whole balance window.",
    )
    reason = optional_note_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_override = (
            self.fields["use_override"].widget.value_from_datadict(
                self.data, self.files, self.add_prefix("use_override")
            )
            if self.is_bound
            else self.initial.get("use_override", False)
        )
        if not use_override:
            self.fields["capacity_override"].widget.attrs["disabled"] = True

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("use_override") and cleaned_data.get("capacity_override") is None:
            self.add_error(
                "capacity_override", "Enter a weekly capacity when the override is enabled."
            )
        if not cleaned_data.get("use_override"):
            cleaned_data["capacity_override"] = None
        return cleaned_data


class EffectiveRoleForm(forms.Form):
    role = forms.ChoiceField(choices=EffectiveRole.Role.choices)
    effective_start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    effective_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    faculty_equivalent = forms.BooleanField(required=False, label="Faculty-equivalent")
    faculty_leadership = forms.ChoiceField(
        choices=EffectiveRole.FacultyLeadership.choices,
        required=False,
        label="Faculty leadership",
    )
    reason = optional_note_field()

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        if cleaned_data.get("role") != EffectiveRole.Role.COMMUNITY_PRECEPTOR:
            cleaned_data["faculty_equivalent"] = False
        if cleaned_data.get("role") != EffectiveRole.Role.FACULTY:
            cleaned_data["faculty_leadership"] = ""
        return cleaned_data


class EndEffectiveFactForm(forms.Form):
    effective_end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    reason = optional_note_field()


class FacultyAvailabilityTemplateForm(forms.Form):
    effective_start = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Blank means no lower date bound.",
    )
    effective_end = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Blank means no upper date bound.",
    )
    reason = optional_note_field()

    def __init__(self, *args, physician, template=None, transition_from=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.transition_from = transition_from
        if transition_from:
            self.fields["effective_start"].required = True
            self.fields["effective_start"].label = "Start date"
            self.fields[
                "effective_start"
            ].help_text = "The preceding template ends on the calendar day before this date."
            self.fields.pop("effective_end")
        self.schedule_input_context_date = _schedule_input_context_date(self, template)
        input_mode = float_schedule_input_mode(physician, on_date=self.schedule_input_context_date)
        community_float = input_mode == "community_float"
        admin_category = CustomAvailabilityCategory.objects.filter(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN, is_active=True
        ).first()
        category_filter = Q(
            is_active=True,
            assignments__physician=physician,
            assignments__is_active=True,
        )
        automatic_admin = has_automatic_admin_option(
            physician, on_date=self.schedule_input_context_date
        )
        if automatic_admin and admin_category:
            category_filter |= Q(pk=admin_category.pk)
        custom_categories = CustomAvailabilityCategory.objects.filter(category_filter).distinct()
        choices = [
            *(
                [("base:off", "Off")]
                if community_float
                else [
                    (f"base:{state}", label)
                    for state, label in FacultyAvailabilityTemplate.AvailabilityState.choices
                ]
            ),
            *(
                (
                    f"category:{category.pk}",
                    _schedule_option_label(category, automatic_admin=automatic_admin),
                )
                for category in custom_categories
            ),
        ]
        if community_float:
            choices = [
                choice
                for choice in choices
                if choice[0] == "base:off"
                or (admin_category and choice[0] == f"category:{admin_category.pk}")
            ]
        assigned_category_ids = {category.pk for category in custom_categories}
        for slot, label in FacultyAvailabilityTemplate.SLOT_CHOICES:
            slot_choices = list(choices)
            retained_selection = template.slots.get(slot) if template else None
            retained_category_id = FacultyAvailabilityTemplate.selection_category_id(
                retained_selection
            )
            if retained_category_id and retained_category_id not in assigned_category_ids:
                retained_category = CustomAvailabilityCategory.objects.filter(
                    pk=retained_category_id
                ).first()
                if retained_category:
                    slot_choices.append(
                        (f"category:{retained_category.pk}", retained_category.name)
                    )
            self.fields[slot] = forms.ChoiceField(
                choices=slot_choices,
                widget=forms.RadioSelect,
                initial=f"base:{
                    FacultyAvailabilityTemplate.AvailabilityState.OFF
                    if community_float and template is None
                    else FacultyAvailabilityTemplate.AvailabilityState.OPEN
                }",
                label=label,
            )
        self.order_fields(
            (
                "effective_start",
                *(("effective_end",) if not transition_from else ()),
                *(slot for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES),
                "reason",
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if (
            self.transition_from
            and start
            and self.transition_from.effective_start
            and start <= self.transition_from.effective_start
        ):
            self.add_error(
                "effective_start", "Choose a date later than the preceding template's start."
            )
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data

    def slot_values(self):
        values = {}
        for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES:
            value = self.cleaned_data[slot]
            if value.startswith("base:"):
                values[slot] = FacultyAvailabilityTemplate.base_selection(
                    value.removeprefix("base:")
                )
            else:
                values[slot] = {"category_id": int(value.removeprefix("category:"))}
        return values


class MonthlyPhysicianAvailabilityRuleForm(forms.Form):
    weekday = forms.ChoiceField(
        choices=MonthlyPhysicianAvailabilityRule.WEEKDAY_CHOICES,
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    ordinals = forms.MultipleChoiceField(
        choices=(
            ("all", "All occurrences"),
            *((str(value), ordinal_label(value)) for value in range(1, 6)),
        ),
        widget=forms.CheckboxSelectMultiple,
        label="Occurrences",
    )
    periods = forms.MultipleChoiceField(
        choices=MonthlyPhysicianAvailabilityRule.PeriodOfDay.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=list(MonthlyPhysicianAvailabilityRule.PeriodOfDay.values),
        label="Periods",
    )
    annual_window = forms.ChoiceField(
        choices=(("all_year", "All year"), ("window", "Selected months")),
        widget=forms.RadioSelect,
        label="Annual window",
    )
    annual_start_month = forms.TypedChoiceField(
        choices=(("", "Start month"), *MonthlyPhysicianAvailabilityRule.MONTH_CHOICES),
        coerce=int,
        empty_value=None,
        required=False,
        label="Start month",
    )
    annual_end_month = forms.TypedChoiceField(
        choices=(("", "End month"), *MonthlyPhysicianAvailabilityRule.MONTH_CHOICES),
        coerce=int,
        empty_value=None,
        required=False,
        label="End month",
    )
    effective_start = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
        help_text="Blank means no lower date bound.",
    )
    effective_end = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
        help_text="Blank means no upper date bound.",
    )
    reason = optional_note_field()

    def __init__(self, *args, physician, rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.schedule_input_context_date = _schedule_input_context_date(self, rule)
        input_mode = float_schedule_input_mode(physician, on_date=self.schedule_input_context_date)
        community_float = input_mode == "community_float"
        retained_selection = rule.availability_selection if rule else None
        retained_category_id = FacultyAvailabilityTemplate.selection_category_id(retained_selection)
        category_filter = Q(
            is_active=True,
            assignments__physician=physician,
            assignments__is_active=True,
        )
        if retained_category_id:
            category_filter |= Q(pk=retained_category_id)
        admin_category = CustomAvailabilityCategory.objects.filter(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN, is_active=True
        ).first()
        automatic_admin = has_automatic_admin_option(
            physician, on_date=self.schedule_input_context_date
        )
        if automatic_admin and admin_category:
            category_filter |= Q(pk=admin_category.pk)
        categories = CustomAvailabilityCategory.objects.filter(category_filter).distinct()
        if community_float:
            categories = (
                categories.filter(pk=admin_category.pk) if admin_category else categories.none()
            )
        category_choices = [
            (
                f"category:{category.pk}",
                _schedule_option_label(
                    category, automatic_admin=automatic_admin, include_impact=True
                ),
            )
            for category in categories
        ]
        self.fields["availability"] = forms.ChoiceField(
            choices=(
                (
                    "Fixed states",
                    (("base:off", "Off"),)
                    if community_float
                    else tuple(
                        (f"base:{state}", label)
                        for state, label in FacultyAvailabilityTemplate.AvailabilityState.choices
                    ),
                ),
                ("Schedule options", tuple(category_choices)),
            ),
            label="Schedule option",
        )
        self.order_fields(
            (
                "weekday",
                "ordinals",
                "periods",
                "annual_window",
                "annual_start_month",
                "annual_end_month",
                "availability",
                "effective_start",
                "effective_end",
                "reason",
            )
        )
        if not self.is_bound:
            self.initial.setdefault(
                "annual_window",
                "window"
                if self.initial.get("annual_start_month") is not None
                or self.initial.get("annual_end_month") is not None
                else "all_year",
            )

    def clean_weekday(self):
        return int(self.cleaned_data["weekday"])

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        if cleaned_data.get("annual_window") == "all_year":
            cleaned_data["annual_start_month"] = None
            cleaned_data["annual_end_month"] = None
        elif cleaned_data.get("annual_window") == "window" and (
            cleaned_data.get("annual_start_month") is None
            or cleaned_data.get("annual_end_month") is None
        ):
            self.add_error(
                "annual_start_month", "Choose both annual window months or leave both blank."
            )
            self.add_error(
                "annual_end_month", "Choose both annual window months or leave both blank."
            )
        return cleaned_data

    def availability_selection(self):
        value = self.cleaned_data["availability"]
        if value.startswith("base:"):
            return FacultyAvailabilityTemplate.base_selection(value.removeprefix("base:"))
        return {"category_id": int(value.removeprefix("category:"))}


class MonthlyPhysicianAvailabilityRuleDeactivateForm(forms.Form):
    reason = optional_note_field()


class CustomAvailabilityCategoryForm(forms.Form):
    name = forms.CharField(max_length=120, label="Schedule option name")
    impact = forms.ChoiceField(
        choices=CustomAvailabilityCategory.Impact.choices, label="Scheduling impact"
    )
    reason = optional_note_field()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Schedule option name cannot be blank.")
        return name


class CustomAvailabilityCategoryAssignmentForm(forms.Form):
    physicians = forms.ModelMultipleChoiceField(
        queryset=Physician.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Faculty physicians",
        help_text="Only selected physicians may use this schedule option in future schedule input.",
    )
    reason = optional_note_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["physicians"].queryset = Physician.objects.filter(
            roles__role=EffectiveRole.Role.FACULTY
        ).distinct()


class CustomAvailabilityCategoryDeactivateForm(forms.Form):
    reason = optional_note_field()
