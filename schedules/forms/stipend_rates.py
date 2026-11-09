from django import forms

from .shared import optional_note_field


class StipendRateValueForm(forms.Form):
    hourly_rate = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=0.01,
        label="Hourly rate (USD)",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    note = optional_note_field()


class StipendRateTransitionForm(StipendRateValueForm):
    transition_date = forms.DateField(
        label="Transition date",
        help_text="The new rate begins on this date.",
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class StipendRateCorrectionForm(StipendRateValueForm):
    effective_start = forms.DateField(
        required=False,
        label="Effective start",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    effective_end = forms.DateField(
        required=False,
        label="Effective end",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, rate, **kwargs):
        super().__init__(*args, **kwargs)
        if rate.effective_start is None:
            self.fields["effective_start"].disabled = True
            self.fields[
                "effective_start"
            ].help_text = "The first rate remains unbounded into the past."
        if rate.effective_end is None:
            self.fields["effective_end"].disabled = True
            self.fields[
                "effective_end"
            ].help_text = "The last rate remains unbounded into the future."

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("effective_start")
        end = cleaned_data.get("effective_end")
        if start and end and end < start:
            self.add_error("effective_end", "End date cannot be before start date.")
        return cleaned_data


class StipendRateRemovalForm(forms.Form):
    note = optional_note_field()

    def __init__(self, *args, previous, following, **kwargs):
        super().__init__(*args, **kwargs)
        if previous and following:
            self.fields["absorber"] = forms.ChoiceField(
                choices=(
                    (previous.pk, f"Previous rate (${previous.hourly_rate})"),
                    (following.pk, f"Next rate (${following.hourly_rate})"),
                ),
                widget=forms.RadioSelect,
                label="Rate that will absorb this period",
            )
