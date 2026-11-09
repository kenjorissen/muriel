from django import forms

from ..models.physicians import Physician
from ..models.vacations import Vacation

MAX_VACATION_CALENDAR_UPLOAD_BYTES = 10 * 1024 * 1024


class VacationRangeForm(forms.Form):
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


class VacationForm(VacationRangeForm):
    physician = forms.ModelChoiceField(
        queryset=Physician.objects.all(),
        widget=forms.Select(attrs={"class": "field-width--moderate"}),
    )
    period_of_day = forms.ChoiceField(
        choices=Vacation.PeriodOfDay.choices,
        initial=Vacation.PeriodOfDay.WHOLE_DAY,
        label="Coverage",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    note = forms.CharField(
        required=False,
        label="Optional note",
        widget=forms.Textarea(attrs={"class": "field-width--wide"}),
        help_text="Optional context for this vacation.",
    )


class VacationCalendarImportForm(forms.Form):
    workbook = forms.FileField(
        label="Vacation calendar workbook",
        help_text="Choose the July-through-June .xlsx workbook. Residents are ignored.",
        widget=forms.FileInput(
            attrs={
                "accept": ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            }
        ),
    )
    replace = forms.BooleanField(
        required=False,
        label="Replace workbook-scoped vacations",
        help_text=(
            "Synchronize matched Faculty vacations inside the workbook's 12-month window. "
            "Exact matches remain; stale ranges are removed only when Apply is selected."
        ),
    )

    def clean_workbook(self):
        workbook = self.cleaned_data["workbook"]
        if not workbook.name.casefold().endswith(".xlsx"):
            raise forms.ValidationError("Choose an Excel .xlsx workbook.")
        if workbook.size > MAX_VACATION_CALENDAR_UPLOAD_BYTES:
            raise forms.ValidationError("The workbook must be 10 MB or smaller.")
        return workbook
