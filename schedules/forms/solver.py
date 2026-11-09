from django import forms

from ..models.physicians import Physician
from .academic_events import AcademicYearChoiceField
from .preceptors import AssignmentPhysicianChoiceField


class PreceptingSolveForm(forms.Form):
    RANGE_MONTH = "month"
    RANGE_QUARTER = "quarter"
    RANGE_CUSTOM = "custom"
    RANGE_CHOICES = (
        (RANGE_MONTH, "Month"),
        (RANGE_QUARTER, "Quarter"),
        (RANGE_CUSTOM, "Custom"),
    )
    BALANCE_SPAN_CUSTOM = "custom"
    BALANCE_SPAN_CHOICES = (
        ("1-day", "1 day"),
        ("1-week", "1 week"),
        ("2-weeks", "2 weeks"),
        ("4-weeks", "4 weeks"),
        ("1-month", "1 month"),
        ("3-months", "3 months"),
        ("6-months", "6 months"),
        ("1-year", "1 year"),
        (BALANCE_SPAN_CUSTOM, "Custom"),
    )

    mode = forms.ChoiceField(choices=(("fill", "Fill open work"), ("repair", "Repair findings")))
    range_kind = forms.ChoiceField(choices=RANGE_CHOICES)
    month = forms.DateField(
        required=False,
        input_formats=("%Y-%m",),
        widget=forms.DateInput(
            format="%Y-%m",
            attrs={"type": "month", "class": "field-width--compact", "aria-label": "Month"},
        ),
    )
    academic_year = AcademicYearChoiceField(required=False)
    quarter = forms.ChoiceField(
        required=False,
        choices=((1, "Q1"), (2, "Q2"), (3, "Q3"), (4, "Q4")),
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    balance_span = forms.ChoiceField(
        choices=BALANCE_SPAN_CHOICES,
        initial="6-months",
        widget=forms.Select(attrs={"class": "field-width--compact"}),
    )
    balance_custom_days = forms.IntegerField(required=False, min_value=1)
    excluded_physician_ids = forms.ModelMultipleChoiceField(
        required=False,
        queryset=Physician.objects.none(),
        label="Do not assign these physicians in this proposal",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["excluded_physician_ids"].queryset = Physician.objects.order_by(
            "display_name", "pk"
        )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("range_kind")
        if kind == self.RANGE_MONTH and not cleaned.get("month"):
            self.add_error("month", "Choose a month.")
        elif kind == self.RANGE_QUARTER:
            if not cleaned.get("academic_year"):
                self.add_error("academic_year", "Choose an academic year.")
            if not cleaned.get("quarter"):
                self.add_error("quarter", "Choose a quarter.")
        elif kind == self.RANGE_CUSTOM:
            if not cleaned.get("start_date"):
                self.add_error("start_date", "Choose a start date.")
            if not cleaned.get("end_date"):
                self.add_error("end_date", "Choose an end date.")
        if cleaned.get("balance_span") == self.BALANCE_SPAN_CUSTOM and not cleaned.get(
            "balance_custom_days"
        ):
            self.add_error("balance_custom_days", "Enter a positive whole number of days.")
        return cleaned


class LateDocReviewForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    decision = forms.ChoiceField(
        choices=(("confirmed", "Confirm proposed physician"), ("replaced", "Choose a replacement"))
    )
    replacement_physician = forms.ModelChoiceField(
        required=False, queryset=Physician.objects.none(), label="Replacement Faculty physician"
    )

    def __init__(self, *args, candidates, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replacement_physician"].queryset = candidates

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == "replaced" and not cleaned.get("replacement_physician"):
            self.add_error("replacement_physician", "Choose a replacement physician.")
        return cleaned


class ProposalAssignmentForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, alternatives=None, candidates=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.alternatives = alternatives
        if alternatives is not None:
            self.fields["physician"] = AssignmentPhysicianChoiceField(
                alternatives,
                empty_label="Remove assignment",
                label="Physician alternative",
                required=False,
            )
        else:
            self.fields["physician"] = forms.ModelChoiceField(
                required=False,
                queryset=candidates or Physician.objects.none(),
                empty_label="Remove assignment",
            )
        self.order_fields(("revision", "physician", "note"))
