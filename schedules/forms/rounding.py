from datetime import date

from django import forms

from ..models.physicians import Physician
from ..models.rounding import CasualRounderRule
from ..services.rounding import (
    normal_rounder_covers_week,
    normal_rounder_eligibility_message,
    normal_rounders_for_week,
)
from .shared import optional_note_field, ordinal_label


class RoundingWeekRangeForm(forms.Form):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "data-period-picker-input": ""})
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "data-period-picker-input": ""})
    )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "End date cannot be before start date.")
        return cleaned_data


class NormalRounderForm(forms.Form):
    week = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
        label="Monday-starting week",
    )
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.all(),
        widget=forms.Select(attrs={"class": "field-width--moderate"}),
    )
    reason = optional_note_field()

    def __init__(self, *args, fixed_week=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_week = fixed_week
        if fixed_week:
            self.fields.pop("week")
        self.fields["physician"].queryset = Physician.objects.none()
        week = fixed_week or self._week_from_input()
        if week and week.weekday() == 0:
            self.fields["physician"].queryset = normal_rounders_for_week(week)

    def _week_from_input(self):
        value = self.data.get("week") if self.is_bound else self.initial.get("week")
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except TypeError, ValueError:
            return None

    def clean_week(self):
        week = self.cleaned_data["week"]
        if week.weekday() != 0:
            raise forms.ValidationError("Rounding weeks must start on a Monday.")
        return week

    def clean(self):
        cleaned_data = super().clean()
        week = self.fixed_week or cleaned_data.get("week")
        if self.fixed_week:
            cleaned_data["week"] = self.fixed_week
        if week and week.weekday() == 0:
            physician = cleaned_data.get("physician")
            if physician and not normal_rounder_covers_week(physician, week):
                self.add_error("physician", normal_rounder_eligibility_message(physician, week))
            elif not physician and self.data.get("physician"):
                try:
                    physician = Physician.objects.get(pk=self.data.get("physician"))
                except Physician.DoesNotExist, ValueError, TypeError:
                    physician = None
                if physician:
                    message = normal_rounder_eligibility_message(physician, week)
                    if message:
                        self.add_error(None, message)
        return cleaned_data


class CasualRounderRuleForm(forms.Form):
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.all(),
        widget=forms.Select(attrs={"class": "field-width--moderate"}),
    )
    weekday = forms.ChoiceField(
        choices=CasualRounderRule.WEEKDAY_CHOICES,
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
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data


class RoundingRemovalForm(forms.Form):
    reason = optional_note_field()
