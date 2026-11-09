from django import forms

from ..models.community_preceptors import CommunityPreceptorRule
from ..models.physicians import Physician
from .shared import optional_note_field, ordinal_label


class CommunityPreceptorRuleForm(forms.Form):
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.all(),
        widget=forms.Select(attrs={"class": "field-width--moderate"}),
    )
    weekday = forms.ChoiceField(
        choices=CommunityPreceptorRule.WEEKDAY_CHOICES,
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
        choices=CommunityPreceptorRule.PeriodOfDay.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=list(CommunityPreceptorRule.PeriodOfDay.values),
        label="Periods",
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

    def clean_weekday(self):
        return int(self.cleaned_data["weekday"])

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data


class CommunityPreceptorRemovalForm(forms.Form):
    reason = optional_note_field()
