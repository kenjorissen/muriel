from django import forms

from .shared import optional_note_field


class LeadTimeForm(forms.Form):
    months = forms.IntegerField(
        min_value=0,
        label="Lead time (months)",
        help_text="Whole months before the fixed policy anchor.",
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    note = optional_note_field()
