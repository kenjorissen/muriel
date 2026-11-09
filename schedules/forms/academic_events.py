from django import forms
from django.forms import formset_factory

from ..academic_years import (
    academic_year_for_date,
    academic_year_label,
    default_duration_fits_expected_quarter,
    expected_quarter_date_range,
)
from ..models.academic_events import (
    AcademicEvent,
    AcademicEventDefinition,
    AcademicEventMonthlyRule,
    AcademicEventQuarter,
)
from ..models.physicians import Physician
from ..services.operational_attention import organization_today
from .shared import optional_note_field, ordinal_label

MAX_ACADEMIC_EVENT_DEFINITION_UPLOAD_BYTES = 10 * 1024 * 1024


class AcademicEventRangeForm(forms.Form):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"})
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"})
    )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "End date cannot be before start date.")
        return cleaned_data


class AcademicEventAdditionalRangeForm(forms.Form):
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")
        if bool(start) != bool(end):
            self.add_error(None, "Both range dates are required.")
        if start and end and end < start:
            self.add_error("end_date", "End date cannot be before start date.")
        return cleaned_data


AcademicEventAdditionalRangeFormSet = formset_factory(
    AcademicEventAdditionalRangeForm, extra=0, can_delete=True
)


class AcademicYearChoiceField(forms.TypedChoiceField):
    def __init__(self, *args, years=range(2000, 2101), allow_unbounded=False, **kwargs):
        choices = [(str(year), academic_year_label(year)) for year in sorted(set(years))]
        if allow_unbounded:
            choices.insert(0, ("", "Unbounded"))
        kwargs["choices"] = choices
        kwargs["coerce"] = int
        kwargs["empty_value"] = None
        kwargs.setdefault("widget", forms.Select(attrs={"class": "field-width--compact"}))
        super().__init__(*args, **kwargs)


