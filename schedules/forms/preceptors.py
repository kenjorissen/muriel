from django import forms

from ..services.preceptors import assignment_alternatives_for_session
from .shared import optional_note_field


class PreceptingWorkbookImportForm(forms.Form):
    workbook = forms.FileField(
        label="Precepting workbook",
        help_text="Choose the combined Precepting and Interview .xlsx workbook.",
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
        if workbook.size > 10 * 1024 * 1024:
            raise forms.ValidationError("The workbook must be 10 MB or smaller.")
        return workbook


class PreceptingWorkbookImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)


class AssignmentPhysicianChoiceField(forms.ChoiceField):
    """A grouped, ordered choice field backed by the shared read result."""

    def __init__(self, alternatives, *, empty_label=None, **kwargs):
        self.alternatives = alternatives
        self._physicians = {}
        grouped_choices = []
        if alternatives.current is not None:
            current = alternatives.current
            self._physicians[str(current.physician.pk)] = current.physician
            grouped_choices.append(
                (
                    "Current assignment",
                    [(str(current.physician.pk), self._choice_label(current))],
                )
            )
        for tier in alternatives.tiers:
            choices = []
            for alternative in tier.alternatives:
                self._physicians[str(alternative.physician.pk)] = alternative.physician
                choices.append((str(alternative.physician.pk), self._choice_label(alternative)))
            if choices:
                grouped_choices.append((tier.label, choices))
        if empty_label is not None:
            grouped_choices.insert(0, ("", empty_label))
        super().__init__(choices=grouped_choices, **kwargs)

    def _choice_label(self, alternative):
        return alternative.physician.display_name

    def clean(self, value):
        if value in self.empty_values:
            super().clean(value)
            return None
        value = super().clean(value)
        try:
            return self._physicians[str(value)]
        except KeyError:
            raise forms.ValidationError(
                "Choose one of the listed assignment alternatives."
            ) from None


class AssignmentForm(forms.Form):
    is_locked = forms.BooleanField(required=False, label="Lock this assignment")
    reason = optional_note_field()

    def __init__(self, *args, session, assignment=None, alternatives=None, **kwargs):
        super().__init__(*args, **kwargs)
        if alternatives is None:
            if assignment is None:
                raise ValueError(
                    "AssignmentForm needs alternatives for an empty assignment position."
                )
            alternatives = assignment_alternatives_for_session(
                session,
                assignment.responsibility,
                assignment.primary_position,
            )
        self.alternatives = alternatives
        self.fields["physician"] = AssignmentPhysicianChoiceField(
            alternatives,
            label="Physician alternative",
            required=True,
        )
        self.order_fields(("physician", "is_locked", "reason"))


class AssignmentReasonForm(forms.Form):
    reason = optional_note_field()


class FindingAcknowledgementForm(forms.Form):
    reason = forms.CharField(
        label="Reason for this exception",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
