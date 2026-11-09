from django import forms

from .shared import optional_note_field


class LateDocAssignmentForm(forms.Form):
    override = forms.BooleanField(
        required=False,
        label="Record unavailable physician anyway",
        help_text="This retains a hard availability finding for the weekly draft.",
    )
    note = optional_note_field()

    def __init__(self, *args, candidates, **kwargs):
        super().__init__(*args, **kwargs)
        self._physicians = {}
        choices = []
        if candidates.current:
            current = candidates.current
            self._physicians[str(current.physician.pk)] = current.physician
            choices.append(
                (
                    "Current assignment",
                    [(str(current.physician.pk), current.physician.display_name)],
                )
            )
        for candidate in candidates.candidates:
            self._physicians[str(candidate.physician.pk)] = candidate.physician
            choices.append(
                (
                    candidate.tier_label,
                    [
                        (
                            str(candidate.physician.pk),
                            candidate.physician.display_name,
                        )
                    ],
                )
            )
        self.fields["physician"] = forms.ChoiceField(
            choices=choices, label="Late Doc physician", required=True
        )
        self.order_fields(("physician", "override", "note"))

    def clean_physician(self):
        try:
            return self._physicians[self.cleaned_data["physician"]]
        except KeyError:
            raise forms.ValidationError("Choose one of the listed Late Doc physicians.") from None


class LateDocRemovalForm(forms.Form):
    note = optional_note_field()