class AcademicEventForm(AcademicEventRangeForm):
    definition = forms.ModelChoiceField(
        queryset=AcademicEventDefinition.objects.filter(is_active=True),
        label="Event definition",
        widget=forms.Select(attrs={"class": "field-width--moderate"}),
    )
    academic_year = AcademicYearChoiceField(label="Academic year")
    suffix = forms.CharField(
        required=False,
        max_length=120,
        label="Event detail (optional)",
        help_text="Appended to the definition name on schedules.",
        widget=forms.TextInput(attrs={"class": "field-width--moderate"}),
    )
    note = forms.CharField(
        required=False,
        label="Event note (optional)",
        widget=forms.Textarea(attrs={"class": "field-width--full", "rows": 2}),
    )
    required_participant_count = forms.IntegerField(
        required=False,
        min_value=1,
        label="Required participants",
        widget=forms.NumberInput(attrs={"class": "field-width--compact"}),
    )
    period_of_day = forms.ChoiceField(
        choices=AcademicEvent.PeriodOfDay.choices,
        required=False,
        initial=AcademicEvent.PeriodOfDay.WHOLE_DAY,
        label="Affected Coverage",
        widget=forms.RadioSelect,
    )
    precepting_impact = forms.ChoiceField(
        choices=AcademicEvent.PreceptingImpact.choices,
        required=False,
        initial=AcademicEvent.PreceptingImpact.HARD,
        label="Precepting impact",
        help_text=(
            "Hard makes physicians unavailable; Soft keeps them eligible and records a "
            "finding; None has no scheduling effect."
        ),
        widget=forms.RadioSelect,
    )
    participants = forms.ModelMultipleChoiceField(
        queryset=Physician.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Physicians",
    )
    all_available_faculty = forms.BooleanField(
        required=False,
        label="All available Faculty",
        help_text="Check this or select individual physicians.",
    )
    reason = optional_note_field()

    def clean_period_of_day(self):
        return self.cleaned_data.get("period_of_day") or AcademicEvent.PeriodOfDay.WHOLE_DAY

    def clean_precepting_impact(self):
        return self.cleaned_data.get("precepting_impact") or AcademicEvent.PreceptingImpact.HARD

    def clean(self):
        cleaned_data = super().clean()
        _validate_impact_coverage(self, "precepting_impact", "period_of_day")
        return cleaned_data

    def __init__(
        self,
        *args,
        allow_resolved_definition=False,
        allow_monthly_definition=False,
        apply_create_defaults=False,
        current_academic_year=None,
        existing_academic_year=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["participants"].widget.attrs["class"] = "field-width--wide"
        self.fields["reason"].widget.attrs["class"] = "field-width--full"
        self.fields["reason"].widget.attrs["rows"] = 2
        current_academic_year = current_academic_year or academic_year_for_date(
            organization_today()
        )
        academic_years = set(range(current_academic_year, current_academic_year + 11))
        if existing_academic_year is not None:
            academic_years.add(existing_academic_year)
        self.fields["academic_year"] = AcademicYearChoiceField(
            years=academic_years,
            initial=current_academic_year,
            label="Academic year",
        )
        academic_year = self._academic_year_from_input()
        if academic_year is not None:
            definitions = AcademicEventDefinition.objects.effective_for(academic_year)
            if not allow_monthly_definition:
                definitions = definitions.filter(
                    scheduling_mode=AcademicEventDefinition.SchedulingMode.YEARLY
                )
            if not allow_resolved_definition:
                definitions = definitions.exclude(
                    annual_events__academic_year=academic_year, annual_events__is_active=True
                )
            self.fields["definition"].queryset = definitions
        else:
            self.fields["definition"].queryset = AcademicEventDefinition.objects.none()
        definitions = tuple(self.fields["definition"].queryset)
        duplicate_names = {
            definition.base_name.casefold()
            for definition in definitions
            if sum(
                other.base_name.casefold() == definition.base_name.casefold()
                for other in definitions
            )
            > 1
        }
        self.fields["definition"].label_from_instance = lambda definition: (
            f"{definition.base_name} ({definition.expected_quarter})"
            if definition.base_name.casefold() in duplicate_names
            else definition.base_name
        )

        if self.is_bound and apply_create_defaults:
            self._apply_create_defaults()
        if self.is_bound:
            return
        definition = self._initial_definition()
        if (
            definition
            and definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
            and academic_year is not None
            and definition.applies_to(academic_year)
        ):
            start_date, end_date = expected_quarter_date_range(
                academic_year, definition.expected_quarter, definition.default_duration_days
            )
            self.initial.setdefault("start_date", start_date)
            self.initial.setdefault("end_date", end_date)
            self.initial.setdefault("period_of_day", definition.default_period_of_day)
            self.initial.setdefault("precepting_impact", definition.default_precepting_impact)
            self.initial.setdefault(
                "required_participant_count", definition.default_required_participant_count
            )

    def _academic_year_from_input(self):
        value = (
            self.data.get("academic_year") if self.is_bound else self.initial.get("academic_year")
        )
        try:
            return int(value)
        except TypeError, ValueError:
            return None

    def _initial_definition(self):
        value = self.initial.get("definition")
        if isinstance(value, AcademicEventDefinition):
            return value
        try:
            return AcademicEventDefinition.objects.get(pk=value)
        except AcademicEventDefinition.DoesNotExist, TypeError, ValueError:
            return None

    def _apply_create_defaults(self):
        academic_year = self._academic_year_from_input()
        if academic_year is None:
            return
        definition = (
            self.fields["definition"].queryset.filter(pk=self.data.get("definition")).first()
        )
        if (
            definition is None
            or definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.YEARLY
        ):
            return

        data = self.data.copy()
        start_date, end_date = expected_quarter_date_range(
            academic_year, definition.expected_quarter, definition.default_duration_days
        )
        defaults = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "period_of_day": definition.default_period_of_day,
            "precepting_impact": definition.default_precepting_impact,
        }
        for field_name, value in defaults.items():
            if not data.get(field_name):
                data[field_name] = value
        self.data = data


class AcademicEventRemovalForm(forms.Form):
    reason = optional_note_field()


class AcademicEventAssignmentForm(forms.Form):
    participants = forms.ModelMultipleChoiceField(
        queryset=Physician.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Physicians",
    )
    all_available_faculty = forms.BooleanField(
        required=False,
        label="All available Faculty",
        help_text="Check this or select individual physicians.",
    )
    reason = optional_note_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["participants"].widget.attrs["class"] = "field-width--wide"
        self.fields["reason"].widget.attrs["class"] = "field-width--full"
        self.fields["reason"].widget.attrs["rows"] = 2

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("all_available_faculty") and cleaned_data.get("participants"):
            self.add_error(
                "all_available_faculty",
                "All available Faculty cannot be combined with individual physicians.",
            )
        return cleaned_data


