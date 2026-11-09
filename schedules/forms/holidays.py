from django import forms

from .shared import optional_note_field


class HolidayOccurrenceForm(forms.Form):
    observed_date = forms.DateField(
        label="Observed date",
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
    )
    note = optional_note_field()


class CalendarYearSelectionForm(forms.Form):
    calendar_year = forms.TypedChoiceField(
        coerce=int,
        label="Calendar year",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )

    def __init__(self, *args, years, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["calendar_year"].choices = [
            (str(year), str(year)) for year in sorted(set(years))
        ]