class AcademicEventDefinitionForm(forms.Form):
    base_name = forms.CharField(
        max_length=120,
        label="Name",
        widget=forms.TextInput(attrs={"class": "field-width--moderate"}),
    )
    scheduling_mode = forms.ChoiceField(
        choices=AcademicEventDefinition.SchedulingMode.choices,
        required=False,
        initial=AcademicEventDefinition.SchedulingMode.YEARLY,
        label="Scheduling mode",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    expected_quarter = forms.ChoiceField(
        choices=AcademicEventQuarter.choices,
        required=False,
        initial=AcademicEventQuarter.Q1,
        label="Expected academic-year quarter",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    typically_scheduled = forms.CharField(
        required=False,
        label="Typically Scheduled",
        help_text="Optional source description used to order definitions within a quarter.",
        widget=forms.Textarea(attrs={"class": "field-width--full", "rows": 2}),
    )
    default_duration_days = forms.IntegerField(
        min_value=1,
        required=False,
        initial=1,
        label="Default duration (days)",
        widget=forms.NumberInput(attrs={"class": "field-width--compact"}),
    )
    default_period_of_day = forms.ChoiceField(
        choices=AcademicEvent.PeriodOfDay.choices,
        initial=AcademicEvent.PeriodOfDay.WHOLE_DAY,
        label="Affected coverage",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    default_precepting_impact = forms.ChoiceField(
        choices=AcademicEvent.PreceptingImpact.choices,
        required=False,
        initial=AcademicEvent.PreceptingImpact.HARD,
        label="Default Precepting impact",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    default_required_participant_count = forms.IntegerField(
        min_value=1,
        required=False,
        initial=1,
        label="Default required participants",
        widget=forms.NumberInput(attrs={"class": "field-width--compact"}),
    )
    all_available_faculty = forms.BooleanField(
        required=False,
        label="All available Faculty",
        help_text="Check this or select individual default physicians.",
    )
    default_participants = forms.ModelMultipleChoiceField(
        queryset=Physician.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Default physicians",
    )
    start_academic_year = AcademicYearChoiceField(label="Start academic year")
    end_academic_year = AcademicYearChoiceField(
        required=False, allow_unbounded=True, label="End academic year"
    )

    def __init__(
        self,
        *args,
        current_academic_year=None,
        existing_start_academic_year=None,
        existing_end_academic_year=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if (
            self.is_bound
            and self.data.get("scheduling_mode")
            == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
        ):
            # Disabled yearly-only controls must not affect a Monthly-rules submission.
            data = self.data.copy()
            data.pop("expected_quarter", None)
            data.pop("default_duration_days", None)
            self.data = data
        current_academic_year = current_academic_year or academic_year_for_date(
            organization_today()
        )
        start_years = set(range(current_academic_year - 5, current_academic_year + 11))
        end_years = set(range(current_academic_year, current_academic_year + 11))
        if existing_start_academic_year is not None:
            start_years.add(existing_start_academic_year)
        if existing_end_academic_year is not None:
            end_years.add(existing_end_academic_year)
        self.fields["start_academic_year"] = AcademicYearChoiceField(
            years=start_years,
            initial=current_academic_year,
            label="Start academic year",
        )
        self.fields["end_academic_year"] = AcademicYearChoiceField(
            years=end_years,
            allow_unbounded=True,
            required=False,
            initial=None,
            label="End academic year",
        )
        self.fields["default_participants"].widget.attrs["class"] = "field-width--wide"
        self.order_fields(
            (
                "base_name",
                "scheduling_mode",
                "expected_quarter",
                "typically_scheduled",
                "default_duration_days",
                "default_period_of_day",
                "default_precepting_impact",
                "default_required_participant_count",
                "all_available_faculty",
                "default_participants",
                "start_academic_year",
                "end_academic_year",
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data["default_required_participant_count"] = (
            cleaned_data.get("default_required_participant_count") or 1
        )
        expected_quarter = cleaned_data.get("expected_quarter")
        scheduling_mode = (
            cleaned_data.get("scheduling_mode") or AcademicEventDefinition.SchedulingMode.YEARLY
        )
        cleaned_data["scheduling_mode"] = scheduling_mode
        if (
            scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
            and not expected_quarter
        ):
            self.add_error("expected_quarter", "Yearly definitions need an expected quarter.")
        if (
            scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
            and not cleaned_data.get("default_duration_days")
        ):
            self.add_error("default_duration_days", "Default duration is required.")
        if scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES:
            cleaned_data["expected_quarter"] = None
        if cleaned_data.get("all_available_faculty") and cleaned_data.get("default_participants"):
            self.add_error(
                "all_available_faculty",
                "All available Faculty cannot be combined with individual default physicians.",
            )
        duration_days = cleaned_data.get("default_duration_days")
        if (
            scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
            and expected_quarter
            and duration_days
            and not default_duration_fits_expected_quarter(expected_quarter, duration_days)
        ):
            self.add_error(
                "default_duration_days",
                "Default duration must fit within an academic year when it starts on the first "
                "day of the expected quarter.",
            )
        start = cleaned_data.get("start_academic_year")
        end = cleaned_data.get("end_academic_year")
        if start and end and end < start:
            self.add_error(
                "end_academic_year",
                f"End academic year {academic_year_label(end)} cannot be before start academic year "
                f"{academic_year_label(start)}.",
            )
        _validate_impact_coverage(self, "default_precepting_impact", "default_period_of_day")
        return cleaned_data

    def clean_default_precepting_impact(self):
        return (
            self.cleaned_data.get("default_precepting_impact")
            or AcademicEvent.PreceptingImpact.HARD
        )


class AcademicEventMonthlyRuleForm(forms.Form):
    weekday = forms.ChoiceField(
        choices=AcademicEventMonthlyRule.WEEKDAY_CHOICES,
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
    effective_start = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
    )
    effective_end = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
    )
    reason = optional_note_field()

    def clean_weekday(self):
        return int(self.cleaned_data["weekday"])

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("effective_start")
            and cleaned_data.get("effective_end")
            and cleaned_data["effective_end"] < cleaned_data["effective_start"]
        ):
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data


class AcademicEventMonthlyRuleRemovalForm(forms.Form):
    reason = optional_note_field()


def _validate_impact_coverage(form, impact_field, coverage_field):
    impact = form.cleaned_data.get(impact_field)
    coverage = form.cleaned_data.get(coverage_field)
    if (
        impact in (AcademicEvent.PreceptingImpact.HARD, AcademicEvent.PreceptingImpact.SOFT)
        and coverage == AcademicEvent.PeriodOfDay.NONE
    ):
        form.add_error(
            coverage_field, "Hard or Soft Precepting impact requires AM, PM, or Whole day coverage."
        )
    if coverage == AcademicEvent.PeriodOfDay.NONE and impact not in (
        None,
        AcademicEvent.PreceptingImpact.NONE,
    ):
        form.add_error(impact_field, "None coverage requires None Precepting impact.")


class AcademicYearSelectionForm(forms.Form):
    academic_year = AcademicYearChoiceField(label="Academic year")

    def __init__(self, *args, years=None, **kwargs):
        super().__init__(*args, **kwargs)
        if years is not None:
            self.fields["academic_year"] = AcademicYearChoiceField(
                years=years, label="Academic year"
            )


class AcademicEventDefinitionImportForm(forms.Form):
    workbook = forms.FileField(
        label="Academic Year Calendar workbook",
        help_text="Choose the clinic Academic Year Calendar .xlsx workbook.",
        widget=forms.FileInput(
            attrs={
                "accept": ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            }
        ),
    )

    def clean_workbook(self):
        workbook = self.cleaned_data["workbook"]
        if not workbook.name.casefold().endswith(".xlsx"):
            raise forms.ValidationError("Choose an Excel .xlsx workbook.")
        if workbook.size > MAX_ACADEMIC_EVENT_DEFINITION_UPLOAD_BYTES:
            raise forms.ValidationError("The workbook must be 10 MB or smaller.")
        return workbook


class AcademicEventImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)
